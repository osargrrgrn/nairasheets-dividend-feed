from pathlib import Path
import json
import re

from .discover import discover_official_pdfs, title_is_strongly_irrelevant
from .pdf_extract import download_pdf_text, compact
from .parse import parse_dividend_pdf, has_dividend_evidence, make_event_id
from .tickers import resolve_ticker
from .validate import validate_event
from .publish import read_csv, merge_events, write_csv, write_html

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
STATE = ROOT / "collector_state.json"
ARCHIVE = ROOT / "disclosure_archive.json"
FEED = DOCS / "dividends.csv"
PENDING_FEED = DOCS / "pending_dividends.csv"

FINANCIAL_STATEMENT_HINTS = (
    "financial statement",
    "financial statements",
    "quarter 1",
    "quarter 2",
    "quarter 3",
    "quarter 4",
    "quarter 5",
    "annual report",
)

NON_ACTIONABLE_TITLE_HINTS = (
    "notice of annual general meeting",
    "notices of annual general meeting",
    "agm notice",
    "notice of meeting",
)

DATE_FIELDS = (
    "qualification_date",
    "payment_date",
    "closure_date",
    "announcement_date",
)

CURRENT_ACTION_PATTERNS = (
    r"\bboard\b[\s\S]{0,180}\b(?:proposed|recommended|approved|declared)\b[\s\S]{0,180}\b(?:dividend|distribution)\b",
    r"\b(?:proposed|recommended|approved|declared)\b[\s\S]{0,180}\b(?:interim|final|special)?\s*(?:dividend|distribution)\b",
    r"\bsubsequent\s+to\s+(?:the\s+)?(?:balance\s+sheet|reporting)\s+date\b[\s\S]{0,300}\b(?:dividend|distribution)\b",
    r"\b(?:interim|final|special)\s+dividend\b[\s\S]{0,250}\b(?:per\s+(?:ordinary\s+)?share|kobo)\b",
)

def financial_statement_has_current_dividend_action(text, provisional):
    """
    Financial statements often contain historical dividend notes.
    Keep one only when there is evidence of a current actionable dividend.
    """
    low = text.lower()

    if provisional.qualification_date or provisional.payment_date:
        return True

    if float(provisional.dividend_per_share or 0) <= 0:
        return False

    return any(re.search(pattern, low, re.I | re.S) for pattern in CURRENT_ACTION_PATTERNS)

def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

def load_state():
    return load_json(STATE, {"processed": {}})

def save_state(state):
    save_json(STATE, state)

def merge_archive(existing, newly_discovered):
    by_url = {}

    for item in existing:
        url = item.get("url", "")
        if url:
            by_url[url] = item

    for item in newly_discovered:
        url = item.get("url", "")
        if not url:
            continue

        previous = by_url.get(url, {})
        merged = dict(previous)
        merged.update(item)
        by_url[url] = merged

    return sorted(by_url.values(), key=lambda x: x.get("url", ""))

def normalise_type(value):
    value = (value or "").strip().lower()

    aliases = {
        "dividend": "",
        "distribution": "",
    }

    return aliases.get(value, value)

def same_dividend_type(a, b):
    a = normalise_type(a)
    b = normalise_type(b)

    if not a or not b:
        return True

    return a == b

def amounts_match(a, b):
    try:
        a = float(a or 0)
        b = float(b or 0)
    except Exception:
        return False

    if a <= 0 or b <= 0:
        return False

    return abs(a - b) <= max(0.0001, 0.001 * max(abs(a), abs(b)))

def pending_match_score(candidate, pending):
    if not candidate.ticker or candidate.ticker.upper() != (pending.get("ticker") or "").upper():
        return -1

    if not same_dividend_type(candidate.dividend_type, pending.get("dividend_type")):
        return -1

    candidate_dps = float(candidate.dividend_per_share or 0)
    pending_dps = float(pending.get("dividend_per_share") or 0)

    score = 0

    if amounts_match(candidate_dps, pending_dps):
        score += 100
    elif candidate_dps > 0 and pending_dps > 0:
        return -1
    else:
        score += 15

    candidate_dates = sum(bool(getattr(candidate, field, "")) for field in DATE_FIELDS)
    pending_dates = sum(bool(pending.get(field)) for field in DATE_FIELDS)

    score += candidate_dates * 5
    score += pending_dates * 2

    return score

def find_pending_match(candidate, pending_rows):
    matches = []

    for row in pending_rows:
        score = pending_match_score(candidate, row)
        if score >= 0:
            matches.append((score, row))

    if not matches:
        return None

    matches.sort(key=lambda x: x[0], reverse=True)

    if float(candidate.dividend_per_share or 0) <= 0:
        best_score = matches[0][0]
        equally_good = [row for score, row in matches if score == best_score]
        if len(equally_good) > 1:
            return None

    return matches[0][1]

def enrich_from_pending(candidate, pending):
    if not pending:
        return candidate

    if float(candidate.dividend_per_share or 0) <= 0:
        candidate.dividend_per_share = float(pending.get("dividend_per_share") or 0)

    if not candidate.currency:
        candidate.currency = pending.get("currency") or "NGN"

    if normalise_type(candidate.dividend_type) == "":
        candidate.dividend_type = pending.get("dividend_type") or candidate.dividend_type

    for field in (
        "qualification_date",
        "payment_date",
        "closure_date",
        "announcement_date",
        "registrar",
    ):
        if not getattr(candidate, field, "") and pending.get(field):
            setattr(candidate, field, pending.get(field))

    if (candidate.status or "").lower() == "proposed":
        old_status = (pending.get("status") or "").lower()
        if old_status in {"declared", "approved"}:
            candidate.status = old_status

    candidate.event_id = make_event_id(
        candidate.ticker,
        candidate.company,
        candidate.qualification_date,
        candidate.payment_date,
        candidate.dividend_per_share,
        candidate.dividend_type,
    )

    return candidate

def pending_key(row):
    return "|".join([
        (row.get("ticker") or "").upper().strip(),
        (row.get("dividend_type") or "").lower().strip(),
        (row.get("currency") or "").upper().strip(),
        str(row.get("dividend_per_share") or "").strip(),
    ])

def merge_pending(existing, incoming, promoted_keys=None):
    promoted_keys = promoted_keys or set()
    by_key = {}

    for row in existing + incoming:
        key = pending_key(row)

        if not row.get("ticker") or not row.get("dividend_per_share"):
            continue

        if key in promoted_keys:
            continue

        current = by_key.get(key)

        if current is None:
            by_key[key] = row
            continue

        def score(r):
            return sum(bool(r.get(k)) for k in [
                "qualification_date",
                "payment_date",
                "closure_date",
                "announcement_date",
                "registrar",
                "source_url",
            ])

        if score(row) >= score(current):
            by_key[key] = row

    return sorted(
        by_key.values(),
        key=lambda r: (
            r.get("announcement_date", ""),
            r.get("ticker", ""),
            r.get("dividend_type", ""),
        )
    )

def is_obvious_statement_noise(title, provisional, text=""):
    low_title = (title or "").lower()

    if not any(hint in low_title for hint in FINANCIAL_STATEMENT_HINTS):
        return False

    return not financial_statement_has_current_dividend_action(text, provisional)

def is_non_actionable_review_noise(title, provisional):
    """
    Suppress documents that contain a stray dividend/distribution word but
    produce no actionable dividend data at all.

    This intentionally does NOT suppress:
      - any positive dividend amount
      - any qualification/payment date
      - any resolved ticker with other useful evidence
    """
    dps = float(provisional.dividend_per_share or 0)
    has_dates = bool(provisional.qualification_date or provisional.payment_date)

    if dps > 0 or has_dates:
        return False

    low_title = (title or "").lower()

    # No ticker + no amount + no dates is not useful enough for manual review.
    if not provisional.ticker:
        return True

    # A plain AGM notice with no extracted dividend amount or dates is also
    # non-actionable. If it actually contains dividend terms, the parser should
    # have extracted them and it will not be suppressed by the checks above.
    if any(hint in low_title for hint in NON_ACTIONABLE_TITLE_HINTS):
        return True

    return False


def refresh_confidence(candidate):
    """
    Confidence belongs to the merged dividend event, not to the individual PDF.

    A PDF may contain only part of the event. After pending reconciliation has
    supplied the missing fields, recalculate confidence from the completed
    event before deciding whether it can be published.
    """
    candidate.confidence = "high"

    if not candidate.ticker:
        candidate.confidence = "review"
    elif float(candidate.dividend_per_share or 0) <= 0:
        candidate.confidence = "review"
    elif not candidate.qualification_date:
        candidate.confidence = "review"
    elif not candidate.payment_date:
        candidate.confidence = "review"

    return candidate


def published_event_key(row):
    """
    Identify the corporate action itself rather than the source PDF.

    Published events have qualification and payment dates, so these can be
    used safely with ticker, amount and dividend type to collapse duplicate
    disclosures such as MTNN's corporate-action notice and earnings release.
    """
    try:
        amount = float(row.get("dividend_per_share") or 0)
    except Exception:
        amount = 0.0

    return "|".join([
        (row.get("ticker") or "").upper().strip(),
        normalise_type(row.get("dividend_type")),
        (row.get("currency") or "").upper().strip(),
        f"{amount:.6f}",
        (row.get("qualification_date") or "").strip(),
        (row.get("payment_date") or "").strip(),
    ])


def published_row_score(row):
    """
    Prefer the strongest source when several official PDFs describe one event.
    Corporate-action/dividend notices beat generic earnings releases, while
    complete metadata still receives the highest weight.
    """
    title = (row.get("source_title") or "").lower()
    score = 0

    for field in (
        "ticker",
        "company",
        "dividend_per_share",
        "currency",
        "dividend_type",
        "qualification_date",
        "payment_date",
        "closure_date",
        "announcement_date",
        "registrar",
        "source_url",
        "source_title",
    ):
        if row.get(field):
            score += 2

    if "dividend" in title:
        score += 12
    if "corporate action" in title:
        score += 8
    if "distribution" in title:
        score += 5
    if "earnings release" in title:
        score -= 2
    if (row.get("confidence") or "").lower() == "high":
        score += 4

    return score


def merge_published_rows(primary, secondary):
    """
    Merge two rows representing the same published dividend.
    Keep the better source as the base but fill any missing metadata from the
    other official document.
    """
    if published_row_score(secondary) > published_row_score(primary):
        primary, secondary = secondary, primary

    merged = dict(primary)

    for field, value in secondary.items():
        if not merged.get(field) and value:
            merged[field] = value

    # Stabilise event_id so duplicate source documents cannot create separate
    # identities for the same corporate action.
    merged["event_id"] = make_event_id(
        merged.get("ticker", ""),
        merged.get("company", ""),
        merged.get("qualification_date", ""),
        merged.get("payment_date", ""),
        float(merged.get("dividend_per_share") or 0),
        merged.get("dividend_type", ""),
    )

    merged["confidence"] = "high"
    return merged


def dedupe_published_events(rows):
    by_event = {}

    for row in rows:
        key = published_event_key(row)

        # Never collapse malformed rows with no ticker or amount.
        if not row.get("ticker") or float(row.get("dividend_per_share") or 0) <= 0:
            fallback_key = "EVENT_ID|" + (row.get("event_id") or row.get("source_url") or repr(row))
            by_event[fallback_key] = row
            continue

        current = by_event.get(key)

        if current is None:
            by_event[key] = dict(row)
        else:
            by_event[key] = merge_published_rows(current, row)

    return sorted(
        by_event.values(),
        key=lambda r: (
            r.get("qualification_date", ""),
            r.get("ticker", ""),
            r.get("dividend_type", ""),
        )
    )


def review_reason_codes(provisional, errors):
    reasons = []

    if not provisional.ticker:
        reasons.append("ticker_unresolved")

    if float(provisional.dividend_per_share or 0) <= 0:
        reasons.append("dividend_amount_unresolved")

    if not provisional.qualification_date:
        reasons.append("qualification_date_missing")

    if not provisional.payment_date:
        reasons.append("payment_date_missing")

    if errors:
        reasons.append("validation_failed")

    return reasons

def main():
    state = load_state()
    processed = state.setdefault("processed", {})

    current_discovered = discover_official_pdfs()

    old_archive = load_json(ARCHIVE, [])
    archive = merge_archive(old_archive, current_discovered)
    save_json(ARCHIVE, archive)

    discovered = archive

    existing_pending = read_csv(PENDING_FEED)

    accepted = []
    pending = []
    review = []
    promoted_pending_keys = set()

    rejected_non_dividend = 0
    rejected_statement_noise = 0
    rejected_non_actionable = 0
    pending_omitted_from_review = 0
    inspected = 0
    dividend_candidates = 0
    reconciled_events = 0

    for item in discovered:
        url = item["url"]
        title = item.get("title", "")

        if title_is_strongly_irrelevant(title, url):
            rejected_non_dividend += 1
            continue

        if processed.get(url) == "accepted":
            continue

        try:
            text = compact(download_pdf_text(url))
            inspected += 1

            if not has_dividend_evidence(text):
                rejected_non_dividend += 1
                processed[url] = "not_dividend"
                continue

            dividend_candidates += 1

            provisional = parse_dividend_pdf(
                text=text,
                source_url=url,
                source_title=title,
                ticker="",
            )

            provisional.ticker = resolve_ticker(
                provisional.company,
                title
            )

            if is_obvious_statement_noise(title, provisional, text):
                rejected_statement_noise += 1
                processed[url] = "statement_noise"
                continue

            matched_pending = find_pending_match(provisional, existing_pending + pending)

            if matched_pending:
                provisional = enrich_from_pending(provisional, matched_pending)

            provisional.event_id = make_event_id(
                provisional.ticker,
                provisional.company,
                provisional.qualification_date,
                provisional.payment_date,
                provisional.dividend_per_share,
                provisional.dividend_type,
            )

            # Patch 17: reconciliation may have completed an event that the
            # original PDF marked "review". Recalculate confidence now.
            provisional = refresh_confidence(provisional)

            errors = validate_event(provisional)

            if provisional.confidence == "high" and not errors:
                accepted.append(provisional.to_dict())
                processed[url] = "accepted"

                if matched_pending:
                    promoted_pending_keys.add(pending_key(matched_pending))
                    reconciled_events += 1

            elif (
                provisional.ticker
                and provisional.dividend_per_share > 0
                and provisional.dividend_type
            ):
                # Genuine but incomplete dividends belong in the pending feed,
                # not in the manual review queue as well.
                pending.append(provisional.to_dict())
                pending_omitted_from_review += 1
                processed[url] = "pending"

            elif is_non_actionable_review_noise(title, provisional):
                rejected_non_actionable += 1
                processed[url] = "non_actionable_noise"

            else:
                review.append({
                    "url": url,
                    "title": title,
                    "reason_codes": review_reason_codes(provisional, errors),
                    "errors": errors,
                    "parsed": provisional.to_dict(),
                    "classification": "review",
                    "matched_existing_pending": bool(matched_pending),
                })

                processed[url] = "review"

        except Exception as exc:
            review.append({
                "url": url,
                "title": title,
                "reason_codes": ["processing_error"],
                "errors": [repr(exc)],
                "classification": "error",
            })
            processed[url] = "error"

    existing = read_csv(FEED)
    merged = merge_events(existing, accepted)

    # Patch 17: different official PDFs can describe the same corporate action.
    # Collapse them into one published dividend event.
    before_dedupe = len(merged)
    merged = dedupe_published_events(merged)
    published_duplicates_removed = before_dedupe - len(merged)

    write_csv(FEED, merged)
    write_html(DOCS / "index.html", merged)

    merged_pending = merge_pending(
        existing_pending,
        pending,
        promoted_keys=promoted_pending_keys,
    )
    write_csv(PENDING_FEED, merged_pending)

    (ROOT / "review_queue.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    save_state(state)

    print(f"Visible/current official PDFs found: {len(current_discovered)}")
    print(f"Archived official PDFs total: {len(archive)}")
    print(f"PDFs inspected this run: {inspected}")
    print(f"Rejected as non-dividend: {rejected_non_dividend}")
    print(f"Rejected financial-statement noise: {rejected_statement_noise}")
    print(f"Rejected non-actionable review noise: {rejected_non_actionable}")
    print(f"Dividend candidates after PDF inspection: {dividend_candidates}")
    print(f"Pending dividends reconciled/promoted: {reconciled_events}")
    print(f"Published new complete events: {len(accepted)}")
    print(f"Published duplicate events removed: {published_duplicates_removed}")
    print(f"New pending dividend events: {len(pending)}")
    print(f"Pending items omitted from manual review: {pending_omitted_from_review}")
    print(f"Pending feed total: {len(merged_pending)}")
    print(f"Manual review/error items: {len(review)}")
    print(f"Published feed total: {len(merged)}")

if __name__ == "__main__":
    main()

from pathlib import Path
import json
import re

from .discover import discover_official_pdfs, title_is_strongly_irrelevant
from .pdf_extract import download_pdf_text, compact
from .parse import parse_dividend_pdf, has_dividend_evidence, make_event_id
from .tickers import resolve_ticker
from .validate import validate_event
from .publish import read_csv, merge_events, write_csv, write_html
from .backfill import discover_2026_backfill
from .reconcile import (
    reconcile_evidence,
    quarantine_uncorroborated_agm,
    suspicious_tiny_ngn,
    has_strong_corroboration,
)

from .pending_resolver import resolve_pending_events

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
STATE = ROOT / "collector_state.json"
ARCHIVE = ROOT / "disclosure_archive.json"
FEED = DOCS / "dividends.csv"
PENDING_FEED = DOCS / "pending_dividends.csv"

# Patch 21: never re-download the entire archive on every run.
# New PDFs are always processed immediately. Older unresolved documents are
# revisited in a rotating batch so parser/ticker improvements can still
# recover them over successive runs.
MAX_HISTORICAL_RECHECK = 15  # Reduced from 30 — OCR makes per-PDF processing slower

RECHECKABLE_STATES = {
    "pending",
    "review",
    "error",
}

MAX_BACKFILL_PROCESS_PER_RUN = 20  # Reduced from 40 — OCR makes per-PDF processing slower

STABLE_SKIP_STATES = {
    "accepted",
    "not_dividend",
    "statement_noise",
    "non_actionable_noise",
}

# ---------------------------------------------------------------------------
# Patch 33: High-value title signals that jump the rotating recheck queue.
# ---------------------------------------------------------------------------
HIGH_VALUE_TITLE_SIGNALS = (
    "CORPORATE_ACTION_ANNOUNCEMENT",
    "CORPORATE_ACTIONS_ANNOUNCEMENT",
    "DIVIDEND_ANNOUNCEMENT",
    "INTERIM_DIVIDEND_ANNOUNCEMENT",
    "FINAL_DIVIDEND_ANNOUNCEMENT",
    "CORPORATE_ACTION_CORPORATE_ACTIONS",
    "CORPORATE_DISCLOSURE_CORPORATE_ACTIONS",
    "NGX_NOTIFICATION",
    "NGX_DIV_ANNOUNCEMENT",
)

MAX_PRIORITY_RECHECK = 10  # Reduced from 20 — OCR makes per-PDF processing slower


def is_high_value_unprocessed(item, processed):
    url = item.get("url", "").upper()
    title = item.get("title", "").upper().replace(" ", "_")
    status = processed.get(item.get("url", ""), "")
    if status in STABLE_SKIP_STATES:
        return False
    return any(signal in url or signal in title for signal in HIGH_VALUE_TITLE_SIGNALS)


def select_priority_documents(archive, processed, already_selected_urls):
    priority = []
    for item in archive:
        url = item.get("url", "")
        if not url or url in already_selected_urls:
            continue
        if is_high_value_unprocessed(item, processed):
            priority.append(item)
        if len(priority) >= MAX_PRIORITY_RECHECK:
            break
    return priority


def below_ngn_hard_floor(event) -> bool:
    """
    Hard publication floor.

    Any NGN payout below N0.01 is never auto-published, regardless of
    corroboration. It remains available in pending/review for manual checking.
    """
    currency = (getattr(event, "currency", "") or "NGN").upper().strip()

    if currency != "NGN":
        return False

    try:
        amount = float(getattr(event, "dividend_per_share", 0) or 0)
    except Exception:
        return True

    return 0 <= amount < 0.01

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
    Recalculate completeness after reconciliation while preserving source
    quality assigned by the parser.
    """
    original = (candidate.confidence or "").lower().strip()

    complete = (
        bool(candidate.ticker)
        and float(candidate.dividend_per_share or 0) > 0
        and bool(candidate.qualification_date)
        and bool(candidate.payment_date)
    )

    if not complete:
        candidate.confidence = "review"
        return candidate

    if original == "source_review":
        candidate.confidence = "source_review"
        return candidate

    if original == "medium":
        candidate.confidence = "medium"
        return candidate

    candidate.confidence = "high"
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
    if (provisional.confidence or "").lower() == "source_review":
        reasons.append("lower_quality_source")
    elif (provisional.confidence or "").lower() == "medium":
        reasons.append("mixed_source_quality")

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


def select_documents_for_run(archive, current_discovered, processed, state):
    """
    Process all newly discovered documents plus a bounded rotating sample of
    older unresolved documents.

    This prevents every workflow run from re-downloading the full archive,
    while still allowing later parser/ticker patches to improve older pending,
    review, and error items.
    """
    current_urls = {
        item.get("url", "")
        for item in current_discovered
        if item.get("url")
    }

    by_url = {
        item.get("url", ""): item
        for item in archive
        if item.get("url")
    }

    selected = []
    selected_urls = set()

    # 1) New/current discovery always gets first priority.
    for url in current_urls:
        item = by_url.get(url)
        if item and url not in selected_urls:
            selected.append(item)
            selected_urls.add(url)

    # 2) Patch 33: Priority recheck — high-value unprocessed documents jump
    #    the rotating queue so they are never starved by less useful items.
    priority_docs = select_priority_documents(archive, processed, selected_urls)
    for item in priority_docs:
        url = item.get("url", "")
        if url and url not in selected_urls:
            selected.append(item)
            selected_urls.add(url)
    if priority_docs:
        print(
            f"[Patch 33] Priority recheck: {len(priority_docs)} high-value "
            "unprocessed documents added to selection",
            flush=True,
        )

    # 3) Build unresolved historical pool only.
    historical = []
    for item in archive:
        url = item.get("url", "")
        if not url or url in current_urls:
            continue

        status = processed.get(url, "")

        # Never waste time reprocessing stable classifications on each run.
        if status in STABLE_SKIP_STATES:
            continue

        # Revisit unresolved states and legacy/unclassified URLs.
        if status in RECHECKABLE_STATES or not status:
            historical.append(item)

    historical.sort(key=lambda item: item.get("url", ""))

    # 4) Rotate through the historical pool so the same first 30 do not
    # starve the rest of the archive.
    if historical:
        cursor = int(state.get("historical_recheck_cursor", 0) or 0)
        cursor %= len(historical)

        take = min(MAX_HISTORICAL_RECHECK, len(historical))

        for offset in range(take):
            item = historical[(cursor + offset) % len(historical)]
            url = item.get("url", "")
            if url and url not in selected_urls:
                selected.append(item)
                selected_urls.add(url)

        state["historical_recheck_cursor"] = (cursor + take) % len(historical)
    else:
        state["historical_recheck_cursor"] = 0

    return selected, len(historical)


def main():
    state = load_state()
    processed = state.setdefault("processed", {})

    # Normal live discovery remains unchanged.
    current_discovered = discover_official_pdfs()

    old_archive = load_json(ARCHIVE, [])

    # Patch 31: one-time historical 2026 backfill.
    backfill_discovered = []

    if not state.get("backfill_2026_completed"):
        known_urls = {
            item.get("url", "")
            for item in old_archive
            if isinstance(item, dict) and item.get("url")
        }

        try:
            backfill_discovered, backfill_stats = discover_2026_backfill(
                known_urls
            )

            state["backfill_2026_completed"] = True
            state["backfill_2026_candidates_added"] = len(
                backfill_discovered
            )
            state["backfill_2026_stats"] = backfill_stats

            print(
                f"[Backfill 2026] added {len(backfill_discovered)} "
                "historical candidate PDFs",
                flush=True,
            )

        except Exception as exc:
            # Do not mark completed on failure; next run may retry.
            backfill_discovered = []
            print(
                f"[Backfill 2026] failed: {repr(exc)}",
                flush=True,
            )
    else:
        print(
            "[Backfill 2026] already completed — skipping sweep",
            flush=True,
        )

    # Merge live + historical discoveries into the persistent archive.
    all_new_discovered = current_discovered + backfill_discovered
    archive = merge_archive(old_archive, all_new_discovered)
    save_json(ARCHIVE, archive)

    # Prioritize a bounded slice of backfill documents immediately.
    priority_backfill = backfill_discovered[:MAX_BACKFILL_PROCESS_PER_RUN]
    selection_discovered = current_discovered + priority_backfill

    if priority_backfill:
        print(
            f"[Backfill 2026] prioritizing {len(priority_backfill)} "
            "historical documents this run",
            flush=True,
        )

    discovered, historical_recheck_pool = select_documents_for_run(
        archive,
        selection_discovered,
        processed,
        state,
    )

    existing_pending = read_csv(PENDING_FEED)
    existing_published = read_csv(FEED)

    prior_review_items = load_json(ROOT / "review_queue.json", [])
    prior_review_evidence = [
        item.get("parsed")
        for item in prior_review_items
        if isinstance(item, dict) and isinstance(item.get("parsed"), dict)
    ]

    accepted = []
    pending = []
    review = []
    promoted_pending_keys = set()

    rejected_non_dividend = 0
    rejected_statement_noise = 0
    rejected_non_actionable = 0
    priority_recheck_count = 0
    pending_omitted_from_review = 0
    inspected = 0
    dividend_candidates = 0
    reconciled_events = 0
    cross_document_reconciliations = 0
    agm_rows_demoted = 0
    tiny_amount_rows_held = 0

    for item in discovered:
        url = item["url"]
        title = item.get("title", "")

        if is_high_value_unprocessed(item, processed):
            priority_recheck_count += 1

        if title_is_strongly_irrelevant(title, url):
            rejected_non_dividend += 1
            continue

        if processed.get(url) in STABLE_SKIP_STATES and url not in {
            item.get("url", "") for item in current_discovered
        }:
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

            provisional.ticker = (
                resolve_ticker(provisional.company, title)
                or (item.get("ticker") or "").upper().strip()
            )

            if is_obvious_statement_noise(title, provisional, text):
                rejected_statement_noise += 1
                processed[url] = "statement_noise"
                continue

            matched_pending = find_pending_match(provisional, existing_pending + pending)

            if matched_pending:
                provisional = enrich_from_pending(provisional, matched_pending)

            evidence_rows = (
                existing_pending
                + pending
                + existing_published
                + accepted
                + prior_review_evidence
            )

            provisional, cross_match, corroborated = reconcile_evidence(
                provisional,
                evidence_rows,
                title,
                url,
            )

            if cross_match:
                cross_document_reconciliations += 1

            # A lower-quality AGM/financial-statement source can become
            # publishable only when independently corroborated by stronger
            # official evidence.
            if corroborated and provisional.confidence in {
                "source_review",
                "medium",
                "review",
            }:
                provisional.confidence = "high"

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

            current_evidence_rows = (
                existing_pending
                + pending
                + existing_published
                + accepted
                + prior_review_evidence
            )

            # Patch 26 hard floor:
            # Any NGN payout below N0.01 can never auto-publish.
            hard_floor_hold = below_ngn_hard_floor(provisional)

            if hard_floor_hold:
                provisional.confidence = "review"
                tiny_amount_rows_held += 1

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

    evidence_for_existing = (
        existing_pending + pending + accepted + prior_review_evidence
    )

    safe_existing, demoted_agm_rows = quarantine_uncorroborated_agm(
        existing_published,
        evidence_for_existing,
    )
    agm_rows_demoted = len(demoted_agm_rows)

    safe_after_tiny = []
    demoted_tiny_rows = []

    for row in safe_existing:
        currency = (row.get("currency") or "NGN").upper().strip()
        try:
            amount = float(row.get("dividend_per_share") or 0)
        except Exception:
            amount = 0.0

        # Patch 26 hard floor applies to existing published rows too.
        if currency == "NGN" and 0 <= amount < 0.01:
            demoted = dict(row)
            demoted["confidence"] = "review"
            demoted["hold_reason"] = f"sub_1kobo_ngn:{amount}"
            demoted_tiny_rows.append(demoted)
        else:
            safe_after_tiny.append(row)

    tiny_amount_rows_held += len(demoted_tiny_rows)

    # Preserve questionable evidence in pending rather than deleting it.
    pending.extend(demoted_agm_rows)
    pending.extend(demoted_tiny_rows)

    # Patch 32: resolve complementary/duplicate pending evidence already in
    # the archive. Promotion requires independent official PDFs, matching
    # ticker/amount/type, non-conflicting fields, complete qualification and
    # payment dates, strong-source corroboration, and all existing safety rules.
    resolved_pending, unresolved_pending, pending_resolution_stats = (
        resolve_pending_events(
            existing_pending + pending,
            published_rows=safe_after_tiny + accepted,
        )
    )

    for row in resolved_pending:
        row["event_id"] = make_event_id(
            row.get("ticker", ""),
            row.get("company", ""),
            row.get("qualification_date", ""),
            row.get("payment_date", ""),
            row.get("dividend_per_share", 0),
            row.get("dividend_type", ""),
        )

    accepted.extend(resolved_pending)

    merged = merge_events(safe_after_tiny, accepted)

    # Patch 17: different official PDFs can describe the same corporate action.
    # Collapse them into one published dividend event.
    before_dedupe = len(merged)
    merged = dedupe_published_events(merged)
    published_duplicates_removed = before_dedupe - len(merged)

    write_csv(FEED, merged)
    write_html(DOCS / "index.html", merged)

    merged_pending = merge_pending(
        [],
        unresolved_pending,
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
    print(f"Historical unresolved recheck pool: {historical_recheck_pool}")
    print(f"[Patch 33] Priority high-value documents processed: {priority_recheck_count}")
    print(f"Documents selected for this run: {len(discovered)}")
    print(f"PDFs inspected this run: {inspected}")
    print(f"Rejected as non-dividend: {rejected_non_dividend}")
    print(f"Rejected financial-statement noise: {rejected_statement_noise}")
    print(f"Rejected non-actionable review noise: {rejected_non_actionable}")
    print(f"Dividend candidates after PDF inspection: {dividend_candidates}")
    print(f"Pending dividends reconciled/promoted: {reconciled_events}")
    print(f"Cross-document reconciliations: {cross_document_reconciliations}")
    print(f"Uncorroborated AGM rows demoted from published: {agm_rows_demoted}")
    print(f"Sub-1-kobo NGN rows held/demoted: {tiny_amount_rows_held}")
    print(
        "Pending resolver: "
        f"promoted={pending_resolution_stats['promoted']} "
        f"stale_published_removed="
        f"{pending_resolution_stats['stale_published_duplicates_removed']} "
        f"conflicts={pending_resolution_stats['blocked_conflict']} "
        f"incomplete={pending_resolution_stats['blocked_incomplete']} "
        f"single_source={pending_resolution_stats['blocked_single_source']} "
        f"safety_holds={pending_resolution_stats['blocked_safety']}"
    )
    print(f"Published new complete events: {len(accepted)}")
    print(f"Published duplicate events removed: {published_duplicates_removed}")
    print(f"New pending dividend events: {len(pending)}")
    print(f"Pending items omitted from manual review: {pending_omitted_from_review}")
    print(f"Pending feed total: {len(merged_pending)}")
    print(f"Manual review/error items: {len(review)}")
    print(f"Published feed total: {len(merged)}")

if __name__ == "__main__":
    main()

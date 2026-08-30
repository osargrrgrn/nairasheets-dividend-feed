from pathlib import Path
import json
from .discover import discover_official_pdfs, title_is_strongly_irrelevant
from .pdf_extract import download_pdf_text, compact
from .parse import parse_dividend_pdf, has_dividend_evidence
from .tickers import resolve_ticker
from .validate import validate_event
from .publish import read_csv, merge_events, write_csv, write_html

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
STATE = ROOT / "collector_state.json"
FEED = DOCS / "dividends.csv"
PENDING_FEED = DOCS / "pending_dividends.csv"

def load_state():
    if not STATE.exists():
        return {"processed": {}}
    return json.loads(STATE.read_text(encoding="utf-8"))

def save_state(state):
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

def pending_key(row):
    return "|".join([
        row.get("ticker", "").upper().strip(),
        row.get("dividend_type", "").lower().strip(),
        row.get("currency", "").upper().strip(),
        str(row.get("dividend_per_share", "")).strip(),
    ])

def merge_pending(existing, incoming):
    """
    Keep the newest/best version of the same incomplete dividend event.
    Matching is intentionally conservative: ticker + type + currency + DPS.
    """
    by_key = {}

    for row in existing + incoming:
        key = pending_key(row)
        if not row.get("ticker") or not row.get("dividend_per_share"):
            continue

        current = by_key.get(key)
        if current is None:
            by_key[key] = row
            continue

        # Prefer whichever record has more useful dates/metadata.
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

def main():
    state = load_state()
    processed = state.setdefault("processed", {})

    discovered = discover_official_pdfs()

    accepted = []
    pending = []
    review = []

    rejected_non_dividend = 0
    inspected = 0
    dividend_candidates = 0

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

            from .parse import make_event_id
            provisional.event_id = make_event_id(
                provisional.ticker,
                provisional.company,
                provisional.qualification_date,
                provisional.payment_date,
                provisional.dividend_per_share,
                provisional.dividend_type,
            )

            errors = validate_event(provisional)

            if provisional.confidence == "high" and not errors:
                accepted.append(provisional.to_dict())
                processed[url] = "accepted"

            elif (
                provisional.ticker
                and provisional.dividend_per_share > 0
                and provisional.dividend_type
            ):
                # Genuine dividend, but missing dates or another required field.
                # Preserve it automatically so a later NGX filing can enrich it.
                pending.append(provisional.to_dict())

                review.append({
                    "url": url,
                    "title": title,
                    "errors": errors,
                    "parsed": provisional.to_dict(),
                    "classification": "pending_dividend",
                })

                processed[url] = "pending"

            else:
                review.append({
                    "url": url,
                    "title": title,
                    "errors": errors,
                    "parsed": provisional.to_dict(),
                    "classification": "review",
                })

                processed[url] = "review"

        except Exception as exc:
            review.append({
                "url": url,
                "title": title,
                "errors": [repr(exc)],
                "classification": "error",
            })
            processed[url] = "error"

    existing = read_csv(FEED)
    merged = merge_events(existing, accepted)
    write_csv(FEED, merged)
    write_html(DOCS / "index.html", merged)

    existing_pending = read_csv(PENDING_FEED)
    merged_pending = merge_pending(existing_pending, pending)
    write_csv(PENDING_FEED, merged_pending)

    (ROOT / "review_queue.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    save_state(state)

    print(f"Discovered official PDFs: {len(discovered)}")
    print(f"PDFs inspected: {inspected}")
    print(f"Rejected as non-dividend: {rejected_non_dividend}")
    print(f"Dividend candidates after PDF inspection: {dividend_candidates}")
    print(f"Published new complete events: {len(accepted)}")
    print(f"New pending dividend events: {len(pending)}")
    print(f"Pending feed total: {len(merged_pending)}")
    print(f"Review/error items: {len(review)}")
    print(f"Published feed total: {len(merged)}")

if __name__ == "__main__":
    main()

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

def load_state():
    if not STATE.exists():
        return {"processed": {}}
    return json.loads(STATE.read_text(encoding="utf-8"))

def save_state(state):
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

def main():
    state = load_state()
    processed = state.setdefault("processed", {})

    discovered = discover_official_pdfs()

    accepted = []
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
            provisional.ticker = resolve_ticker(provisional.company, title)

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
            else:
                review.append({
                    "url": url,
                    "title": title,
                    "errors": errors,
                    "parsed": provisional.to_dict(),
                })
                processed[url] = "review"

        except Exception as exc:
            review.append({
                "url": url,
                "title": title,
                "errors": [repr(exc)],
            })
            processed[url] = "error"

    existing = read_csv(FEED)
    merged = merge_events(existing, accepted)
    write_csv(FEED, merged)
    write_html(DOCS / "index.html", merged)

    (ROOT / "review_queue.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    save_state(state)

    print(f"Discovered official PDFs: {len(discovered)}")
    print(f"PDFs inspected: {inspected}")
    print(f"Rejected as non-dividend: {rejected_non_dividend}")
    print(f"Dividend candidates after PDF inspection: {dividend_candidates}")
    print(f"Published new events: {len(accepted)}")
    print(f"Review/error items: {len(review)}")
    print(f"Feed total: {len(merged)}")

if __name__ == "__main__":
    main()

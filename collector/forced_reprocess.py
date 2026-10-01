"""One-shot forced reprocessing for known NGX document IDs.

Document IDs live in forced_reprocess.json. Each queued document is downloaded
and parsed again even if collector_state marks its URL as accepted/stable. A
successful complete reparse replaces rows from that exact official source in
the published feed, updates the HTML feed, marks the URL accepted, and removes
the document ID from the queue. Failed/incomplete reparses stay queued for the
next run.
"""

from pathlib import Path
import json
import re

from .pdf_extract import download_pdf_text, compact
from .parse import parse_dividend_pdf, make_event_id
from .tickers import resolve_ticker
from .validate import validate_event
from .publish import read_csv, write_csv, write_html
from .feed_integrity import validate_published_feed, finalize_published_rows

ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "forced_reprocess.json"
ARCHIVE = ROOT / "disclosure_archive.json"
STATE = ROOT / "collector_state.json"
FEED = ROOT / "docs" / "dividends.csv"
HTML = ROOT / "docs" / "index.html"


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _document_id(url):
    name = (url or "").rsplit("/", 1)[-1]
    match = re.match(r"(\d+)(?:_|\b)", name)
    return match.group(1) if match else ""


def _find_archive_item(archive, doc_id):
    for item in archive:
        if isinstance(item, dict) and _document_id(item.get("url", "")) == str(doc_id):
            return item
    return None


def _reparse(item):
    url = item["url"]
    title = item.get("title", "")
    text = compact(download_pdf_text(url))
    event = parse_dividend_pdf(
        text=text,
        source_url=url,
        source_title=title,
        ticker="",
    )
    event.ticker = (
        resolve_ticker(event.company, title)
        or (item.get("ticker") or "").upper().strip()
    )
    event.event_id = make_event_id(
        event.ticker,
        event.company,
        event.qualification_date,
        event.payment_date,
        event.dividend_per_share,
        event.dividend_type,
    )
    errors = validate_event(event)
    complete = (
        bool(event.ticker)
        and float(event.dividend_per_share or 0) > 0
        and bool(event.qualification_date)
        and bool(event.payment_date)
        and not errors
    )
    if not complete:
        raise ValueError(f"forced reparse incomplete/invalid: {errors or event.to_dict()}")
    return event.to_dict()


def main():
    queue = _load(QUEUE, {"document_ids": []})
    queued = [str(x) for x in queue.get("document_ids", [])]
    if not queued:
        print("[ForcedReprocess] queue empty")
        return

    archive = _load(ARCHIVE, [])
    state = _load(STATE, {"processed": {}})
    processed = state.setdefault("processed", {})
    rows = read_csv(FEED)
    completed = []

    for doc_id in queued:
        item = _find_archive_item(archive, doc_id)
        if not item:
            print(f"[ForcedReprocess] {doc_id}: not found in disclosure archive; keeping queued")
            continue

        url = item["url"]
        try:
            corrected = _reparse(item)

            # Replace every previously published interpretation of this exact
            # official PDF. This is deliberate: a corrected amount changes the
            # event_id, so ordinary event-id merging cannot remove the stale row.
            rows = [r for r in rows if (r.get("source_url") or "").strip() != url.strip()]
            rows.append(corrected)
            rows, rejected = finalize_published_rows(rows)
            if rejected:
                rejected_same_source = [
                    r for r in rejected
                    if (r.get("source_url") or "").strip() == url.strip()
                ]
                if rejected_same_source:
                    raise ValueError(f"corrected row rejected by finalizer: {rejected_same_source}")

            errors, _warnings = validate_published_feed(rows)
            if errors:
                raise ValueError(f"feed integrity failed after forced reparse: {errors}")

            processed[url] = "accepted"
            completed.append(doc_id)
            print(
                f"[ForcedReprocess] {doc_id}: repaired {corrected.get('ticker')} "
                f"to {corrected.get('dividend_per_share')} {corrected.get('currency', 'NGN')}"
            )
        except Exception as exc:
            print(f"[ForcedReprocess] {doc_id}: FAILED {exc!r}; keeping queued")

    if completed:
        write_csv(FEED, rows)
        write_html(HTML, rows)
        _save(STATE, state)
        queue["document_ids"] = [x for x in queued if x not in set(completed)]
        _save(QUEUE, queue)
        print(f"[ForcedReprocess] completed and dequeued: {', '.join(completed)}")


if __name__ == "__main__":
    main()

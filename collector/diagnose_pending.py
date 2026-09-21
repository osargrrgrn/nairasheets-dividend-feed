"""
Read-only diagnostic: why are pending dividend events still incomplete?

For every row in docs/pending_dividends.csv that is missing a payment date
(or a qualification date), this downloads the source PDF, extracts the text
exactly the way the pipeline does, and prints the surrounding wording for
every date and every payment-related phrase it can find.

Nothing is written. Nothing is published. This only produces evidence so the
date extractors in collector/parse.py can be fixed against real wording
instead of guesswork.

Usage (from a workflow or locally):
    python -u -m collector.diagnose_pending
    python -u -m collector.diagnose_pending 25 50   # limit, offset
"""

import csv
import re
import sys
from pathlib import Path

from .pdf_extract import download_pdf_text, compact
from .parse import (
    normalize_ngx_dividend_text,
    extract_payment_date,
    extract_qualification_date,
    infer_currency_and_dps,
)

ROOT = Path(__file__).resolve().parents[1]
PENDING_FEED = ROOT / "docs" / "pending_dividends.csv"

MONTHS = (
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

# Any date-shaped string, in the formats NGX filings actually use.
DATE_RE = re.compile(
    rf"(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?(?:{MONTHS})\.?,?\s+\d{{4}}"
    rf"|(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}})",
    re.I,
)

# Words that should sit near a payment date in a real filing.
PAY_WORD_RE = re.compile(
    r"payment|payable|paid|pay\s+date|e-?dividend|warrant|credit(?:ed)?\s+to|"
    r"remit|disburse|distribution\s+date|effective\s+date",
    re.I,
)

QUAL_WORD_RE = re.compile(
    r"qualification|record\s+date|closure|close\s+of\s+business|register\s+of\s+members|"
    r"books?\s+clos|entitle",
    re.I,
)

CONTEXT = 110
MAX_SNIPPETS = 8


def _snippets(text, regex, limit=MAX_SNIPPETS):
    out = []
    seen = set()
    for m in regex.finditer(text):
        a = max(0, m.start() - CONTEXT)
        b = min(len(text), m.end() + CONTEXT)
        frag = re.sub(r"\s+", " ", text[a:b]).strip()
        low = frag.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(frag)
        if len(out) >= limit:
            break
    return out


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    offset = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    if not PENDING_FEED.exists():
        print(f"No pending feed at {PENDING_FEED}")
        return

    with PENDING_FEED.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    incomplete = [
        r for r in rows
        if not (r.get("qualification_date", "").strip()
                and r.get("payment_date", "").strip())
    ]

    # One entry per source document; keep every row that cites it.
    by_url = {}
    for r in incomplete:
        url = (r.get("source_url") or "").strip()
        if not url:
            continue
        by_url.setdefault(url, []).append(r)

    urls = sorted(by_url.keys())
    total = len(urls)

    print("=" * 74)
    print(f"Pending rows: {len(rows)}   incomplete: {len(incomplete)}   "
          f"distinct source documents: {total}")
    print(f"Inspecting documents {offset} to {min(offset + limit, total)}")
    print("=" * 74)

    for i, url in enumerate(urls[offset:offset + limit], start=offset + 1):
        group = by_url[url]
        head = group[0]

        print()
        print("-" * 74)
        print(f"[{i}/{total}] {head.get('source_title', '')[:90]}")
        for r in group:
            print(f"    row: {r.get('ticker','?'):<12} "
                  f"amount={r.get('dividend_per_share','?'):<8} "
                  f"qd={r.get('qualification_date') or '-':<12} "
                  f"pd={r.get('payment_date') or '-':<12} "
                  f"type={r.get('dividend_type','') }")
        print(f"    url: {url}")

        try:
            raw = download_pdf_text(url)
        except Exception as exc:
            print(f"    !! download/extract failed: {exc!r}")
            continue

        if not raw or not raw.strip():
            print("    !! no text extracted (scanned PDF with no OCR result?)")
            continue

        text = normalize_ngx_dividend_text(compact(raw))
        print(f"    text length: {len(text)}")

        # What the current extractors make of it.
        try:
            cur_q = extract_qualification_date(text)
        except Exception as exc:
            cur_q = f"ERROR {exc!r}"
        try:
            cur_p = extract_payment_date(text)
        except Exception as exc:
            cur_p = f"ERROR {exc!r}"
        try:
            cur_amt = infer_currency_and_dps(text)
        except Exception as exc:
            cur_amt = f"ERROR {exc!r}"

        print(f"    extract_qualification_date -> {cur_q or '(none)'}")
        print(f"    extract_payment_date       -> {cur_p or '(none)'}")
        print(f"    infer_currency_and_dps     -> {cur_amt}")

        dates = DATE_RE.findall(text)
        print(f"    date-shaped strings in document: {len(dates)}")
        if dates:
            uniq = []
            for d in dates:
                d = re.sub(r"\s+", " ", d).strip()
                if d not in uniq:
                    uniq.append(d)
            print(f"      {uniq[:12]}")

        pay = _snippets(text, PAY_WORD_RE)
        print(f"    -- payment wording ({len(pay)} shown) --")
        for s in pay:
            print(f"      > {s}")

        if not head.get("qualification_date", "").strip():
            qual = _snippets(text, QUAL_WORD_RE, limit=5)
            print(f"    -- qualification wording ({len(qual)} shown) --")
            for s in qual:
                print(f"      > {s}")

    print()
    print("=" * 74)
    print(f"Done. Inspected {min(limit, max(0, total - offset))} of {total} documents.")
    if offset + limit < total:
        print(f"Next batch: python -u -m collector.diagnose_pending {limit} {offset + limit}")
    print("Read-only; nothing was written.")


if __name__ == "__main__":
    main()

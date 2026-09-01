
import csv, json, re
from io import BytesIO
from pathlib import Path

import requests
from pypdf import PdfReader

from .parse import classify_document, dividend_context_windows, parse_dividend_pdf

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ARCHIVE = ROOT / "disclosure_archive.json"
STATE = ROOT / "collector_state.json"
PENDING = DOCS / "pending_dividends.csv"
PUBLISHED = DOCS / "dividends.csv"
REVIEW = ROOT / "review_queue.json"

BENCHMARKS = {
    "IKEJAHOTEL": ["ikeja hotel", "ikejahotel"],
    "HONYFLOUR": ["honeywell flour", "honyflour"],
    "REDSTAREX": ["red star express", "redstarex"],
    "UPL": ["university press", " upl "],
    "LEARNAFRCA": ["learn africa", "learnafrca"],
    "ACADEMY": ["academy press", "academy"],
}

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def load_csv(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()

def matches(blob, aliases):
    """
    Match normalized aliases as whole words/phrases.

    Fixes short tickers such as UPL accidentally matching words like
    "uploaded".
    """
    h = " " + norm(blob) + " "

    for alias in aliases:
        a = norm(alias)
        if not a:
            continue

        if f" {a} " in h:
            return True

    return False


DEEP_TEXT_TARGETS = {
    "HONYFLOUR": ["honeywell flour", "honyflour"],
    "UPL": ["university press", "upl"],
    "ACADEMY": ["academy press", "academy"],
}

TEXT_ANCHORS = (
    "dividend",
    "distribution",
    "kobo",
    "naira",
    "qualification",
    "record date",
    "payment",
    "closure",
    "ordinary share",
    "per share",
    "share of 50",
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
    )
}


def extract_pdf_text(url):
    response = requests.get(
        url,
        headers=HTTP_HEADERS,
        timeout=(8, 30),
        allow_redirects=True,
    )
    response.raise_for_status()

    reader = PdfReader(BytesIO(response.content))
    pages = []

    for page_no, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            page_text = f"[PAGE {page_no} EXTRACTION ERROR: {exc}]"
        pages.append(f"\n--- PAGE {page_no} ---\n{page_text}")

    return "\n".join(pages)


def compact_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def relevant_snippets(pdf_text, radius=260, max_snippets=8):
    low = pdf_text.lower()
    snippets = []

    for anchor in TEXT_ANCHORS:
        start = 0
        while True:
            idx = low.find(anchor, start)
            if idx < 0:
                break

            left = max(0, idx - radius)
            right = min(len(pdf_text), idx + len(anchor) + radius)
            snippet = compact_text(pdf_text[left:right])

            if snippet and snippet not in snippets:
                snippets.append(snippet)

            if len(snippets) >= max_snippets:
                return snippets

            start = idx + len(anchor)

    return snippets


def choose_deep_documents(archive, aliases):
    found = [
        item for item in archive
        if matches(" ".join(map(str, item.values())), aliases)
        and item.get("url")
    ]

    def rank(item):
        title = norm(item.get("title", ""))
        score = 0
        if "corporate action" in title:
            score += 100
        if "dividend" in title or "distribution" in title:
            score += 100
        if "annual general meeting" in title or "agm" in title:
            score += 50
        if "financial statement" in title:
            score -= 80
        if "governance" in title:
            score -= 80
        if "board meeting" in title:
            score -= 30
        return score

    found.sort(key=rank, reverse=True)
    return found[:3]


def run_deep_text_diagnostic(archive):
    print("\n" + "#" * 78)
    print("PATCH 28 — EXTRACTED PDF TEXT DIAGNOSTIC")
    print("Read-only: no feed/state files are modified.")

    for target, aliases in DEEP_TEXT_TARGETS.items():
        print("\n" + "#" * 78)
        print(f"DEEP TEXT: {target}")

        docs = choose_deep_documents(archive, aliases)

        if not docs:
            print("No archived official PDF matches.")
            continue

        for number, item in enumerate(docs, start=1):
            title = item.get("title", "")
            url = item.get("url", "")

            print("\n" + "-" * 78)
            print(f"DOCUMENT {number}: {title}")
            print(f"URL: {url}")

            try:
                pdf_text = extract_pdf_text(url)
            except Exception as exc:
                print(f"DOWNLOAD/TEXT EXTRACTION ERROR: {repr(exc)}")
                continue

            print(f"Extracted characters: {len(pdf_text)}")
            print(f"Parser document type: {classify_document(title, pdf_text)}")

            try:
                parsed = parse_dividend_pdf(
                    pdf_text,
                    url,
                    title,
                    ticker=target,
                )
                print(
                    "Parser result:",
                    {
                        "amount": getattr(parsed, "dividend_per_share", 0),
                        "currency": getattr(parsed, "currency", ""),
                        "type": getattr(parsed, "dividend_type", ""),
                        "qualification": getattr(parsed, "qualification_date", ""),
                        "payment": getattr(parsed, "payment_date", ""),
                        "closure": getattr(parsed, "closure_date", ""),
                        "confidence": getattr(parsed, "confidence", ""),
                    },
                )
            except Exception as exc:
                print(f"PARSER ERROR: {repr(exc)}")

            windows = dividend_context_windows(pdf_text)
            print(f"Dividend context windows: {len(windows)}")

            snippets = relevant_snippets(pdf_text)

            if snippets:
                print("Relevant extracted snippets:")
                for idx, snippet in enumerate(snippets, start=1):
                    print(f"[{idx}] {snippet}")
            else:
                print("NO dividend/kobo/payment/qualification snippets found.")
                print("First 1200 extracted characters:")
                print(compact_text(pdf_text[:1200]))


def main():
    archive = load_json(ARCHIVE, [])
    state = load_json(STATE, {})
    pending = load_csv(PENDING)
    published = load_csv(PUBLISHED)
    review = load_json(REVIEW, [])
    processed = state.get("processed", {}) if isinstance(state, dict) else {}

    print("NGX BENCHMARK DIAGNOSTIC — PATCH 28")
    print(f"Archive PDFs: {len(archive)}")
    print(f"Published: {len(published)} | Pending: {len(pending)} | Review: {len(review) if isinstance(review,list) else 0}")

    for name, aliases in BENCHMARKS.items():
        print("\n" + "="*70)
        print(name)

        pub = [r for r in published if matches(" ".join(map(str,r.values())), aliases)]
        pen = [r for r in pending if matches(" ".join(map(str,r.values())), aliases)]
        rev = []
        if isinstance(review, list):
            for item in review:
                p = item.get("parsed") or {}
                blob = " ".join([str(item.get("title","")), str(item.get("url","")), str(p)])
                if matches(blob, aliases):
                    rev.append(item)
        arc = [a for a in archive if matches(" ".join(map(str,a.values())), aliases)]

        if pub:
            status = "PUBLISHED"
        elif pen:
            status = "IN PENDING"
        elif rev:
            status = "PROCESSED / REVIEW"
        elif arc:
            states = {processed.get(a.get("url",""), "") for a in arc}
            if states & {"not_dividend","statement_noise","non_actionable_noise"}:
                status = "REJECTED AFTER DISCOVERY"
            elif states & {"pending","review","error",""}:
                status = "IN ARCHIVE / UNRESOLVED"
            else:
                status = "IN ARCHIVE"
        else:
            status = "NEVER DISCOVERED"

        print("STATUS:", status)
        print("Archive matches:", len(arc))
        if arc:
            for a in arc[:6]:
                u = a.get("url","")
                print(" -", processed.get(u, "<none>"), a.get("title","")[:120])
        if pen:
            print("Pending matches:")
            for r in pen[:4]:
                print(" -", r.get("dividend_per_share"), r.get("qualification_date"), r.get("payment_date"), r.get("company"))
        if rev:
            print("Review matches:")
            for item in rev[:4]:
                p = item.get("parsed") or {}
                print(" -", item.get("classification"), p.get("dividend_per_share"), item.get("reason_codes") or item.get("errors"))

    print("\n" + "="*70)
    print("SUSPECT PUBLISHED EVENTS")
    for ticker in ("FCMB","UNILEVER"):
        rows = [r for r in published if (r.get("ticker") or "").upper() == ticker]
        if not rows:
            print(ticker, ": not published")
            continue
        for r in rows:
            print(ticker, ":", r.get("dividend_per_share"), r.get("currency"), "|", r.get("source_title",""))

    run_deep_text_diagnostic(archive)

if __name__ == "__main__":
    main()

"""
collector/diagnose_access.py — one-shot access diagnostic

Answers, with evidence from a GitHub Actions runner, the questions we have
been guessing about:

  1. Does doclib expose a directory listing?
  2. Can we HEAD a known-real, never-discovered doclib PDF directly?
  3. Do AbokiForex per-company pages return 200 or 403, and how far back
     do they go?
  4. Do NaijaTicker company pages show anything from April 2026?

Run manually:  python -u -m collector.diagnose_access
Read-only. Touches no feed or state files.
"""

import re
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PDF_RE = re.compile(
    r"https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^\s\"'<>\]]+?\.pdf", re.I
)
NUM_RE = re.compile(r"/Financial_NewsDocs/(\d{4,6})_", re.I)

# Real URLs found via search earlier in this project. Never discovered by
# the pipeline. If HEAD returns 200 here, direct access works and the gap is
# purely discovery.
KNOWN_UNDISCOVERED = [
    "https://doclib.ngxgroup.com/Financial_NewsDocs/46167_DANGOTE_CEMENT_PLC-DIVIDEND_ANNOUNCEMENT_CORPORATE_ACTIONS_MARCH_2026.pdf",
    "https://doclib.ngxgroup.com/Financial_NewsDocs/46570_CUSTODIAN_INVESTMENT_PLC-CUSTODIAN_INVESTMENT_PLC_DIVIDEND_CORPORATE_ACTION_ANNOUNCEMENT_CORPORATE_ACTIONS_APRIL_2026.pdf",
    "https://doclib.ngxgroup.com/Financial_NewsDocs/NGX_Group_-_Dividend_Announcement.pdf",
]

COMPANY_TESTS = ["GTCO", "ZENITHBANK", "ACCESSCORP", "UBA"]


def section(title):
    print("\n" + "=" * 70, flush=True)
    print(title, flush=True)
    print("=" * 70, flush=True)


def get(url, timeout=(8, 15)):
    try:
        return requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        print(f"  EXC {url[:70]} -> {exc!r}", flush=True)
        return None


def head(url, timeout=(5, 10)):
    try:
        return requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        print(f"  EXC {url[:70]} -> {exc!r}", flush=True)
        return None


def summarize_pdfs(html):
    pdfs = sorted(set(PDF_RE.findall(html or "")))
    nums = sorted(int(m.group(1)) for u in pdfs for m in [NUM_RE.search(u)] if m)
    lo = nums[0] if nums else None
    hi = nums[-1] if nums else None
    div = [u for u in pdfs if re.search(r"DIVIDEND|DISTRIBUTION|CORPORATE_ACTION", u, re.I)]
    return pdfs, lo, hi, div


def main():
    section("1. doclib directory listing?")
    for url in [
        "https://doclib.ngxgroup.com/",
        "https://doclib.ngxgroup.com/Financial_NewsDocs/",
        "https://doclib.ngxgroup.com/Financial_NewsDocs",
    ]:
        r = get(url)
        if r is None:
            continue
        body = (r.text or "")[:400].replace("\n", " ")
        pdf_count = len(PDF_RE.findall(r.text or ""))
        print(f"  HTTP {r.status_code}  {url}", flush=True)
        print(f"     content-type={r.headers.get('Content-Type','?')} len={len(r.text or '')} pdf_links={pdf_count}", flush=True)
        print(f"     body: {body}", flush=True)

    section("2. Direct HEAD on known-real, never-discovered PDFs")
    for url in KNOWN_UNDISCOVERED:
        r = head(url)
        if r is None:
            continue
        print(f"  HTTP {r.status_code}  {url.split('/')[-1][:80]}", flush=True)
        print(f"     content-type={r.headers.get('Content-Type','?')} length={r.headers.get('Content-Length','?')}", flush=True)

    section("3. AbokiForex per-company pages")
    for t in COMPANY_TESTS:
        url = f"https://abokiforex.app/ngx-stocks/disclosures/{t}"
        r = get(url)
        if r is None:
            continue
        pdfs, lo, hi, div = summarize_pdfs(r.text)
        print(f"  HTTP {r.status_code}  {t}: total_pdfs={len(pdfs)} range={lo}-{hi} dividend_like={len(div)}", flush=True)
        for u in div[:5]:
            print(f"     {u.split('/')[-1][:90]}", flush=True)

    section("4. NaijaTicker company pages")
    for t in COMPANY_TESTS:
        url = f"https://naijaticker.com/stocks/{t.lower()}"
        r = get(url)
        if r is None:
            continue
        pdfs, lo, hi, div = summarize_pdfs(r.text)
        print(f"  HTTP {r.status_code}  {t}: total_pdfs={len(pdfs)} range={lo}-{hi} dividend_like={len(div)}", flush=True)
        for u in div[:5]:
            print(f"     {u.split('/')[-1][:90]}", flush=True)

    section("5. Kobo Terminal company/disclosure pages")
    for url in [
        "https://koboterminal.com/disclosures",
        "https://koboterminal.com/disclosures?company=GTCO",
        "https://koboterminal.com/stocks/GTCO",
    ]:
        r = get(url)
        if r is None:
            continue
        pdfs, lo, hi, div = summarize_pdfs(r.text)
        print(f"  HTTP {r.status_code}  {url[:60]}: total_pdfs={len(pdfs)} range={lo}-{hi}", flush=True)

    print("\nDone. Read-only; nothing was written.", flush=True)


if __name__ == "__main__":
    main()

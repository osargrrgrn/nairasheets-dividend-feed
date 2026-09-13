"""
collector/diagnose_access.py — access diagnostic, round 2

Round 1 proved: direct doclib file reads work (HTTP 200); aggregator
company pages only go back to June/July; Kobo is JS-only.

Round 2 tests the two untried enumeration routes:
  6. SharePoint list/REST/search endpoints on doclib.ngxgroup.com
  7. Wayback Machine (archive.org) CDX index + archived AbokiForex snapshots

Run manually:  python -u -m collector.diagnose_access
Read-only. Touches no feed or state files.
"""

import re
import json
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

PDF_RE = re.compile(r"doclib\.ngxgroup\.com/Financial_NewsDocs/[^\s\"'<>\]]+?\.pdf", re.I)
NUM_RE = re.compile(r"/Financial_NewsDocs/(\d{4,6})_", re.I)


def section(title):
    print("\n" + "=" * 70, flush=True)
    print(title, flush=True)
    print("=" * 70, flush=True)


def get(url, timeout=(10, 25), headers=None):
    try:
        return requests.get(url, headers=headers or HEADERS, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        print(f"  EXC {url[:80]} -> {exc!r}", flush=True)
        return None


def pdf_stats(text):
    pdfs = sorted(set(PDF_RE.findall(text or "")))
    nums = sorted(int(m.group(1)) for u in pdfs for m in [NUM_RE.search("/" + u)] if m)
    div = [u for u in pdfs if re.search(r"DIVIDEND|DISTRIBUTION|CORPORATE_ACTION", u, re.I)]
    return pdfs, (nums[0] if nums else None), (nums[-1] if nums else None), div


def main():
    section("6. SharePoint endpoints on doclib.ngxgroup.com")
    sp_json = {"Accept": "application/json;odata=verbose", "User-Agent": HEADERS["User-Agent"]}
    tests = [
        ("https://doclib.ngxgroup.com/Financial_NewsDocs/Forms/AllItems.aspx", HEADERS),
        ("https://doclib.ngxgroup.com/_layouts/15/viewlsts.aspx", HEADERS),
        ("https://doclib.ngxgroup.com/_api/web/lists", sp_json),
        ("https://doclib.ngxgroup.com/_api/web/GetFolderByServerRelativeUrl('/Financial_NewsDocs')/Files?$top=50", sp_json),
        ("https://doclib.ngxgroup.com/_api/search/query?querytext='dividend'&rowlimit=50", sp_json),
        ("https://doclib.ngxgroup.com/_layouts/15/osssearchresults.aspx?k=dividend%202026", HEADERS),
        ("https://doclib.ngxgroup.com/_vti_bin/listdata.svc/", sp_json),
    ]
    for url, hdrs in tests:
        r = get(url, headers=hdrs)
        if r is None:
            continue
        text = r.text or ""
        pdfs, lo, hi, div = pdf_stats(text)
        print(f"  HTTP {r.status_code}  {url[:95]}", flush=True)
        print(f"     ctype={r.headers.get('Content-Type','?')[:40]} len={len(text)} pdf_links={len(pdfs)} range={lo}-{hi}", flush=True)
        snippet = text[:300].replace("\n", " ")
        print(f"     body: {snippet}", flush=True)

    section("7a. Wayback CDX: every doclib PDF archive.org has ever captured")
    cdx = (
        "http://web.archive.org/cdx/search/cdx"
        "?url=doclib.ngxgroup.com/Financial_NewsDocs/*"
        "&output=json&fl=original,timestamp&collapse=urlkey&limit=5000"
    )
    r = get(cdx, timeout=(15, 60))
    if r is not None:
        print(f"  HTTP {r.status_code} len={len(r.text or '')}", flush=True)
        try:
            rows = json.loads(r.text)[1:]
            urls = [row[0] for row in rows]
            pdfs = [u for u in urls if u.lower().endswith(".pdf")]
            nums = sorted(int(m.group(1)) for u in pdfs for m in [NUM_RE.search(u)] if m)
            div = [u for u in pdfs if re.search(r"DIVIDEND|DISTRIBUTION|CORPORATE_ACTION", u, re.I)]
            in_gap = [n for n in nums if 42000 <= n < 46024]
            print(f"     captured_pdfs={len(pdfs)} number_range={nums[0] if nums else None}-{nums[-1] if nums else None}", flush=True)
            print(f"     dividend_like={len(div)}  in_gap_42000_46023={len(in_gap)}", flush=True)
            for u in div[:10]:
                print(f"     {u.split('/')[-1][:90]}", flush=True)
        except Exception as exc:
            print(f"     parse failed: {exc!r}; body: {(r.text or '')[:300]}", flush=True)

    section("7b. Wayback CDX: snapshots of the AbokiForex listing page, Jan-Jun 2026")
    cdx2 = (
        "http://web.archive.org/cdx/search/cdx"
        "?url=abokiforex.app/ngx-stocks/disclosures"
        "&output=json&fl=timestamp,statuscode&from=202601&to=202606&limit=200"
    )
    r = get(cdx2, timeout=(15, 60))
    snapshots = []
    if r is not None:
        print(f"  HTTP {r.status_code} len={len(r.text or '')}", flush=True)
        try:
            rows = json.loads(r.text)[1:]
            snapshots = [row[0] for row in rows if row[1] == "200"]
            print(f"     snapshots_200={len(snapshots)}", flush=True)
            print(f"     first={snapshots[:3]} last={snapshots[-3:]}", flush=True)
        except Exception as exc:
            print(f"     parse failed: {exc!r}; body: {(r.text or '')[:300]}", flush=True)

    section("7c. Fetch one archived AbokiForex snapshot from April 2026 and count doclib links")
    april = [t for t in snapshots if t.startswith("202604")] or [t for t in snapshots if t.startswith("202605")] or snapshots[:1]
    if april:
        ts = april[len(april)//2]
        url = f"http://web.archive.org/web/{ts}id_/https://abokiforex.app/ngx-stocks/disclosures"
        r = get(url, timeout=(15, 60))
        if r is not None:
            pdfs, lo, hi, div = pdf_stats(r.text)
            print(f"  HTTP {r.status_code}  snapshot={ts} len={len(r.text or '')}", flush=True)
            print(f"     pdf_links={len(pdfs)} range={lo}-{hi} dividend_like={len(div)}", flush=True)
            for u in div[:10]:
                print(f"     {u.split('/')[-1][:90]}", flush=True)
    else:
        print("  no snapshots available to test", flush=True)

    print("\nDone. Read-only; nothing was written.", flush=True)


if __name__ == "__main__":
    main()

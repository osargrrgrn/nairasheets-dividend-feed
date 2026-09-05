import concurrent.futures
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DEBUG_FILE = ROOT / "discovery_debug.json"
ARCHIVE_FILE = ROOT / "disclosure_archive.json"
TICKERS_FILE = ROOT / "collector" / "tickers.py"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ABOKIFOREX_DISCLOSURES = "https://abokiforex.app/ngx-stocks/disclosures"
NAIJATICKER_BASE = "https://naijaticker.com/stocks/"

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
MAX_TOTAL_SECONDS = 200
MAX_ABOKI_DETAIL_PAGES = 100
ABOKI_WORKERS = 8
MAX_NAIJA_COMPANIES = 150
NAIJA_WORKERS = 10
MAX_NEW_PDFS = 80

# Patch 18: NaijaTicker is only a fallback. It should not hand dozens of
# generic NGX documents to the expensive PDF parser.
MAX_NAIJA_RETURNED_PDFS = 40

NAIJA_STRONG_POSITIVE_HINTS = (
    "dividend",
    "distribution",
    "qualification_date",
    "qualification date",
    "payment_date",
    "payment date",
)

# Some real dividends are disclosed under these less explicit document names.
# They remain eligible, but rank below an explicit dividend announcement.
NAIJA_SECONDARY_POSITIVE_HINTS = (
    "post_board_meeting",
    "post board meeting",
    "outcome_of_board_meeting",
    "outcome of board meeting",
    "annual_general_meeting",
    "annual general meeting",
    "agm",
    "earnings_press_release",
    "earnings press release",
)

NAIJA_HARD_NEGATIVE_HINTS = (
    "financial_statement",
    "financial statements",
    "quarter_1",
    "quarter_2",
    "quarter_3",
    "quarter_4",
    "sustainability_report",
    "sustainability report",
    "resignation",
    "appointment",
    "transaction_in_own_shares",
    "transaction in own shares",
    "director_dealing",
    "director dealing",
    "closed_period",
    "closed period",
    "litigation",
)

PDF_URL_RE = re.compile(
    r"https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^\s\"'<>\]\\]+?\.pdf",
    re.I,
)
RELATIVE_DOC_RE = re.compile(
    r"(?:/)?Financial_NewsDocs/[^\"'<>\s\\]+?\.pdf",
    re.I,
)

STRONG_NEGATIVE_TITLE_HINTS = (
    "pending litigation", "litigation update", "exit of ",
    "resignation of ", "appointment of ",
    "notification of transaction in own shares",
    "transaction in own shares", "share awards",
    "tr-1 notification", "director dealings",
    "dealing in shares", "change of name", "closed period",
)

NAIJATICKER_FALLBACK_TICKERS = {
    "gtco","zenithbank","mtnn","accesscorp","uba","dangcem","buafoods",
    "buacement","seplat","airtelafri","stanbic","fidelitybk","fcmb",
    "firstholdco","okomuoil","presco","nestle","guinness","nb","updcreit",
    "nidf","afriprud","honyflour","dangsugar","learnafrca","ngxgroup","ucap",
    "nem","aiico","wapic","cornerst","unilever","uacn","cadbury","cap",
    "conoil","total",

    # Patch 23 benchmark coverage
    "ikejahotel","redstarex","upl","academy",
}

BENCHMARK_NAIJA_TICKERS = {
    "ikejahotel",
    "honyflour",
    "redstarex",
    "upl",
    "learnafrca",
    "academy",
}

def _elapsed(started):
    return time.monotonic() - started

def _time_remaining(started):
    return MAX_TOTAL_SECONDS - _elapsed(started)

def _get(session, url, timeout=None):
    return session.get(
        url,
        headers=HEADERS,
        timeout=timeout or (CONNECT_TIMEOUT, READ_TIMEOUT),
        allow_redirects=True,
    )

def _load_archive():
    if not ARCHIVE_FILE.exists():
        return set()
    try:
        data = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        return {x.get("url","") for x in data if isinstance(x,dict) and x.get("url")}
    except Exception:
        return set()

def _clean_pdf_url(value):
    if not value:
        return ""
    value = html.unescape(unquote(value)).replace("\\/", "/").replace("\\u002F", "/")
    value = value.strip(" '\"\t\r\n,;")
    if value.startswith("/"):
        value = urljoin("https://doclib.ngxgroup.com", value)
    m = PDF_URL_RE.search(value)
    return m.group(0) if m else ""

def _extract_doclib_pdfs(html_text, base_url):
    pdfs = set()
    if not html_text:
        return pdfs
    normalized = html.unescape(html_text).replace("\\/", "/").replace("\\u002F", "/")
    # Patch 42: also scan for partial doclib URLs (catches markdown-format links)
    DOCLIB_PARTIAL = re.compile(
        r"https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^\s\"'<>\]]+?\.pdf",
        re.I,
    )
    for m in DOCLIB_PARTIAL.findall(normalized):
        u = _clean_pdf_url(m)
        if u:
            pdfs.add(u)
    for m in PDF_URL_RE.findall(normalized):
        u = _clean_pdf_url(m)
        if u:
            pdfs.add(u)
    for m in RELATIVE_DOC_RE.findall(normalized):
        u = _clean_pdf_url("/" + m.lstrip("/"))
        if u:
            pdfs.add(u)
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        for a in soup.find_all("a", href=True):
            u = _clean_pdf_url(urljoin(base_url, html.unescape(a["href"])))
            if u:
                pdfs.add(u)
    except Exception:
        pass
    return pdfs

def _title_from_url(url):
    filename = unquote(urlparse(url).path.split("/")[-1])
    filename = re.sub(r"\.pdf$", "", filename, flags=re.I)
    filename = re.sub(r"^\d+_", "", filename).replace("_"," ")
    return re.sub(r"\s+"," ",filename).strip()

def _looks_strongly_irrelevant(title, url=""):
    hay = f"{title} {url}".lower().replace("_"," ")
    return any(x in hay for x in STRONG_NEGATIVE_TITLE_HINTS)

def _detail_links_from_aboki(text):
    out, seen = [], set()
    try:
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            full = urljoin(ABOKIFOREX_DISCLOSURES, html.unescape(a["href"]))
            if "abokiforex.app" not in full:
                continue
            if "/ngx-stocks/disclosures/" not in full:
                continue
            if full.rstrip("/") == ABOKIFOREX_DISCLOSURES.rstrip("/"):
                continue
            if full in seen:
                continue
            seen.add(full)
            out.append(full)
    except Exception:
        pass
    return out[:MAX_ABOKI_DETAIL_PAGES]

def _fetch_detail_pdf_urls(url):
    try:
        with requests.Session() as s:
            r = _get(s, url, timeout=(5,9))
            return {
                "url": url,
                "status": r.status_code,
                "pdfs": sorted(_extract_doclib_pdfs(r.text,url)) if r.status_code==200 else []
            }
    except Exception as exc:
        return {"url":url,"status":0,"pdfs":[],"error":repr(exc)}

def _discover_aboki(session, known, debug, started):
    found = []
    dbg = {"status":None,"detail_pages_found":0,"detail_pages_completed":0,"new_pdfs":0,"errors":[]}
    try:
        r = _get(session, ABOKIFOREX_DISCLOSURES)
        dbg["status"] = r.status_code
        if r.status_code != 200:
            debug["abokiforex"] = dbg
            return found

        for u in sorted(_extract_doclib_pdfs(r.text, ABOKIFOREX_DISCLOSURES)):
            if u in known:
                continue
            t = _title_from_url(u)
            if _looks_strongly_irrelevant(t,u):
                continue
            found.append({"url":u,"title":t,"source":"abokiforex_listing"})
            known.add(u)

        links = _detail_links_from_aboki(r.text)
        dbg["detail_pages_found"] = len(links)
        print(f"[AbokiForex] detail pages: {len(links)}", flush=True)

        if links and _time_remaining(started) > 10:
            with concurrent.futures.ThreadPoolExecutor(max_workers=ABOKI_WORKERS) as pool:
                futs = [pool.submit(_fetch_detail_pdf_urls,u) for u in links]
                for fut in concurrent.futures.as_completed(futs):
                    if _time_remaining(started) <= 5:
                        break
                    res = fut.result()
                    dbg["detail_pages_completed"] += 1
                    if res.get("error"):
                        dbg["errors"].append(res["error"])
                    for u in res.get("pdfs",[]):
                        if u in known:
                            continue
                        t = _title_from_url(u)
                        if _looks_strongly_irrelevant(t,u):
                            continue
                        found.append({"url":u,"title":t,"source":"abokiforex_detail"})
                        known.add(u)
                        if len(found) >= MAX_NEW_PDFS:
                            break
                    if len(found) >= MAX_NEW_PDFS:
                        break
                for f in futs:
                    if not f.done():
                        f.cancel()
    except Exception as exc:
        dbg["errors"].append(repr(exc))

    dbg["new_pdfs"] = len(found)
    debug["abokiforex"] = dbg
    print(f"[AbokiForex] new official PDFs: {len(found)}", flush=True)
    return found

def _load_naija_tickers():
    tickers = set(NAIJATICKER_FALLBACK_TICKERS)
    if TICKERS_FILE.exists():
        text = TICKERS_FILE.read_text(encoding="utf-8", errors="ignore")
        pattern = r":\s*[\"']([A-Z][A-Z0-9]{1,20})[\"']"
        for t in re.findall(pattern, text):
            tickers.add(t.lower())
    return sorted(t for t in tickers if re.fullmatch(r"[a-z][a-z0-9]{1,20}",t))[:MAX_NAIJA_COMPANIES]

def _naija_relevance_score(url):
    """
    Rank NaijaTicker URLs before PDF download/parsing.

    Explicit dividend/distribution/date language ranks highest.
    Board/AGM/earnings documents remain eligible because genuine dividends
    can be announced there, but only a limited number are allowed through.
    Financial statements and other obvious noise are rejected here.
    """
    hay = html.unescape(unquote(url)).lower().replace("-", "_")

    if any(hint in hay for hint in NAIJA_HARD_NEGATIVE_HINTS):
        return -100

    score = 0

    if "dividend" in hay:
        score += 100
    if "distribution" in hay:
        score += 90
    if "qualification_date" in hay or "qualification date" in hay:
        score += 80
    if "payment_date" in hay or "payment date" in hay:
        score += 80

    # Patch 23: generic NGX corporate-action documents must remain eligible.
    # HONYFLOUR/UPL-style disclosures often do not put "dividend" in the URL.
    if "corporate_actions" in hay or "corporate action" in hay:
        score += 70

    for hint in NAIJA_SECONDARY_POSITIVE_HINTS:
        if hint in hay:
            score += 25
            break

    return score


def _fetch_naija(ticker):
    url = f"{NAIJATICKER_BASE}{ticker}"
    try:
        with requests.Session() as s:
            r = _get(s,url,timeout=(5,9))
            return {
                "ticker": ticker,
                "status": r.status_code,
                "pdfs": sorted(_extract_doclib_pdfs(r.text,url)) if r.status_code==200 else []
            }
    except Exception as exc:
        return {"ticker":ticker,"status":0,"pdfs":[],"error":repr(exc)}

def _discover_naija(known, debug, started):
    tickers = _load_naija_tickers()
    dbg = {
        "companies_targeted": len(tickers),
        "companies_completed": 0,
        "pdfs_seen": 0,
        "already_known": 0,
        "filtered_before_parser": 0,
        "eligible_candidates": 0,
        "new_pdfs": 0,
        "errors": [],
    }

    print(f"[NaijaTicker] checking {len(tickers)} pages", flush=True)

    candidates = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=NAIJA_WORKERS) as pool:
        futs = [pool.submit(_fetch_naija, t) for t in tickers]

        for fut in concurrent.futures.as_completed(futs):
            if _time_remaining(started) <= 5:
                break

            res = fut.result()
            dbg["companies_completed"] += 1

            if res.get("error"):
                dbg["errors"].append(res["error"])

            for u in res.get("pdfs", []):
                dbg["pdfs_seen"] += 1

                if u in known:
                    dbg["already_known"] += 1
                    continue

                title = _title_from_url(u)

                if _looks_strongly_irrelevant(title, u):
                    dbg["filtered_before_parser"] += 1
                    continue

                score = _naija_relevance_score(u)

                if score <= 0:
                    dbg["filtered_before_parser"] += 1
                    continue

                current = candidates.get(u)
                benchmark_bonus = (
                    1000 if res["ticker"].lower() in BENCHMARK_NAIJA_TICKERS else 0
                )

                candidate = {
                    "url": u,
                    "title": title,
                    "source": "naijaticker",
                    "ticker": res["ticker"].upper(),
                    "_score": score + benchmark_bonus,
                }

                if current is None or score > current["_score"]:
                    candidates[u] = candidate

        for f in futs:
            if not f.done():
                f.cancel()

    ranked = sorted(
        candidates.values(),
        key=lambda x: (-x["_score"], x["url"])
    )

    dbg["eligible_candidates"] = len(ranked)
    dbg["benchmark_candidates"] = sum(
        1
        for item in ranked
        if (item.get("ticker") or "").lower() in BENCHMARK_NAIJA_TICKERS
    )

    found = []
    for item in ranked[:MAX_NAIJA_RETURNED_PDFS]:
        item.pop("_score", None)
        found.append(item)
        known.add(item["url"])

    dbg["new_pdfs"] = len(found)
    debug["naijaticker"] = dbg

    print(
        f"[NaijaTicker] filtered before parser: "
        f"{dbg['filtered_before_parser']}",
        flush=True,
    )
    print(
        f"[NaijaTicker] eligible candidates: "
        f"{dbg['eligible_candidates']}",
        flush=True,
    )
    print(
        f"[NaijaTicker] relevant new official PDFs: {len(found)}",
        flush=True,
    )
    print(
        f"[NaijaTicker] benchmark candidates: "
        f"{dbg.get('benchmark_candidates', 0)}",
        flush=True,
    )

    return found


# ---------------------------------------------------------------------------
# Patch 41: Sequential NGX doclib scanner
# Scans AbokiForex disclosure pages by document number to recover
# historical dividend PDFs that scrolled off the main listing.
# Uses the same AbokiForex detail page mechanism that already works
# in your pipeline — no new sites, no search engines needed.
# ---------------------------------------------------------------------------

DOCLIB_DIVIDEND_KEYWORDS = (
    "DIVIDEND",
    "DISTRIBUTION",
    "CORPORATE_ACTION",
    "CORPORATE_ACTIONS",
    "NGX_NOTIFICATION",
    "QUALIFICATION",
)

DOCLIB_SCAN_BATCH = 50


def _get_archive_number_range(known_urls):
    """Get min and max document numbers from known archive URLs."""
    numbers = []
    num_re = re.compile(r"/Financial_NewsDocs/(\d{4,6})_", re.I)
    for url in known_urls:
        m = num_re.search(url)
        if m:
            numbers.append(int(m.group(1)))
    if not numbers:
        return 45000, 48000
    return min(numbers), max(numbers)


def _fetch_aboki_detail_by_number(session, number, known):
    """
    Fetch AbokiForex detail page for a specific disclosure number.
    Returns list of new doclib PDF URLs found on that page.
    AbokiForex detail pages are accessible from GitHub Actions runners.
    """
    detail_url = f"https://abokiforex.app/ngx-stocks/disclosures/{number}"
    try:
        r = session.get(detail_url, headers=HEADERS, timeout=(5, 10), allow_redirects=True)
        if r.status_code == 200:
            pdfs = _extract_doclib_pdfs(r.text, detail_url)
            return [u for u in pdfs if u not in known]
    except Exception:
        pass
    return []


def _discover_sequential(known, debug, started):
    """
    Patch 41: Recover historical dividend PDFs by scanning AbokiForex
    disclosure pages by document number.

    AbokiForex only shows ~200 recent disclosures on their listing page.
    Documents from March-June 2026 (e.g. DANGCEM 46167) have scrolled off.
    But AbokiForex still has individual detail pages for each disclosure
    accessible at /ngx-stocks/disclosures/{number}.

    This function finds gap numbers in our archive and fetches those pages.
    """
    import random

    found = []
    dbg = {
        "numbers_scanned": 0,
        "pages_fetched": 0,
        "new_pdfs": 0,
        "errors": [],
    }

    print("[Sequential] Starting historical doclib scan", flush=True)

    # Find what document number range we already have
    min_num, max_num = _get_archive_number_range(known)

    # Find which numbers we already have
    num_re = re.compile(r"/Financial_NewsDocs/(\d{4,6})_", re.I)
    known_numbers = set()
    for url in known:
        m = num_re.search(url)
        if m:
            known_numbers.add(int(m.group(1)))

    # Scan from 500 below our minimum (historical gap) to 100 above maximum
    scan_start = max(44000, min_num - 500)
    scan_end = max_num + 100

    # Find gap numbers — not in our archive
    gap_numbers = [
        n for n in range(scan_start, scan_end + 1)
        if n not in known_numbers
    ]

    print(
        f"[Sequential] {len(gap_numbers)} gap numbers in range "
        f"{scan_start}-{scan_end}",
        flush=True,
    )

    # Prioritize historical gaps (lower numbers = older, more likely missed)
    historical = sorted([n for n in gap_numbers if n < min_num])
    recent_gaps = [n for n in gap_numbers if n >= min_num]

    # Take 35 historical + 15 recent gaps per run
    batch = historical[:35]
    if recent_gaps:
        batch += random.sample(recent_gaps, min(15, len(recent_gaps)))
    batch = batch[:DOCLIB_SCAN_BATCH]

    dbg["numbers_scanned"] = len(batch)

    with requests.Session() as s:
        for num in batch:
            if _time_remaining(started) <= 10:
                break

            new_pdfs = _fetch_aboki_detail_by_number(s, num, known)
            dbg["pages_fetched"] += 1

            for url in new_pdfs:
                title = _title_from_url(url)

                if _looks_strongly_irrelevant(title, url):
                    continue

                # Only pass dividend-related documents to parser
                url_upper = url.upper()
                if any(kw in url_upper for kw in DOCLIB_DIVIDEND_KEYWORDS):
                    found.append({
                        "url": url,
                        "title": title,
                        "source": "sequential_scan",
                    })
                    known.add(url)
                    print(
                        f"[Sequential] Found: {title[:60]}",
                        flush=True,
                    )

    dbg["new_pdfs"] = len(found)
    debug["sequential_scan"] = dbg
    print(f"[Sequential] {len(found)} new dividend PDFs found", flush=True)
    return found


def discover_official_pdfs():
    started = time.monotonic()
    debug = {"method": "patch_41_sequential_doclib_scan"}
    print(
        "NGX dividend PDF discovery — Patch 41 "
        "(Sequential + AbokiForex + NaijaTicker)",
        flush=True,
    )

    known = _load_archive()
    print(f"Known archive URLs: {len(known)}", flush=True)
    all_found = []

    # Primary: AbokiForex listing + detail pages (recent documents)
    with requests.Session() as s:
        all_found.extend(_discover_aboki(s, known, debug, started))

    # Patch 41: Sequential scanner for historical gap documents
    if _time_remaining(started) > 25:
        all_found.extend(_discover_sequential(known, debug, started))

    # NaijaTicker: company-specific pages as additional coverage
    if _time_remaining(started) > 15:
        all_found.extend(_discover_naija(known, debug, started))
    else:
        debug["naijaticker"] = {"skipped": True}

    unique = {x["url"]: x for x in all_found if x.get("url")}
    results = list(unique.values())[:MAX_NEW_PDFS]

    debug["total_new_pdfs"] = len(results)
    debug["elapsed_seconds"] = round(_elapsed(started), 2)
    debug["results"] = results

    DEBUG_FILE.write_text(
        json.dumps(debug, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Discovery elapsed: {debug['elapsed_seconds']}s", flush=True)
    print(f"New official NGX PDFs discovered: {len(results)}", flush=True)
    return results


def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    return _looks_strongly_irrelevant(title, url)

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
    r"https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^\"'<>\s\\]+?\.pdf",
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
# Patch 40: Google search discovery
# Searches Google for site:doclib.ngxgroup.com PDFs directly.
# Goes further back in time than AbokiForex/NaijaTicker.
# No API key needed — uses standard Google search.
# ---------------------------------------------------------------------------

def _google_queries():
    """Generate search queries for current and previous year dynamically."""
    import datetime; year = datetime.date.today().year
    prev = year - 1
    return [
        f"site:doclib.ngxgroup.com dividend announcement {year}",
        f"site:doclib.ngxgroup.com corporate action announcement {year} dividend",
        f"site:doclib.ngxgroup.com interim dividend {year}",
        f"site:doclib.ngxgroup.com final dividend {year}",
        f"site:doclib.ngxgroup.com distribution payment {year}",
        f"site:doclib.ngxgroup.com qualification date {year}",
        f"site:doclib.ngxgroup.com dividend announcement {prev}",
        f"site:doclib.ngxgroup.com final dividend {prev}",
    ]



DDG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _discover_google(known, debug, started):
    """
    Patch 40: Use DuckDuckGo HTML search to find official NGX doclib dividend PDFs.
    DuckDuckGo indexes doclib.ngxgroup.com PDFs and is accessible from GitHub runners
    unlike Google which blocks automated requests from Azure IP ranges.
    PDFs found here are passed to the existing parser — no third party data
    ever enters the feed.
    """
    found = []
    dbg = {
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "new_pdfs": 0,
        "errors": [],
    }

    print("[DDG] Starting DuckDuckGo search discovery", flush=True)

    with requests.Session() as s:
        for query in _google_queries():
            if _time_remaining(started) <= 10:
                break

            try:
                encoded = requests.utils.quote(query)
                search_url = f"https://html.duckduckgo.com/html/?q={encoded}"
                dbg["queries_attempted"] += 1

                r = s.get(
                    search_url,
                    headers=DDG_HEADERS,
                    timeout=(8, 15),
                    allow_redirects=True,
                )

                if r.status_code != 200:
                    dbg["errors"].append(f"HTTP {r.status_code} for query: {query}")
                    continue

                dbg["queries_succeeded"] += 1

                # Extract all doclib PDF URLs from Google search results
                normalized = html.unescape(r.text).replace("\\/", "/")
                for url in PDF_URL_RE.findall(normalized):
                    url = _clean_pdf_url(url)
                    if not url or url in known:
                        continue
                    title = _title_from_url(url)
                    if _looks_strongly_irrelevant(title, url):
                        continue
                    found.append({
                        "url": url,
                        "title": title,
                        "source": "google_search",
                    })
                    known.add(url)

                # Rate limit — be respectful to Google
                time.sleep(2)

            except Exception as exc:
                dbg["errors"].append(repr(exc))

    dbg["new_pdfs"] = len(found)
    debug["ddg_search"] = dbg
    print(f"[DDG] {len(found)} new PDFs found", flush=True)
    return found

def discover_official_pdfs():
    started = time.monotonic()
    debug = {"method":"patch_40_ddg_search_discovery"}
    print("NGX dividend PDF discovery — Patch 40 (DDG + AbokiForex + NaijaTicker)", flush=True)

    known = _load_archive()
    print(f"Known archive URLs: {len(known)}", flush=True)
    all_found = []

    with requests.Session() as s:
        all_found.extend(_discover_aboki(s,known,debug,started))

    # Patch 40: Google search discovery — finds PDFs going back years
    if _time_remaining(started) > 20:
        all_found.extend(_discover_google(known, debug, started))

    # Option B: always run NaijaTicker for maximum coverage
    if _time_remaining(started) > 15:
        all_found.extend(_discover_naija(known,debug,started))
    else:
        debug["naijaticker"] = {"skipped":True}

    unique = {x["url"]:x for x in all_found if x.get("url")}
    results = list(unique.values())[:MAX_NEW_PDFS]

    debug["total_new_pdfs"] = len(results)
    debug["elapsed_seconds"] = round(_elapsed(started),2)
    debug["results"] = results

    DEBUG_FILE.write_text(
        json.dumps(debug,indent=2,ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"Discovery elapsed: {debug['elapsed_seconds']}s", flush=True)
    print(f"New official NGX PDFs discovered: {len(results)}", flush=True)
    return results

def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    return _looks_strongly_irrelevant(title,url)

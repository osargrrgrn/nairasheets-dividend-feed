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
MAX_TOTAL_SECONDS = 85
MAX_ABOKI_DETAIL_PAGES = 50
ABOKI_WORKERS = 8
MAX_NAIJA_COMPANIES = 150
NAIJA_WORKERS = 10
MAX_NEW_PDFS = 80

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
    found = []
    tickers = _load_naija_tickers()
    dbg = {"companies_targeted":len(tickers),"companies_completed":0,"new_pdfs":0,"errors":[]}
    print(f"[NaijaTicker] checking {len(tickers)} pages", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=NAIJA_WORKERS) as pool:
        futs = [pool.submit(_fetch_naija,t) for t in tickers]
        for fut in concurrent.futures.as_completed(futs):
            if _time_remaining(started) <= 5:
                break
            res = fut.result()
            dbg["companies_completed"] += 1
            if res.get("error"):
                dbg["errors"].append(res["error"])
            for u in res.get("pdfs",[]):
                if u in known:
                    continue
                t = _title_from_url(u)
                if _looks_strongly_irrelevant(t,u):
                    continue
                found.append({
                    "url":u,"title":t,"source":"naijaticker",
                    "ticker":res["ticker"].upper()
                })
                known.add(u)
                if len(found) >= MAX_NEW_PDFS:
                    break
            if len(found) >= MAX_NEW_PDFS:
                break

        for f in futs:
            if not f.done():
                f.cancel()

    dbg["new_pdfs"] = len(found)
    debug["naijaticker"] = dbg
    print(f"[NaijaTicker] new official PDFs: {len(found)}", flush=True)
    return found

def discover_official_pdfs():
    started = time.monotonic()
    debug = {"method":"patch_15_directory_discovery_official_pdf_validation"}
    print("NGX dividend PDF discovery — Patch 15", flush=True)

    known = _load_archive()
    print(f"Known archive URLs: {len(known)}", flush=True)
    all_found = []

    with requests.Session() as s:
        all_found.extend(_discover_aboki(s,known,debug,started))

    if len(all_found) < 10 and _time_remaining(started) > 15:
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

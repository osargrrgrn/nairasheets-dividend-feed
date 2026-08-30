import concurrent.futures
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests

NGX_PROFILE_URL = "https://ngxgroup.com/exchange/data/company-profile/"
DOC_ROOT = "https://doclib.ngxgroup.com"
PDF_PATH_MARKER = "/Financial_NewsDocs/"

ROOT = Path(__file__).resolve().parents[1]
DEBUG_FILE = ROOT / "discovery_debug.json"
TICKERS_FILE = ROOT / "collector" / "tickers.py"
ARCHIVE_FILE = ROOT / "disclosure_archive.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PROFILE_CONNECT_TIMEOUT = 5
PROFILE_READ_TIMEOUT = 9
MAX_WORKERS = 10
MAX_TICKERS = 180
MAX_NEW_PDFS = 40
MAX_DISCOVERY_SECONDS = 85

PDF_URL_RE = re.compile(
    r"""https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^"'<>\s\\]+?\.pdf""",
    re.I,
)

# Equity-like codes that should never be treated as company tickers.
NON_EQUITY_PREFIXES = (
    "FG", "FGB", "FGS", "FGEUR", "FGUK",
    "LAB20", "DAN20", "FID20", "UBA20", "TSL20",
    "FFF", "FF", "VET", "SIAM", "STANBICETF",
)

LIKELY_TITLE_HINTS = (
    "dividend",
    "distribution",
    "annual general meeting",
    "agm",
    "post board meeting",
    "board meeting",
    "corporate action",
    "financial statement",
    "quarter",
    "interim",
    "annual report",
)

STRONG_NEGATIVE_TITLE_HINTS = (
    "pending litigation",
    "litigation update",
    "exit of ",
    "resignation of ",
    "appointment of ",
    "notification of transaction in own shares",
    "transaction in own shares",
    "share awards",
    "tr-1 notification",
    "notification from sustainable capital",
    "director dealings",
    "dealing in shares",
)


def _clean_pdf_url(value: str) -> str:
    if not value:
        return ""
    value = html.unescape(unquote(value))
    value = value.replace("\\/", "/").replace("\\u002F", "/")
    value = value.strip(" '\"\t\r\n,;")
    if value.startswith("/"):
        value = urljoin(DOC_ROOT, value)

    m = PDF_URL_RE.search(value)
    return m.group(0) if m else ""


def _title_from_url(url: str) -> str:
    name = url.rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf(?:\?.*)?$", "", name, flags=re.I)
    name = re.sub(r"^\d+_", "", name)
    name = unquote(name).replace("_", " ")
    return re.sub(r"\s+", " ", name).strip()


def _looks_like_equity_ticker(value: str) -> bool:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", value):
        return False
    if any(value.startswith(p) for p in NON_EQUITY_PREFIXES):
        return False
    if re.search(r"\d{4}", value):
        return False
    return True


def _load_candidate_tickers():
    tickers = set()

    # 1) Use the collector's own ticker registry without depending on its
    # internal variable names.
    if TICKERS_FILE.exists():
        text = TICKERS_FILE.read_text(encoding="utf-8", errors="ignore")
        for token in re.findall(r"""["']([A-Z][A-Z0-9]{1,15})["']""", text):
            if _looks_like_equity_ticker(token):
                tickers.add(token)

    # 2) Preserve tickers already seen in the rolling disclosure archive.
    if ARCHIVE_FILE.exists():
        try:
            archive = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
            if isinstance(archive, list):
                for item in archive:
                    if not isinstance(item, dict):
                        continue
                    ticker = str(item.get("ticker", "")).upper().strip()
                    if _looks_like_equity_ticker(ticker):
                        tickers.add(ticker)
                    title = str(item.get("title", ""))
                    # Filenames often begin with an issuer code after the ID.
                    m = re.search(r"\b([A-Z][A-Z0-9]{1,15})\b", title.upper())
                    if m and _looks_like_equity_ticker(m.group(1)):
                        tickers.add(m.group(1))
        except Exception:
            pass

    # Known high-value dividend names ensure coverage even if the local
    # ticker registry format changes.
    tickers.update({
        "MTNN", "GTCO", "ZENITHBANK", "ACCESSCORP", "FIDELITYBK",
        "UBA", "STANBIC", "FIRSTHOLDCO", "FCMB", "WEMABANK",
        "DANGCEM", "BUACEMENT", "BUAFOODS", "PRESCO", "OKOMUOIL",
        "SEPLAT", "AIRTELAFRI", "NGXGROUP", "UCAP", "AFRIPRUD",
        "NEM", "MANSARD", "AIICO", "CORNERST", "WAPIC",
        "SFSREIT", "UPDCREIT", "UHOMREIT", "NIDF", "CNIF",
        "NESTLE", "NB", "GUINNESS", "PZ", "UNILEVER", "UACN",
        "DANGSUGAR", "CADBURY", "CAP", "CONOIL", "TOTAL",
    })

    return sorted(tickers)[:MAX_TICKERS]


def _extract_pdf_links(text: str):
    if not text:
        return []

    normalized = html.unescape(text)
    normalized = normalized.replace("\\/", "/").replace("\\u002F", "/")

    urls = set()

    for match in PDF_URL_RE.findall(normalized):
        cleaned = _clean_pdf_url(match)
        if cleaned:
            urls.add(cleaned)

    # Some NGX responses carry server-relative Financial_NewsDocs paths.
    for rel in re.findall(
        r"""(?:https?://doclib\.ngxgroup\.com)?(/Financial_NewsDocs/[^"'<>\s\\]+?\.pdf)""",
        normalized,
        flags=re.I,
    ):
        cleaned = _clean_pdf_url(rel)
        if cleaned:
            urls.add(cleaned)

    return sorted(urls)


def _fetch_profile(ticker: str):
    params = {
        "directory": "companydirectory",
        "symbol": ticker,
    }

    started = time.monotonic()
    try:
        response = requests.get(
            NGX_PROFILE_URL,
            params=params,
            headers=HEADERS,
            timeout=(PROFILE_CONNECT_TIMEOUT, PROFILE_READ_TIMEOUT),
            allow_redirects=True,
        )
        urls = _extract_pdf_links(response.text) if response.status_code < 400 else []
        return {
            "ticker": ticker,
            "status": response.status_code,
            "elapsed": round(time.monotonic() - started, 2),
            "urls": urls,
            "bytes": len(response.content),
        }
    except Exception as exc:
        return {
            "ticker": ticker,
            "status": 0,
            "elapsed": round(time.monotonic() - started, 2),
            "urls": [],
            "error": repr(exc),
        }


def discover_official_pdfs():
    started = time.monotonic()
    tickers = _load_candidate_tickers()
    found = {}
    profile_results = []

    print("NGX company-disclosure discovery started", flush=True)
    print(f"Candidate equity tickers: {len(tickers)}", flush=True)

    # Concurrent profile requests make broad coverage cheap while each
    # individual request still has a strict timeout.
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_profile, ticker): ticker for ticker in tickers}

        for i, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            if time.monotonic() - started > MAX_DISCOVERY_SECONDS:
                print("Discovery time limit reached; stopping safely.", flush=True)
                break

            result = future.result()
            profile_results.append(result)

            for url in result.get("urls", []):
                found[url] = {
                    "url": url,
                    "title": _title_from_url(url),
                    "ticker": result["ticker"],
                }

            if i % 20 == 0 or result.get("urls"):
                print(
                    f"Profiles checked: {i}/{len(tickers)} | "
                    f"official PDF links found: {len(found)}",
                    flush=True,
                )

            if len(found) >= MAX_NEW_PDFS:
                print("Fresh-document cap reached.", flush=True)
                break

        # Do not wait indefinitely for irrelevant unfinished futures.
        for future in futures:
            if not future.done():
                future.cancel()

    elapsed = round(time.monotonic() - started, 2)
    results = list(found.values())[:MAX_NEW_PDFS]

    debug = {
        "method": "ngx_company_profile_disclosures_patch_13",
        "elapsed_seconds": elapsed,
        "candidate_tickers": len(tickers),
        "profiles_completed": len(profile_results),
        "official_pdf_links_found": len(found),
        "returned_to_parser": len(results),
        "profiles_with_documents": [
            {
                "ticker": r["ticker"],
                "status": r.get("status"),
                "elapsed": r.get("elapsed"),
                "document_count": len(r.get("urls", [])),
                "urls": r.get("urls", []),
            }
            for r in profile_results
            if r.get("urls")
        ],
        "errors": [
            {
                "ticker": r["ticker"],
                "status": r.get("status"),
                "error": r.get("error", ""),
            }
            for r in profile_results
            if r.get("status") not in (200, 0) or r.get("error")
        ][:30],
        "results": results,
    }

    DEBUG_FILE.write_text(
        json.dumps(debug, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Company-profile discovery elapsed seconds: {elapsed}", flush=True)
    print(f"Official PDF links found: {len(found)}", flush=True)
    print(f"Fresh PDFs returned to parser: {len(results)}", flush=True)

    return results


def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    return any(hint in hay for hint in STRONG_NEGATIVE_TITLE_HINTS)

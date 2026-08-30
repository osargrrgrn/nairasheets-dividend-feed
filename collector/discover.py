import html
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

DOC_ROOT = "https://doclib.ngxgroup.com"
NEWS_FOLDER = "/Financial_NewsDocs"
OFFICIAL_DOC_HOST = "doclib.ngxgroup.com"
PDF_PATH_MARKER = "/Financial_NewsDocs/"

ROOT = Path(__file__).resolve().parents[1]
DEBUG_FILE = ROOT / "discovery_debug.json"

SEARCH_TERMS = (
    "dividend",
    "distribution",
    '"annual general meeting"',
    "agm",
    '"post board meeting"',
    '"board meeting notification"',
)

LIKELY_TITLE_HINTS = (
    "dividend",
    "distribution",
    "annual general meeting",
    "agm",
    "post board meeting",
    "board meeting notification",
    "corporate action",
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

PDF_URL_RE = re.compile(
    r"""https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^"'<>\s]+?\.pdf""",
    re.I,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json;odata=verbose, application/json;q=0.9, */*;q=0.8",
}

REQUEST_CONNECT_TIMEOUT = 6
REQUEST_READ_TIMEOUT = 12
MAX_RESULTS_PER_TERM = 40
MAX_RETURNED_PDFS = 24
MAX_DISCOVERY_SECONDS = 75


def _clean_url(url: str) -> str:
    if not url:
        return ""
    url = html.unescape(url).replace("\\/", "/").strip(" '\"\t\r\n")
    if url.startswith("/"):
        url = urljoin(DOC_ROOT, url)
    m = re.search(
        r"(https?://doclib\.ngxgroup\.com/Financial_NewsDocs/.+?\.pdf)",
        url,
        re.I,
    )
    return m.group(1) if m else url


def _title_from_url(url: str) -> str:
    name = url.rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf(?:\?.*)?$", "", name, flags=re.I)
    name = re.sub(r"^\d+_", "", name)
    name = name.replace("%20", " ")
    name = re.sub(r"_+", " ", name)
    return name.strip()


def _looks_likely(title: str, url: str) -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    if any(bad in hay for bad in STRONG_NEGATIVE_TITLE_HINTS):
        return False
    return any(hint in hay for hint in LIKELY_TITLE_HINTS)


def _add(found, url, title=""):
    url = _clean_url(url)
    if OFFICIAL_DOC_HOST not in url.lower():
        return
    if PDF_PATH_MARKER.lower() not in url.lower():
        return
    if not url.lower().endswith(".pdf"):
        return
    title = (title or _title_from_url(url)).strip()
    if not _looks_likely(title, url):
        return
    found[url] = {"url": url, "title": title}


def _extract_rows(payload, found):
    if isinstance(payload, dict):
        cells = payload.get("Cells")
        if isinstance(cells, dict):
            cells = cells.get("results", cells)

        if isinstance(cells, list):
            props = {}
            for cell in cells:
                if isinstance(cell, dict):
                    key = str(cell.get("Key", "")).strip()
                    value = cell.get("Value")
                    if key and value is not None:
                        props[key] = value
            path = props.get("Path") or props.get("ServerRelativeUrl")
            title = props.get("Title") or ""
            if isinstance(path, str):
                _add(found, path, str(title))

        key = str(payload.get("Key", "")).lower()
        value = payload.get("Value")
        if key in {"path", "serverrelativeurl", "url"} and isinstance(value, str):
            _add(found, value)

        for value in payload.values():
            _extract_rows(value, found)

    elif isinstance(payload, list):
        for value in payload:
            _extract_rows(value, found)

    elif isinstance(payload, str):
        text = html.unescape(payload).replace("\\/", "/")
        for match in PDF_URL_RE.findall(text):
            _add(found, match)


def _search_term(session, term, found, debug, started):
    if time.monotonic() - started > MAX_DISCOVERY_SECONDS:
        return

    endpoint = f"{DOC_ROOT}/_api/search/query"
    query_text = (
        f'{term} '
        f'Path:"{DOC_ROOT}{NEWS_FOLDER}" '
        'FileExtension:pdf '
        'LastModifiedTime>=2026-01-01'
    )

    params = {
        "querytext": f"'{query_text}'",
        "rowlimit": str(MAX_RESULTS_PER_TERM),
        "startrow": "0",
        "trimduplicates": "false",
        "selectproperties": "'Title,Path,FileExtension,LastModifiedTime'",
    }

    attempt = {"term": term, "url": endpoint, "before": len(found)}
    print(f"NGX discovery search: {term}", flush=True)

    try:
        response = session.get(
            endpoint,
            params=params,
            headers=HEADERS,
            timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT),
            allow_redirects=True,
        )
        attempt["status"] = response.status_code
        attempt["bytes"] = len(response.content)
        attempt["content_type"] = response.headers.get("content-type", "")

        if response.status_code < 400:
            try:
                _extract_rows(response.json(), found)
            except Exception:
                text = html.unescape(response.text).replace("\\/", "/")
                for match in PDF_URL_RE.findall(text):
                    _add(found, match)

    except Exception as exc:
        attempt["error"] = repr(exc)

    attempt["after"] = len(found)
    attempt["added"] = attempt["after"] - attempt["before"]
    debug["searches"].append(attempt)

    print(
        f"  +{attempt['added']} likely PDFs "
        f"(running total {attempt['after']})",
        flush=True,
    )


def discover_official_pdfs():
    started = time.monotonic()
    found = {}
    debug = {
        "method": "focused_direct_ngx_search_patch_12",
        "searches": [],
        "limits": {
            "max_results_per_term": MAX_RESULTS_PER_TERM,
            "max_returned_pdfs": MAX_RETURNED_PDFS,
            "max_discovery_seconds": MAX_DISCOVERY_SECONDS,
        },
    }

    print("Focused NGX dividend discovery started", flush=True)

    with requests.Session() as session:
        for term in SEARCH_TERMS:
            if len(found) >= MAX_RETURNED_PDFS:
                break
            if time.monotonic() - started > MAX_DISCOVERY_SECONDS:
                print("Discovery time limit reached; stopping safely.", flush=True)
                break
            _search_term(session, term, found, debug, started)

    results = list(found.values())[:MAX_RETURNED_PDFS]
    elapsed = round(time.monotonic() - started, 2)

    debug["elapsed_seconds"] = elapsed
    debug["likely_pdfs_found"] = len(found)
    debug["returned_to_parser"] = len(results)
    debug["results"] = results

    DEBUG_FILE.write_text(
        json.dumps(debug, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Focused discovery elapsed seconds: {elapsed}", flush=True)
    print(f"Likely official PDFs found: {len(found)}", flush=True)
    print(f"PDFs returned to parser: {len(results)}", flush=True)
    return results


def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    return any(hint in hay for hint in STRONG_NEGATIVE_TITLE_HINTS)

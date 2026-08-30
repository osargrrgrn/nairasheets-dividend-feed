import html
import json
import re
from pathlib import Path
from urllib.parse import quote, urljoin

import requests

DOC_ROOT = "https://doclib.ngxgroup.com"
NEWS_FOLDER = "/Financial_NewsDocs"
OFFICIAL_DOC_HOST = "doclib.ngxgroup.com"
PDF_PATH_MARKER = "/Financial_NewsDocs/"

ROOT = Path(__file__).resolve().parents[1]
DEBUG_FILE = ROOT / "discovery_debug.json"

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
    r'https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^"\'<>\s]+?\.pdf',
    re.I,
)

RELATIVE_PDF_RE = re.compile(
    r'(?:/)?Financial_NewsDocs/[^"\'<>\s]+?\.pdf',
    re.I,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json;odata=verbose, application/xml;q=0.9, text/html;q=0.8, */*;q=0.7",
}

def _clean_url(url: str) -> str:
    if not url:
        return ""

    url = html.unescape(url).replace("\\/", "/")
    url = url.strip(" '\"\t\r\n")

    if url.startswith("/"):
        url = urljoin(DOC_ROOT, url)

    m = re.search(
        r"(https?://doclib\.ngxgroup\.com/Financial_NewsDocs/.+?\.pdf)",
        url,
        re.I,
    )
    if m:
        url = m.group(1)

    return url

def _title_from_url(url: str) -> str:
    name = url.rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf(?:\?.*)?$", "", name, flags=re.I)
    name = re.sub(r"^\d+_", "", name)
    name = name.replace("%20", " ")
    name = re.sub(r"_+", " ", name)
    return name.strip()

def _add(found, url, title=""):
    url = _clean_url(url)

    if OFFICIAL_DOC_HOST not in url.lower():
        return
    if PDF_PATH_MARKER.lower() not in url.lower():
        return
    if not url.lower().endswith(".pdf"):
        return

    found[url] = {
        "url": url,
        "title": (title or _title_from_url(url)).strip(),
    }

def _extract_from_text(text: str, found):
    if not text:
        return

    text = html.unescape(text).replace("\\/", "/")

    for match in PDF_URL_RE.findall(text):
        _add(found, match)

    for match in RELATIVE_PDF_RE.findall(text):
        _add(found, "/" + match.lstrip("/"))

def _extract_json_paths(obj, found):
    """
    Walk arbitrary SharePoint JSON because result shape differs by SharePoint
    version/configuration.
    """
    if isinstance(obj, dict):
        # Common SharePoint property cells look like:
        # {"Key":"Path","Value":"https://...pdf"}
        key = str(obj.get("Key", "")).lower()
        value = obj.get("Value")

        if key in {"path", "serverrelativeurl", "url"} and isinstance(value, str):
            _add(found, value)

        # Folder API may return Name + ServerRelativeUrl.
        server_url = obj.get("ServerRelativeUrl")
        name = obj.get("Name", "")
        if isinstance(server_url, str):
            _add(found, server_url, name if isinstance(name, str) else "")

        for value in obj.values():
            _extract_json_paths(value, found)

    elif isinstance(obj, list):
        for value in obj:
            _extract_json_paths(value, found)

    elif isinstance(obj, str):
        _extract_from_text(obj, found)

def _request(session, url, debug, label, params=None):
    item = {
        "label": label,
        "url": url,
        "params": params or {},
    }

    try:
        response = session.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=(8, 18),
            allow_redirects=True,
        )

        item["status"] = response.status_code
        item["final_url"] = response.url
        item["content_type"] = response.headers.get("content-type", "")
        item["bytes"] = len(response.content)
        item["preview"] = response.text[:1500]

        debug["attempts"].append(item)
        return response

    except Exception as exc:
        item["error"] = repr(exc)
        debug["attempts"].append(item)
        return None

def _sharepoint_search(session, found, debug):
    """
    Preferred method. Query the official SharePoint search interface for PDFs
    in Financial_NewsDocs modified in 2026.

    Paging is bounded: max 6 x 500 result requests.
    """
    endpoint = f"{DOC_ROOT}/_api/search/query"

    query_text = (
        f'Path:"{DOC_ROOT}{NEWS_FOLDER}" '
        'AND FileExtension:pdf '
        'AND LastModifiedTime>=2026-01-01'
    )

    start_row = 0
    row_limit = 500

    for page_no in range(6):
        params = {
            "querytext": f"'{query_text}'",
            "rowlimit": str(row_limit),
            "startrow": str(start_row),
            "trimduplicates": "false",
            "selectproperties": "'Title,Path,FileExtension,LastModifiedTime'",
        }

        response = _request(
            session,
            endpoint,
            debug,
            f"sharepoint_search_page_{page_no + 1}",
            params=params,
        )

        if response is None or response.status_code >= 400:
            break

        before = len(found)

        try:
            payload = response.json()
            _extract_json_paths(payload, found)

            # Try to read TotalRows from any nested location.
            text = json.dumps(payload)
            total_match = re.search(r'"TotalRows"\s*:\s*(\d+)', text)
            total_rows = int(total_match.group(1)) if total_match else None

        except Exception:
            _extract_from_text(response.text, found)
            total_rows = None

        added = len(found) - before

        debug["search_pages"].append({
            "page": page_no + 1,
            "start_row": start_row,
            "added_pdfs": added,
            "running_total": len(found),
            "reported_total_rows": total_rows,
        })

        start_row += row_limit

        if total_rows is not None and start_row >= total_rows:
            break

        # If a whole page returns no new official PDFs, do not keep hammering it.
        if added == 0 and page_no > 0:
            break

def _folder_api(session, found, debug):
    """
    Fallback for SharePoint installations where search is disabled but the
    public document folder itself can be enumerated.
    """
    encoded_folder = quote(NEWS_FOLDER, safe="/")
    endpoint = (
        f"{DOC_ROOT}/_api/web/GetFolderByServerRelativeUrl"
        f"('{encoded_folder}')/Files"
    )

    params = {
        "$select": "Name,ServerRelativeUrl,TimeCreated,TimeLastModified",
        "$top": "5000",
    }

    response = _request(
        session,
        endpoint,
        debug,
        "sharepoint_folder_files",
        params=params,
    )

    if response is None or response.status_code >= 400:
        return

    try:
        _extract_json_paths(response.json(), found)
    except Exception:
        _extract_from_text(response.text, found)

def _library_html(session, found, debug):
    """
    Last fallback: the SharePoint document library HTML. Still direct HTTP,
    still bounded, no browser.
    """
    urls = [
        f"{DOC_ROOT}{NEWS_FOLDER}/Forms/AllItems.aspx",
        f"{DOC_ROOT}{NEWS_FOLDER}/",
    ]

    for idx, url in enumerate(urls, 1):
        response = _request(
            session,
            url,
            debug,
            f"library_html_{idx}",
        )

        if response is not None and response.status_code < 400:
            _extract_from_text(response.text, found)

def discover_official_pdfs():
    found = {}

    debug = {
        "method": "direct_official_doclib_http",
        "attempts": [],
        "search_pages": [],
    }

    with requests.Session() as session:
        _sharepoint_search(session, found, debug)

        # If search produced a useful result set, don't waste time on fallbacks.
        if len(found) < 20:
            _folder_api(session, found, debug)

        if len(found) < 20:
            _library_html(session, found, debug)

    debug["pdfs_found"] = len(found)
    debug["sample_pdfs"] = list(found.values())[:20]

    DEBUG_FILE.write_text(
        json.dumps(debug, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("Direct NGX document-library discovery")
    print(f"Official PDFs found this run: {len(found)}")

    for page in debug["search_pages"]:
        print(
            "Search page "
            f"{page['page']}: +{page['added_pdfs']} PDFs "
            f"(running total {page['running_total']})"
        )

    return list(found.values())

def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    return any(hint in hay for hint in STRONG_NEGATIVE_TITLE_HINTS)

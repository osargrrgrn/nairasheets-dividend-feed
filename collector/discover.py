from urllib.parse import urljoin
from pathlib import Path
import json
import re
import time
from playwright.sync_api import sync_playwright

NGX_DISCLOSURES_URL = "https://ngxgroup.com/exchange/data/corporate-disclosures/"
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

def _clean_pdf_url(url: str) -> str:
    if not url:
        return ""
    url = url.replace("\\/", "/").replace("&amp;", "&")
    m = re.match(r"(.+?\.pdf)(?:\?.*)?$", url, re.I)
    return m.group(1) if m else url

def _title_from_url(url: str) -> str:
    name = url.rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf(?:\?.*)?$", "", name, flags=re.I)
    name = re.sub(r"^\d+_", "", name)
    return re.sub(r"_+", " ", name).strip()

def _add_url(found, url, title=""):
    url = _clean_pdf_url(url)
    if OFFICIAL_DOC_HOST not in url:
        return
    if PDF_PATH_MARKER not in url:
        return
    if ".pdf" not in url.lower():
        return
    found[url] = {
        "url": url,
        "title": (title or _title_from_url(url)).strip(),
    }

def _collect_from_text(text, found):
    if not text:
        return
    text = text.replace("\\/", "/")
    for match in PDF_URL_RE.findall(text):
        _add_url(found, match)

def _collect_dom(page, found):
    try:
        links = page.locator("a")
        for i in range(links.count()):
            a = links.nth(i)
            try:
                href = a.get_attribute("href")
                label = (a.inner_text(timeout=500) or "").strip()
            except Exception:
                continue
            if href:
                _add_url(found, urljoin(page.url, href), label)
    except Exception:
        pass

    try:
        _collect_from_text(page.content(), found)
    except Exception:
        pass

def _safe_post_data(request):
    try:
        data = request.post_data
        if data and len(data) > 5000:
            data = data[:5000] + "...[truncated]"
        return data
    except Exception:
        return None

def _interesting_request(url: str) -> bool:
    low = (url or "").lower()
    hints = (
        "ajax",
        "jet",
        "filter",
        "disclosure",
        "graphql",
        "api",
        "wp-json",
        "admin-ajax",
        "load",
        "page",
    )
    return "ngxgroup.com" in low and any(h in low for h in hints)

def _try_one_pagination_action(page):
    selectors = [
        'a[rel="next"]',
        'button[aria-label*="Next" i]',
        'a[aria-label*="Next" i]',
        '.next.page-numbers',
        'a.next',
        'button.next',
        '.pagination-next a',
        '.pagination__next a',
        '.jet-filters-pagination__next',
        '.jet-smart-filters-pagination__next',
        '.dataTables_paginate .next:not(.disabled)',
        'a:has-text("Next")',
        'button:has-text("Next")',
        'a:has-text("Older")',
        'button:has-text("Older")',
    ]

    for selector in selectors:
        try:
            loc = page.locator(selector)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible() and not el.is_disabled():
                    el.scroll_into_view_if_needed()
                    el.click(timeout=3000)
                    return selector
        except Exception:
            continue

    # One scroll only, purely to trigger lazy/infinite loading if present.
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return "single_scroll"
    except Exception:
        return "none"

def discover_official_pdfs():
    found = {}
    requests_log = []
    responses_log = []

    started = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1400},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )

        def on_request(request):
            try:
                if _interesting_request(request.url):
                    requests_log.append({
                        "method": request.method,
                        "url": request.url,
                        "resource_type": request.resource_type,
                        "post_data": _safe_post_data(request),
                    })
            except Exception:
                pass

        def on_response(response):
            try:
                url = response.url
                ctype = (response.headers.get("content-type") or "").lower()

                if OFFICIAL_DOC_HOST in url and ".pdf" in url.lower():
                    _add_url(found, url)

                if _interesting_request(url):
                    item = {
                        "status": response.status,
                        "url": url,
                        "content_type": ctype,
                    }

                    if any(x in ctype for x in ("json", "html", "text", "javascript")):
                        try:
                            body = response.text()
                            item["body_preview"] = body[:4000]
                            _collect_from_text(body, found)
                        except Exception as exc:
                            item["body_error"] = repr(exc)

                    responses_log.append(item)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(
            NGX_DISCLOSURES_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(5000)
        _collect_dom(page, found)

        action = _try_one_pagination_action(page)

        page.wait_for_timeout(7000)
        _collect_dom(page, found)

        # Hard stop: this diagnostic is never allowed to crawl indefinitely.
        browser.close()

    debug = {
        "page": NGX_DISCLOSURES_URL,
        "elapsed_seconds": round(time.time() - started, 2),
        "pagination_action_attempted": action,
        "pdfs_found": len(found),
        "requests": requests_log[-100:],
        "responses": responses_log[-100:],
    }

    DEBUG_FILE.write_text(
        json.dumps(debug, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Discovery diagnostic elapsed seconds: {debug['elapsed_seconds']}")
    print(f"Discovery diagnostic requests captured: {len(requests_log)}")
    print(f"Discovery diagnostic responses captured: {len(responses_log)}")
    print(f"Discovery diagnostic PDFs found: {len(found)}")
    print(f"Discovery diagnostic pagination action: {action}")

    return list(found.values())

def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    return any(hint in hay for hint in STRONG_NEGATIVE_TITLE_HINTS)

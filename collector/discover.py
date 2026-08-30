from urllib.parse import urljoin
import re
from playwright.sync_api import sync_playwright

NGX_DISCLOSURES_URL = "https://ngxgroup.com/exchange/data/corporate-disclosures/"
OFFICIAL_DOC_HOST = "doclib.ngxgroup.com"
PDF_PATH_MARKER = "/Financial_NewsDocs/"

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
    url = url.split("#")[0]
    return url

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

    # Strip common cache/tracking fragments after .pdf but retain real query
    # strings only if needed. The NGX doclib PDFs normally work without them.
    m = re.match(r"(.+?\.pdf)(?:\?.*)?$", url, re.I)
    if m:
        url = m.group(1)

    previous = found.get(url, {})
    useful_title = (title or previous.get("title") or _title_from_url(url)).strip()

    found[url] = {
        "url": url,
        "title": useful_title,
    }

def _collect_from_text(text, found):
    if not text:
        return

    # JSON responses sometimes escape forward slashes.
    text = text.replace("\\/", "/")

    for match in PDF_URL_RE.findall(text):
        _add_url(found, match)

def _collect_from_dom(page, found):
    # 1. Normal links.
    try:
        anchors = page.locator("a")
        for i in range(anchors.count()):
            a = anchors.nth(i)
            try:
                href = a.get_attribute("href")
                label = (a.inner_text(timeout=1000) or "").strip()
            except Exception:
                continue

            if not href:
                continue

            href = urljoin(page.url, href)
            _add_url(found, href, label)
    except Exception:
        pass

    # 2. Raw HTML catches links embedded in scripts, JSON attributes or
    # JavaScript-rendered card data that are not ordinary anchors.
    try:
        _collect_from_text(page.content(), found)
    except Exception:
        pass

def _click_first_visible(page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()

            for i in range(min(count, 8)):
                el = locator.nth(i)
                if not el.is_visible():
                    continue
                if el.is_disabled():
                    continue

                el.scroll_into_view_if_needed()
                el.click(timeout=5000)
                return True
        except Exception:
            pass

    return False

def _click_next_pagination(page):
    """
    Cover the common pagination systems used by WordPress/Elementor,
    JetEngine/JetSmartFilters, DataTables and conventional rel=next links.
    """
    selectors = [
        'a[rel="next"]',
        'button[aria-label*="Next" i]',
        'a[aria-label*="Next" i]',
        '.next.page-numbers',
        'a.next',
        'button.next',
        '.pagination-next a',
        '.pagination__next a',
        '.jet-filters-pagination__link[data-value="next"]',
        '.jet-filters-pagination__next',
        '.jet-smart-filters-pagination__next',
        '.dataTables_paginate .next:not(.disabled)',
    ]

    if _click_first_visible(page, selectors):
        return True

    # Text-based fallback.
    text_selectors = [
        'a:has-text("Next")',
        'button:has-text("Next")',
        'a:has-text("Older")',
        'button:has-text("Older")',
        'a:has-text("›")',
        'button:has-text("›")',
        'a:has-text("»")',
        'button:has-text("»")',
    ]

    return _click_first_visible(page, text_selectors)

def _click_next_number(page, already_clicked_numbers):
    """
    Some NGX/WordPress pagination widgets expose only numbered page links.
    Click the smallest page number we have not tried yet.
    """
    candidates = []

    selectors = [
        'a.page-numbers',
        '.pagination a',
        '.jet-filters-pagination__link',
        '.jet-smart-filters-pagination__link',
    ]

    for selector in selectors:
        try:
            loc = page.locator(selector)

            for i in range(loc.count()):
                el = loc.nth(i)

                try:
                    text = (el.inner_text(timeout=500) or "").strip()
                except Exception:
                    continue

                if not text.isdigit():
                    continue

                number = int(text)

                if number <= 1 or number in already_clicked_numbers:
                    continue

                if el.is_visible():
                    candidates.append((number, el))
        except Exception:
            pass

    if not candidates:
        return False

    candidates.sort(key=lambda item: item[0])
    number, element = candidates[0]

    try:
        element.scroll_into_view_if_needed()
        element.click(timeout=5000)
        already_clicked_numbers.add(number)
        return True
    except Exception:
        return False

def _crawl_historical_pages(page, found, max_rounds=120):
    """
    Traverse historical disclosure results.

    Stop only after several consecutive rounds reveal no new PDFs and no
    pagination action succeeds. This is deliberately much deeper than the
    previous 'load more' crawler.
    """
    stagnant_rounds = 0
    clicked_numbers = set()
    last_count = len(found)
    seen_page_signatures = set()

    for _ in range(max_rounds):
        _collect_from_dom(page, found)

        try:
            signature = (
                page.url,
                page.locator("body").inner_text(timeout=3000)[-3000:],
            )
        except Exception:
            signature = (page.url, str(len(found)))

        if signature in seen_page_signatures:
            stagnant_rounds += 1
        else:
            seen_page_signatures.add(signature)

        moved = False

        # First prefer explicit Next/Older controls.
        if _click_next_pagination(page):
            moved = True
        elif _click_next_number(page, clicked_numbers):
            moved = True
        else:
            # Infinite-scroll fallback.
            try:
                old_height = page.evaluate("document.body.scrollHeight")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1800)
                new_height = page.evaluate("document.body.scrollHeight")
                moved = new_height > old_height
            except Exception:
                moved = False

        if moved:
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(1200)

        _collect_from_dom(page, found)

        if len(found) > last_count:
            stagnant_rounds = 0
            last_count = len(found)
        else:
            stagnant_rounds += 1

        # Five dead rounds is enough evidence that the UI has been exhausted.
        if stagnant_rounds >= 5 and not moved:
            break

def discover_official_pdfs():
    found = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            viewport={"width": 1440, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )

        # Capture dynamically loaded disclosure payloads.
        def on_response(response):
            try:
                ctype = (response.headers.get("content-type") or "").lower()
                url = response.url

                # Direct PDF response.
                if OFFICIAL_DOC_HOST in url and ".pdf" in url.lower():
                    _add_url(found, url)
                    return

                # AJAX/JSON/HTML responses often contain the document URLs.
                if (
                    "json" in ctype
                    or "text/html" in ctype
                    or "javascript" in ctype
                    or "text/plain" in ctype
                ):
                    body = response.text()
                    _collect_from_text(body, found)
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(
            NGX_DISCLOSURES_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        page.wait_for_timeout(2500)

        _collect_from_dom(page, found)
        _crawl_historical_pages(page, found)

        browser.close()

    return list(found.values())

def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    return any(hint in hay for hint in STRONG_NEGATIVE_TITLE_HINTS)

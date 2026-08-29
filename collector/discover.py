import re
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

NGX_DISCLOSURES_URL = "https://ngxgroup.com/exchange/data/corporate-disclosures/"
OFFICIAL_DOC_HOST = "doclib.ngxgroup.com"
PDF_PATH_MARKER = "/Financial_NewsDocs/"

DIVIDEND_HINTS = (
    "dividend",
    "corporate action",
    "corporate actions",
    "interim dividend",
    "final dividend",
    "distribution",
)

def discover_official_pdfs(max_scrolls: int = 12):
    """
    Open the official NGX Corporate Disclosures page and collect official NGX PDF links.

    This deliberately uses a browser rather than depending on an undocumented NGX API.
    If NGX changes its internal API, the visible disclosure page may still continue to work.
    """
    found = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.goto(NGX_DISCLOSURES_URL, wait_until="networkidle", timeout=90000)

        # The page is dynamic. Scroll to allow additional results to render.
        for _ in range(max_scrolls):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)

        anchors = page.locator("a").all()
        for a in anchors:
            href = a.get_attribute("href")
            text = (a.inner_text() or "").strip()
            if not href:
                continue
            href = urljoin(NGX_DISCLOSURES_URL, href)

            if OFFICIAL_DOC_HOST not in href:
                continue
            if PDF_PATH_MARKER not in href:
                continue
            if not href.lower().split("?")[0].endswith(".pdf"):
                continue

            key = href.split("#")[0]
            found[key] = {
                "url": key,
                "title": text,
            }

        browser.close()

    return list(found.values())

def looks_dividend_related(title: str, url: str) -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    return any(hint in hay for hint in DIVIDEND_HINTS)

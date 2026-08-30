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

# Official NGX PDFs used only to bootstrap the archive before the collector
# has accumulated enough history on its own.
BOOTSTRAP_DISCLOSURES = [
    {
        "url": "https://doclib.ngxgroup.com/Financial_NewsDocs/47694_MTN_NIGERIA_COMMUNICATIONS_PLC-NGX_NOTIFICATION_MTN_NIGERIA_INTERIM_DIVIDEND_NO_36_CORPORATE_ACTIONS_JULY_2026.pdf",
        "title": "MTN NIGERIA COMMUNICATIONS PLC - NGX NOTIFICATION MTN NIGERIA INTERIM DIVIDEND NO 36",
    },
]

def _collect_official_pdf_links(page, found):
    for a in page.locator("a").all():
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

def _try_load_older_results(page, found, max_rounds=30):
    """
    NGX's disclosures page can expose older records via dynamic controls.
    We try common 'load more' / pagination controls and collect official
    PDF links after every successful expansion.

    If the page has no such control, this safely falls back to scrolling.
    """
    previous_count = len(found)
    stagnant_rounds = 0

    for _ in range(max_rounds):
        clicked = False

        # Prefer explicit load-more style buttons; avoid generic navigation links.
        candidates = [
            page.get_by_role("button", name=re.compile(r"load\s+more", re.I)),
            page.get_by_role("button", name=re.compile(r"show\s+more", re.I)),
            page.get_by_role("link", name=re.compile(r"load\s+more", re.I)),
            page.get_by_role("link", name=re.compile(r"show\s+more", re.I)),
            page.locator("button").filter(has_text=re.compile(r"more", re.I)),
        ]

        for locator in candidates:
            try:
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=5000)
                    page.wait_for_timeout(1800)
                    clicked = True
                    break
            except Exception:
                pass

        if not clicked:
            # Infinite-scroll fallback.
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1400)
            except Exception:
                break

        _collect_official_pdf_links(page, found)

        if len(found) == previous_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
            previous_count = len(found)

        # Stop if repeated scrolling/clicking no longer reveals anything.
        if stagnant_rounds >= 4:
            break

def discover_official_pdfs():
    found = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})

        page.goto(
            NGX_DISCLOSURES_URL,
            wait_until="networkidle",
            timeout=90000
        )

        _collect_official_pdf_links(page, found)
        _try_load_older_results(page, found)

        browser.close()

    # Bootstrap known official NGX disclosures so a newly created archive
    # immediately contains relevant recent history.
    for item in BOOTSTRAP_DISCLOSURES:
        found[item["url"]] = item

    return list(found.values())

def title_is_strongly_irrelevant(title: str, url: str = "") -> bool:
    hay = f"{title} {url}".lower().replace("_", " ")
    return any(hint in hay for hint in STRONG_NEGATIVE_TITLE_HINTS)

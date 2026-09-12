"""
collector/deep_scan.py — Patch 50c

Per-company AbokiForex disclosure scraper.

AbokiForex has individual pages for each NGX company at:
  https://abokiforex.app/ngx-stocks/disclosures/{TICKER}

Each page lists ALL historical filings for that company with direct
doclib PDF links — going back much further than the main listing page.

This finds GTCO's April 2026 dividend announcement, Zenith's April
announcement, Access Holdings, UBA etc. that scrolled off the main
200-item listing.

Per run: scrapes 10 company pages, each covering full history.
With 147 companies and 5 runs/day → full coverage in ~3 days.
State tracked in discovery_state.json to avoid re-scraping.
"""

import json
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DEEP_SCAN_STATE = ROOT / "deep_scan_state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://abokiforex.app/ngx-stocks/disclosures",
}

ABOKI_COMPANY_BASE = "https://abokiforex.app/ngx-stocks/disclosures"

PDF_URL_RE = re.compile(
    r"https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^\s\"'<>\]]+?\.pdf",
    re.I,
)

# All 147 NGX tickers — company disclosure pages to scrape
NGX_TICKERS = [
    "GTCO", "ZENITHBANK", "ACCESSCORP", "UBA", "FIDELITYBK",
    "STANBIC", "FBNH", "FCMB", "FIRSTHOLDCO", "WEMABANK",
    "JAIZBANK", "STERLINGNG", "UNITYBNK", "ABBEYBDS",
    "DANGCEM", "BUACEMENT", "WAPCO", "JBERGER", "JULBERGER",
    "BUAFOODS", "FLOURMILL", "NESTLE", "UNILEVER", "CADBURY",
    "NASCON", "VITAFOAM", "HONYFLOUR", "MCNICHOLS",
    "SEPLAT", "TOTAL", "ETERNA", "OKOMUOIL", "PRESCO",
    "OANDO", "ARDOVA", "CONOIL",
    "MTNN", "AIRTELAFRI", "CWG", "CHAMS",
    "GTCO", "CUSTODIAN", "AIICO", "MANSARD", "NEM",
    "CORNERST", "UNIVINSURE", "MBENEFIT", "SUNUASSUR",
    "NGXGROUP", "AFRIPRUD", "UCAP", "VFDGROUP", "TRANSCORP",
    "TRANSCOHOT", "TRANSPOWER", "UACN", "CAP", "BERGER",
    "UPDCREIT", "NIDF", "SFSREIT", "SIAML40ETF", "STANBICETF30",
    "ARADEL", "GEREGU", "JAPAULGOLD",
    "DANGSUGAR", "INTBREW", "NB", "GUINNESS",
    "LEARNAFRCA", "UPL", "ACADEMY", "IKEJAHOTEL",
    "REDSTAREX", "CUTIX", "MAYBAKER", "FIDSON",
    "PZ", "BETAGLAS", "DNMEYER",
    "MULTIVERSE", "COURTVILLE", "ETRANZACT",
    "ABCTRANS", "NAHCO", "OMATEK",
    "AFROMEDIA", "ROYALEX", "LASACO",
    "LINKASSURE", "PRESTIGE", "GOLDINSURE",
    "MBENEFIT", "AXAMANSARD",
    "ETERNAONZ", "MOBIL",
    "NPFMCRFBK", "INFINITYTRUST",
]
# Remove duplicates
NGX_TICKERS = list(dict.fromkeys(NGX_TICKERS))

COMPANIES_PER_RUN = 10


def _load_scan_state() -> dict:
    """Load which companies have been scraped already."""
    if DEEP_SCAN_STATE.exists():
        try:
            return json.loads(DEEP_SCAN_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"scraped": [], "last_run": None}


def _save_scan_state(state: dict):
    """Save scan state."""
    try:
        DEEP_SCAN_STATE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _scrape_company_page(session: requests.Session, ticker: str, known: set) -> list:
    """
    Scrape AbokiForex company disclosure page for a given ticker.
    Returns list of new doclib PDF URLs found.
    """
    url = f"{ABOKI_COMPANY_BASE}/{ticker}"
    try:
        r = session.get(url, headers=HEADERS, timeout=(8, 15), allow_redirects=True)
        if r.status_code != 200:
            return []

        pdfs = PDF_URL_RE.findall(r.text)
        new_pdfs = [p for p in pdfs if p not in known]
        return new_pdfs

    except Exception:
        return []


def discover_historical_pdfs(known_urls: set, debug: dict, started: float) -> list:
    """
    Patch 50c: Scrape per-company AbokiForex disclosure pages.

    Each company page lists ALL historical filings with direct doclib
    PDF links — finds dividends declared months ago that scrolled off
    the main 200-item listing.
    """
    found = []
    dbg = {
        "companies_scraped": 0,
        "companies_failed": 0,
        "new_pdfs": 0,
        "errors": [],
    }

    print("[DeepScan] Starting per-company AbokiForex scrape", flush=True)

    state = _load_scan_state()
    scraped = set(state.get("scraped", []))

    # Find companies not yet scraped
    remaining = [t for t in NGX_TICKERS if t not in scraped]

    if not remaining:
        # All companies scraped — reset and start again
        print("[DeepScan] All companies scraped — resetting for next cycle", flush=True)
        scraped = set()
        remaining = list(NGX_TICKERS)
        state["scraped"] = []

    # Take next batch
    batch = remaining[:COMPANIES_PER_RUN]
    print(f"[DeepScan] Scraping {len(batch)} companies ({len(remaining)} remaining)", flush=True)

    with requests.Session() as s:
        for ticker in batch:
            elapsed = time.monotonic() - started
            if elapsed > 160:
                break

            new_pdfs = _scrape_company_page(s, ticker, known_urls)
            dbg["companies_scraped"] += 1

            if new_pdfs:
                for pdf_url in new_pdfs:
                    title = (
                        pdf_url.split("/Financial_NewsDocs/")[-1]
                        .replace(".pdf", "")
                        .replace("_", " ")
                        .strip()
                    )
                    # Basic relevance filter
                    url_upper = pdf_url.upper()
                    dividend_signals = (
                        "DIVIDEND", "DISTRIBUTION", "CORPORATE_ACTION",
                        "NGX_NOTIFICATION", "QUALIFICATION",
                    )
                    if any(s in url_upper for s in dividend_signals):
                        found.append({
                            "url": pdf_url,
                            "title": title,
                            "source": "deep_scan_company_page",
                        })
                        known_urls.add(pdf_url)
                        print(f"[DeepScan] {ticker}: {title[:50]}", flush=True)
                    else:
                        # Still add to known so we don't re-check
                        known_urls.add(pdf_url)

            scraped.add(ticker)
            time.sleep(1)  # Respectful delay

    # Save state
    state["scraped"] = list(scraped)
    import datetime
    state["last_run"] = datetime.date.today().isoformat()
    _save_scan_state(state)

    dbg["new_pdfs"] = len(found)
    debug["deep_scan"] = dbg
    print(
        f"[DeepScan] {len(found)} new dividend PDFs found "
        f"({dbg['companies_scraped']} companies scraped)",
        flush=True,
    )
    return found

"""
Patch 50: Deep sequential doclib scanner

Scans document numbers 42000-46023 (pre-archive range) to find
historical NGX dividend announcements that scrolled off AbokiForex.

Strategy:
1. For each gap number, try Kobo Terminal and AbokiForex detail pages
2. If those fail, construct likely dividend announcement URLs using
   known company name → filename patterns from the existing archive
3. Try HEAD requests directly to doclib to verify existence

This is how we find GTCO April dividend, Zenith April dividend etc.
"""

import re
import time
import random
import requests
from pathlib import Path

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}

DOCLIB_BASE = "https://doclib.ngxgroup.com/Financial_NewsDocs"
KOBO_DISCLOSURE = "https://koboterminal.com/disclosures"

# Known dividend-related filename patterns from existing archive
# These are the suffixes that appear in dividend announcement filenames
DIVIDEND_SUFFIXES = [
    "DIVIDEND_ANNOUNCEMENT_CORPORATE_ACTIONS",
    "CORPORATE_ACTION_ANNOUNCEMENT_CORPORATE_ACTIONS",
    "CORPORATE_ACTIONS_ANNOUNCEMENT_CORPORATE_ACTIONS",
    "NGX_NOTIFICATION",
    "INTERIM_DIVIDEND",
    "FINAL_DIVIDEND",
    "DISTRIBUTION_PAYMENT",
]

# Company name patterns for known missing companies
# Maps company to the filename fragment NGX uses
COMPANY_PATTERNS = {
    "GTCO": [
        "GUARANTY_TRUST_HOLDING_COMPANY_PLC",
        "GTCO",
    ],
    "ZENITHBANK": [
        "ZENITH_BANK_PLC",
        "ZENITH_BANK",
    ],
    "ACCESSCORP": [
        "ACCESS_HOLDINGS_PLC",
        "ACCESS_BANK_PLC",
    ],
    "UBA": [
        "UNITED_BANK_FOR_AFRICA_PLC",
        "UBA",
    ],
    "FIDELITYBK": [
        "FIDELITY_BANK_PLC",
        "FIDELITY_BANK",
    ],
    "STANBIC": [
        "STANBIC_IBTC_HOLDINGS_PLC",
        "STANBIC_IBTC",
    ],
    "FBNH": [
        "FBN_HOLDINGS_PLC",
        "FIRST_BANK_OF_NIGERIA",
    ],
    "WAPCO": [
        "LAFARGE_AFRICA_PLC",
        "LAFARGE_AFRICA",
    ],
    "BUAFOODS": [
        "BUA_FOODS_PLC",
        "BUA_FOODS",
    ],
    "TRANSCORP": [
        "TRANSCORP_PLC",
        "TRANSCORPORATION_OF_NIGERIA",
    ],
    "WEMABANK": [
        "WEMA_BANK_PLC",
        "WEMA_BANK",
    ],
    "JBERGER": [
        "JULIUS_BERGER_NIGERIA_PLC",
        "JULIUS_BERGER",
    ],
    "NASCON": [
        "NASCON_ALLIED_INDUSTRIES_PLC",
        "NASCON",
    ],
    "UCAP": [
        "UNITED_CAPITAL_PLC",
        "UNITED_CAPITAL",
    ],
    "GEREGU": [
        "GEREGU_POWER_PLC",
        "GEREGU_POWER",
    ],
}

# Month name mapping
MONTHS = {
    1: "JANUARY", 2: "FEBRUARY", 3: "MARCH", 4: "APRIL",
    5: "MAY", 6: "JUNE", 7: "JULY", 8: "AUGUST",
    9: "SEPTEMBER", 10: "OCTOBER", 11: "NOVEMBER", 12: "DECEMBER",
}


def _build_candidate_urls(number: int) -> list:
    """
    Build candidate doclib URLs for a given document number.
    Uses known company patterns and dividend suffix patterns.
    """
    candidates = []
    
    # Try each company × each dividend suffix × each month
    for company, patterns in COMPANY_PATTERNS.items():
        for pattern in patterns:
            for suffix in DIVIDEND_SUFFIXES:
                # Try Q1 2026 months (Jan-Apr) for early declarations
                for month_num in [3, 4, 5]:  # March, April, May most common
                    month = MONTHS[month_num]
                    url = f"{DOCLIB_BASE}/{number}_{pattern}-{suffix}_{month}_2026.pdf"
                    candidates.append((url, company))
    
    return candidates


def _check_url_exists(session: requests.Session, url: str) -> bool:
    """Check if a doclib URL exists using HEAD request."""
    try:
        r = session.head(url, headers=HEADERS, timeout=(3, 6), allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False


def _extract_pdfs_from_page(html: str, base_url: str) -> list:
    """Extract doclib PDF URLs from an HTML page."""
    PDF_RE = re.compile(
        r"https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^\s\"'<>\]]+?\.pdf",
        re.I,
    )
    return list(set(PDF_RE.findall(html)))


def discover_historical_pdfs(known_urls: set, debug: dict, started: float) -> list:
    """
    Patch 50: Deep sequential scan for historical dividend PDFs
    in the 42000-46023 range not covered by AbokiForex.
    """
    import time as _time
    
    found = []
    dbg = {
        "numbers_scanned": 0,
        "urls_tried": 0, 
        "new_pdfs": 0,
        "errors": [],
    }

    print("[DeepScan] Starting historical range scan 42000-46023", flush=True)

    # Get known numbers from archive
    num_re = re.compile(r"/Financial_NewsDocs/(\d{4,6})_", re.I)
    known_numbers = set()
    for url in known_urls:
        m = num_re.search(url)
        if m:
            known_numbers.add(int(m.group(1)))

    # Find gap numbers in target range
    target_range = range(42000, 46024)
    gap_numbers = [n for n in target_range if n not in known_numbers]
    
    print(f"[DeepScan] {len(gap_numbers)} gap numbers in range 42000-46023", flush=True)

    # Prioritize numbers that are likely to be dividend announcements
    # Based on observation: dividend docs tend to cluster in certain ranges
    # Shuffle for variety across runs
    random.shuffle(gap_numbers)
    
    # Process DEEP_SCAN_BATCH numbers per run
    DEEP_SCAN_BATCH = 100
    batch = gap_numbers[:DEEP_SCAN_BATCH]

    with requests.Session() as s:
        for num in batch:
            remaining = 180 - (200 - (started - _time.monotonic() + 200))
            if remaining <= 10:
                break

            dbg["numbers_scanned"] += 1
            
            # Try Kobo Terminal first
            try:
                kobo_url = f"https://koboterminal.com/disclosures/{num}"
                r = s.get(kobo_url, headers=HEADERS, timeout=(4, 8))
                if r.status_code == 200:
                    pdfs = _extract_pdfs_from_page(r.text, kobo_url)
                    for pdf_url in pdfs:
                        if pdf_url not in known_urls:
                            from .discover import _title_from_url, _looks_strongly_irrelevant
                            title = _title_from_url(pdf_url)
                            if not _looks_strongly_irrelevant(title, pdf_url):
                                found.append({"url": pdf_url, "title": title, "source": "deep_scan_kobo"})
                                known_urls.add(pdf_url)
                                print(f"[DeepScan] Found via Kobo: {title[:50]}", flush=True)
                    _time.sleep(0.5)
                    continue
            except Exception:
                pass

            # Try candidate URL construction for known companies
            candidates = _build_candidate_urls(num)
            for url, company in candidates[:5]:  # Max 5 attempts per number
                if url in known_urls:
                    continue
                dbg["urls_tried"] += 1
                if _check_url_exists(s, url):
                    from .discover import _title_from_url, _looks_strongly_irrelevant
                    title = _title_from_url(url)
                    found.append({"url": url, "title": title, "source": "deep_scan_direct"})
                    known_urls.add(url)
                    print(f"[DeepScan] Found: {company} — {title[:50]}", flush=True)
                    break
                _time.sleep(0.1)

    dbg["new_pdfs"] = len(found)
    debug["deep_scan"] = dbg
    print(f"[DeepScan] {len(found)} new PDFs found from historical range", flush=True)
    return found

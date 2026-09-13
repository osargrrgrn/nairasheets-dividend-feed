"""
collector/trw_scraper.py — Patch 49

TRW Stockbrokers scraper for NGX dividend data.

TRW Stockbrokers (trwsb.wordpress.com) is a licensed NGX broker that
publishes two types of useful content:

1. DAILY DISCLOSURE SUMMARIES — posted each trading day, containing
   direct links to official NGX doclib PDFs. Used for discovery.

2. DIVIDEND TABLE — a comprehensive NGX dividend table updated periodically
   throughout the year with qualification and payment dates for 40-80+
   companies. Used for date-filling only (Option A).

Architecture:
- Discovery: extracts doclib PDF URLs from TRW daily posts → feeds into
  the main archive, passes PDFs to the existing parser
- Date filling: parses TRW dividend table → updates known_dates.json
  with qualification/payment dates → used by run.py to fill gaps in
  pending events that already have PDF-extracted amounts

The NGX doclib PDF is ALWAYS the authoritative source. TRW data only
fills gaps — it never creates new events or overrides extracted data.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
KNOWN_DATES_FILE = ROOT / "known_dates.json"

TRW_BASE = "https://trwsb.wordpress.com"

# TRW posts daily disclosure summaries — fetch the most recent ones
TRW_DISCLOSURE_SEARCH = "https://trwsb.wordpress.com/?s=corporate+disclosures+NGX"
TRW_DIVIDEND_TABLE_SEARCH = "https://trwsb.wordpress.com/?s=NGX+dividend+bonus+table"

# Mansa Markets — comprehensive NGX dividend list with dates
MANSA_DIVIDEND_URL = "https://www.mansamarkets.com/blog/dividends-declared-2026-nigeria-ngx-companies"

# Additional Mansa ticker mappings
MANSA_COMPANY_TO_TICKER = {
    "access holdings": "ACCESSCORP",
    "access bank": "ACCESSCORP",
    "airtel africa": "AIRTELAFRI",
    "bua cement": "BUACEMENT",
    "bua foods": "BUAFOODS",
    "dangote cement": "DANGCEM",
    "dangote sugar": "DANGSUGAR",
    "fbn holdings": "FBNH",
    "fcmb group": "FCMB",
    "fidelity bank": "FIDELITYBK",
    "first holdco": "FIRSTHOLDCO",
    "flour mills": "FLOURMILL",
    "geregu power": "GEREGU",
    "gtco": "GTCO",
    "guaranty trust": "GTCO",
    "guinness nigeria": "GUINNESS",
    "julius berger": "JBERGER",
    "lafarge africa": "WAPCO",
    "mtn nigeria": "MTNN",
    "nascon": "NASCON",
    "nem insurance": "NEM",
    "ngx group": "NGXGROUP",
    "nigerian breweries": "NB",
    "okomu oil": "OKOMUOIL",
    "presco": "PRESCO",
    "seplat energy": "SEPLAT",
    "stanbic ibtc": "STANBIC",
    "transcorp": "TRANSCORP",
    "uba": "UBA",
    "united bank": "UBA",
    "unilever nigeria": "UNILEVER",
    "united capital": "UCAP",
    "vfd group": "VFDGROUP",
    "zenith bank": "ZENITHBANK",
    "wema bank": "WEMABANK",
    "berger paints": "BERGER",
    "eterna": "ETERNA",
    "aradel": "ARADEL",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PDF_URL_RE = re.compile(
    r"https?://doclib\.ngxgroup\.com/Financial_NewsDocs/[^\s\"'<>\]]+?\.pdf",
    re.I,
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# Known ticker mappings for TRW company names
TRW_COMPANY_TO_TICKER = {
    "abc transport": "ABCTRANS",
    "access holdings": "ACCESSCORP",
    "africa prudential": "AFRIPRUD",
    "aiico insurance": "AIICO",
    "aradel holdings": "ARADEL",
    "berger paints": "BERGER",
    "beta glass": "BETAGLAS",
    "bua cement": "BUACEMENT",
    "bua foods": "BUAFOODS",
    "cadbury nigeria": "CADBURY",
    "cap": "CAP",
    "chemical and allied": "CAP",
    "cornerstone insurance": "CORNERST",
    "custodian investment": "CUSTODIAN",
    "cwg": "CWG",
    "dangote cement": "DANGCEM",
    "dangote sugar": "DANGSUGAR",
    "ecobank": "ETI",
    "eterna": "ETERNA",
    "e-tranzact": "ETRANZACT",
    "fcmb": "FCMB",
    "fbn holdings": "FBNH",
    "fidelity bank": "FIDELITYBK",
    "first holdco": "FIRSTHOLDCO",
    "fidson": "FIDSON",
    "flour mills": "FLOURMILL",
    "food concepts": "FOODCONC",
    "geregu power": "GEREGU",
    "gtco": "GTCO",
    "guaranty trust": "GTCO",
    "guinness nigeria": "GUINNESS",
    "honeywell flour": "HONYFLOUR",
    "ikeja hotel": "IKEJAHOTEL",
    "infinity trust": "INFINITYTRUST",
    "japaul gold": "JAPAULGOLD",
    "julius berger": "JBERGER",
    "lafarge africa": "WAPCO",
    "learn africa": "LEARNAFRCA",
    "mcnichols": "MCNICHOLS",
    "mecure industries": "MECURE",
    "mtnn": "MTNN",
    "mtn nigeria": "MTNN",
    "mutual benefits": "MBENEFIT",
    "nahco": "NAHCO",
    "nascon": "NASCON",
    "nem insurance": "NEM",
    "ngx group": "NGXGROUP",
    "nigerian exchange group": "NGXGROUP",
    "okomu oil": "OKOMUOIL",
    "presco": "PRESCO",
    "pz cussons": "PZ",
    "red star express": "REDSTAREX",
    "seplat": "SEPLAT",
    "sfs reit": "SFSREIT",
    "stanbic ibtc": "STANBIC",
    "the initiates": "TIP",
    "tip": "TIP",
    "transcorp hotels": "TRANSCOHOT",
    "transcorp power": "TRANSPOWER",
    "transcorp": "TRANSCORP",
    "uac of nigeria": "UACN",
    "uacn": "UACN",
    "uba": "UBA",
    "united bank for africa": "UBA",
    "ucap": "UCAP",
    "united capital": "UCAP",
    "unilever nigeria": "UNILEVER",
    "university press": "UPL",
    "updc real estate": "UPDCREIT",
    "vfd group": "VFDGROUP",
    "wema bank": "WEMABANK",
    "zenith bank": "ZENITHBANK",
}


def _get(url: str, timeout=(8, 15)) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r
    except Exception:
        pass
    return None


def _resolve_ticker(company_name: str) -> str:
    """Map TRW company name to NGX ticker."""
    name = company_name.lower().strip()
    # Try exact match first
    if name in TRW_COMPANY_TO_TICKER:
        return TRW_COMPANY_TO_TICKER[name]
    # Try partial match
    for key, ticker in TRW_COMPANY_TO_TICKER.items():
        if key in name or name in key:
            return ticker
    return ""


def _parse_date(date_str: str) -> Optional[str]:
    """Parse TRW date string to ISO format. Returns None if ambiguous."""
    if not date_str:
        return None
    s = date_str.strip().lower()
    if s in ("tba", "n/a", "na", "", "nil"):
        return None

    # Pattern: "27 May 2026" or "27 may 2026"
    m = re.match(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", s)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2), int(m.group(3))
        month = MONTHS.get(month_str[:3])
        if month and 1 <= day <= 31 and 2024 <= year <= 2030:
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                pass

    # Pattern: "May 2026" — month only, not specific enough
    m2 = re.match(r"([a-z]+)\s+(\d{4})", s)
    if m2:
        return None  # Too vague

    return None


def _parse_amount(amount_str: str) -> tuple[float, str]:
    """Parse TRW dividend amount. Returns (amount, currency)."""
    s = amount_str.strip()
    currency = "NGN"

    if s.startswith("$") or "usd" in s.lower():
        currency = "USD"
        s = s.replace("$", "").strip()
    else:
        s = s.replace("₦", "").replace("N", "").strip()

    # Remove "total", "final", etc.
    s = re.sub(r"\s*(total|final|interim|per share|each).*", "", s, flags=re.I)
    s = s.split("(")[0].strip()

    try:
        return float(s), currency
    except ValueError:
        return 0.0, currency


def _extract_doclib_pdfs_from_text(text: str) -> list[str]:
    """Extract all doclib PDF URLs from HTML text."""
    normalized = text.replace("\\/", "/").replace("\\u002F", "/")
    return list(set(PDF_URL_RE.findall(normalized)))


def _find_all_dividend_table_urls() -> list:
    """Find ALL TRW NGX Dividend Table post URLs for the current year."""
    year = date.today().year
    urls = []
    
    # Search for all table versions
    search_queries = [
        f"https://trwsb.wordpress.com/?s=NGX+dividend+bonus+table+{year}",
        f"https://trwsb.wordpress.com/?s=NGX+dividend+table+{year}",
        TRW_DIVIDEND_TABLE_SEARCH,
    ]
    
    seen = set()
    for search_url in search_queries:
        r = _get(search_url)
        if not r:
            continue
        
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href in seen:
                continue
            title = (a.get_text() or "").lower()
            if (
                "trwsb.wordpress.com" in href
                and str(year) in href
                and ("dividend" in href.lower() or "dividend" in title)
                and ("table" in href.lower() or "table" in title or "bonus" in href.lower())
            ):
                urls.append(href)
                seen.add(href)
    
    print(f"[TRW] Found {len(urls)} dividend table versions", flush=True)
    return urls


def _parse_mansa_dividend_page(html: str) -> dict:
    """
    Parse Mansa Markets dividend page for NGX company dates.
    Returns dict: {ticker: [{qualification_date, payment_date, dividend_per_share, currency, type}]}
    """
    results = {}
    soup = BeautifulSoup(html, "html.parser")
    
    # Mansa Markets uses a table with Company, Ticker, Dividend, Ex-date, Paid columns
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            # Try first row as header
            first_row = table.find("tr")
            if first_row:
                headers = [td.get_text(strip=True).lower() for td in first_row.find_all(["th", "td"])]
        
        col_company = col_ticker = col_amount = col_exdate = col_pay = -1
        for i, h in enumerate(headers):
            if "company" in h:
                col_company = i
            elif "ticker" in h or "symbol" in h:
                col_ticker = i
            elif "dividend" in h or "amount" in h:
                col_amount = i
            elif "ex" in h and "date" in h or "ex-date" in h or "qual" in h:
                col_exdate = i
            elif "pay" in h or "paid" in h:
                col_pay = i
        
        if col_company == -1 and col_ticker == -1:
            continue
        
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells:
                continue
            
            # Get ticker
            ticker = ""
            if col_ticker >= 0 and col_ticker < len(cells):
                ticker = cells[col_ticker].upper().strip()
            elif col_company >= 0 and col_company < len(cells):
                company = cells[col_company].lower().strip()
                # Try Mansa mapping first, then TRW mapping
                ticker = MANSA_COMPANY_TO_TICKER.get(company, "")
                if not ticker:
                    ticker = _resolve_ticker(company)
            
            if not ticker:
                continue
            
            # Get amount
            amount_str = cells[col_amount] if col_amount >= 0 and col_amount < len(cells) else ""
            amount, currency = _parse_amount(amount_str)
            if amount <= 0:
                continue
            
            # Get dates
            qual_str = cells[col_exdate] if col_exdate >= 0 and col_exdate < len(cells) else ""
            pay_str = cells[col_pay] if col_pay >= 0 and col_pay < len(cells) else ""
            
            qual_date = _parse_date(qual_str)
            pay_date = _parse_date(pay_str)
            
            if not qual_date and not pay_date:
                continue
            
            entry = {
                "dividend_per_share": amount,
                "currency": currency,
                "type": "final",
            }
            if qual_date:
                entry["qualification_date"] = qual_date
            if pay_date:
                entry["payment_date"] = pay_date
            
            if ticker not in results:
                results[ticker] = []
            
            existing = [e for e in results[ticker] if abs(e.get("dividend_per_share", 0) - amount) < 0.01]
            if not existing:
                results[ticker].append(entry)
    
    return results


def _parse_dividend_table(html: str) -> dict:
    """
    Parse TRW's NGX Dividend & Bonus Table HTML.
    Returns dict: {ticker: [{qualification_date, payment_date, dividend_per_share, currency, type}]}
    """
    results = {}
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]

        # Find column indices
        col_company = col_dividend = col_qual = col_pay = col_type = -1
        for i, h in enumerate(headers):
            if "company" in h:
                col_company = i
            elif "dividend" in h:
                col_dividend = i
            elif "qualification" in h or "qual" in h:
                col_qual = i
            elif "payment" in h or "pay" in h:
                col_pay = i
            elif "type" in h:
                col_type = i

        if col_company == -1 or col_qual == -1:
            continue

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells or len(cells) <= max(col_company, col_qual):
                continue

            company = cells[col_company] if col_company < len(cells) else ""
            if not company or company.isdigit():
                continue

            ticker = _resolve_ticker(company)
            if not ticker:
                continue

            div_str = cells[col_dividend] if col_dividend >= 0 and col_dividend < len(cells) else ""
            qual_str = cells[col_qual] if col_qual < len(cells) else ""
            pay_str = cells[col_pay] if col_pay >= 0 and col_pay < len(cells) else ""
            type_str = cells[col_type].lower() if col_type >= 0 and col_type < len(cells) else "final"

            amount, currency = _parse_amount(div_str)
            qual_date = _parse_date(qual_str)
            pay_date = _parse_date(pay_str)

            if not qual_date and not pay_date:
                continue
            if amount <= 0:
                continue

            entry = {
                "dividend_per_share": amount,
                "currency": currency,
                "type": "interim" if "interim" in type_str else "final",
            }
            if qual_date:
                entry["qualification_date"] = qual_date
            if pay_date:
                entry["payment_date"] = pay_date

            if ticker not in results:
                results[ticker] = []

            # Avoid duplicates
            existing = [
                e for e in results[ticker]
                if abs(e.get("dividend_per_share", 0) - amount) < 0.001
                and e.get("currency") == currency
            ]
            if not existing:
                results[ticker].append(entry)

    return results


def _update_known_dates(new_data: dict) -> int:
    """
    Update known_dates.json with new data from TRW.
    Only adds new entries — never overwrites existing ones.
    Returns count of new entries added.
    """
    existing = {}
    if KNOWN_DATES_FILE.exists():
        try:
            existing = json.loads(KNOWN_DATES_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    # Preserve metadata
    metadata = {k: v for k, v in existing.items() if k.startswith("_")}
    data = {k: v for k, v in existing.items() if not k.startswith("_")}

    added = 0
    for ticker, entries in new_data.items():
        if ticker not in data:
            data[ticker] = []

        for entry in entries:
            amount = entry.get("dividend_per_share", 0)
            existing_amounts = [
                e.get("dividend_per_share", 0)
                for e in data[ticker]
            ]
            # Only add if not already present
            if not any(abs(a - amount) < 0.001 for a in existing_amounts):
                data[ticker].append(entry)
                added += 1
            else:
                # Update missing dates in existing entry
                for e in data[ticker]:
                    if abs(e.get("dividend_per_share", 0) - amount) < 0.001:
                        changed = False
                        if not e.get("qualification_date") and entry.get("qualification_date"):
                            e["qualification_date"] = entry["qualification_date"]
                            changed = True
                        if not e.get("payment_date") and entry.get("payment_date"):
                            e["payment_date"] = entry["payment_date"]
                            changed = True
                        if changed:
                            added += 1

    # Write back
    output = {
        "_comment": "Auto-generated from TRW Stockbrokers NGX Dividend Table. Do not edit manually.",
        "_sources": ["TRW Stockbrokers (trwsb.wordpress.com) — licensed NGX broker"],
        "_last_updated": date.today().isoformat(),
        **data,
    }

    KNOWN_DATES_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return added


def discover_pdfs_from_trw(known_urls: set, debug: dict, started: float) -> list:
    """
    Patch 49 — Discovery: Scan recent TRW daily disclosure posts for
    doclib PDF links not yet in the archive.

    TRW posts daily summaries of NGX corporate disclosures with direct
    links to the official doclib PDFs. This catches declarations that
    AbokiForex and Kobo Terminal may have missed.
    """
    import time as _time

    found = []
    dbg = {"posts_checked": 0, "new_pdfs": 0, "errors": []}

    print("[TRW] Starting disclosure discovery", flush=True)

    try:
        # Search for recent disclosure summary posts
        r = _get(TRW_DISCLOSURE_SEARCH)
        if not r:
            print("[TRW] Could not reach search page", flush=True)
            debug["trw_discovery"] = dbg
            return found

        soup = BeautifulSoup(r.text, "html.parser")

        # Find post links from 2026
        post_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if (
                "trwsb.wordpress.com/2026/" in href
                and "disclosure" in href.lower()
            ):
                post_links.append(href)

        post_links = list(dict.fromkeys(post_links))[:10]  # max 10 recent posts

        print(f"[TRW] Found {len(post_links)} disclosure posts to check", flush=True)

        for url in post_links:
            remaining = 200 - (time.monotonic() - started)
            if remaining <= 10:
                break

            r2 = _get(url)
            if not r2:
                continue

            dbg["posts_checked"] += 1

            for pdf_url in _extract_doclib_pdfs_from_text(r2.text):
                if pdf_url in known_urls:
                    continue
                from .discover import _title_from_url, _looks_strongly_irrelevant
                title = _title_from_url(pdf_url)
                if _looks_strongly_irrelevant(title, pdf_url):
                    continue
                found.append({"url": pdf_url, "title": title, "source": "trw_disclosure"})
                known_urls.add(pdf_url)

            _time.sleep(1)  # Be respectful

    except Exception as exc:
        dbg["errors"].append(repr(exc))

    dbg["new_pdfs"] = len(found)
    debug["trw_discovery"] = dbg
    print(f"[TRW] {len(found)} new PDFs found from disclosures", flush=True)
    return found


def update_known_dates_from_trw(debug: dict) -> int:
    """
    Patch 49b — Date filling: Fetch ALL TRW NGX Dividend Table versions
    plus Mansa Markets dividend list and update known_dates.json.

    Fetches multiple sources:
    1. All TRW dividend table versions (April 8, April 23, and any updates)
    2. Mansa Markets comprehensive NGX dividend list

    Only fills gaps — never overwrites dates extracted from official NGX PDFs.
    """
    print("[TRW] Updating known_dates.json from all sources", flush=True)

    total_added = 0
    all_new_data = {}

    # 1. Fetch ALL TRW dividend table versions
    table_urls = _find_all_dividend_table_urls()

    for table_url in table_urls:
        print(f"[TRW] Fetching: {table_url}", flush=True)
        r = _get(table_url)
        if not r:
            continue

        new_data = _parse_dividend_table(r.text)
        print(f"[TRW] Parsed {len(new_data)} companies from {table_url.split('/')[-1]}", flush=True)

        # Merge into all_new_data
        for ticker, entries in new_data.items():
            if ticker not in all_new_data:
                all_new_data[ticker] = []
            for entry in entries:
                existing_amounts = [e.get("dividend_per_share", 0) for e in all_new_data[ticker]]
                if not any(abs(a - entry.get("dividend_per_share", 0)) < 0.01 for a in existing_amounts):
                    all_new_data[ticker].append(entry)

    # 2. Fetch Mansa Markets dividend list
    print("[TRW] Fetching Mansa Markets dividend list", flush=True)
    r_mansa = _get(MANSA_DIVIDEND_URL)
    if r_mansa:
        mansa_data = _parse_mansa_dividend_page(r_mansa.text)
        print(f"[TRW] Parsed {len(mansa_data)} companies from Mansa Markets", flush=True)
        for ticker, entries in mansa_data.items():
            if ticker not in all_new_data:
                all_new_data[ticker] = []
            for entry in entries:
                existing_amounts = [e.get("dividend_per_share", 0) for e in all_new_data[ticker]]
                if not any(abs(a - entry.get("dividend_per_share", 0)) < 0.01 for a in existing_amounts):
                    all_new_data[ticker].append(entry)

    # Update known_dates.json with all collected data
    if all_new_data:
        total_added = _update_known_dates(all_new_data)
        print(f"[TRW] Updated known_dates.json: {total_added} entries added/updated from {len(all_new_data)} companies", flush=True)
    else:
        print("[TRW] No new data found from any source", flush=True)

    debug["trw_known_dates"] = {
        "status": "ok",
        "table_versions_found": len(table_urls),
        "total_companies": len(all_new_data),
        "entries_added": total_added,
    }
    return total_added

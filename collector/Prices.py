"""
collector/prices.py — Patch 55

Publish NGX prices as a CSV the tracker can read.

Why this exists: Google Sheets' IMPORTHTML fetcher is blocked or defeated
by most modern market-data sites (no browser User-Agent, no JS). Three
price sources have now failed in the sheet while remaining perfectly
readable from Python. Scraping from inside the spreadsheet puts a fragile
dependency in every buyer's copy, where a break is silent.

So the pipeline fetches prices, validates them, and writes docs/prices.csv.
The tracker reads that file with IMPORTDATA from raw.githubusercontent.com
— the same mechanism the dividend feed already uses.

Sources are tried in order and the first one passing validation wins.
Adding or repairing a source is a change to this file only; no buyer
touches anything.

Output: docs/prices.csv
    ticker,company,price,updated_at
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
PRICES_CSV = ROOT / "docs" / "prices.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

KOBO_LISTED = "https://koboterminal.com/ngx-listed-companies"
KOBO_HOME = "https://koboterminal.com/"

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")
STOCK_HREF_RE = re.compile(r"/stocks/([A-Z][A-Z0-9]{1,11})$", re.I)

# A good scrape returns most of the market. NGX lists ~147 equities.
MIN_ROWS = 100


def _get(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=(10, 30))
        return r if r.status_code == 200 else None
    except Exception:
        return None


def _to_price(text: str) -> float:
    """'₦1,034.00' -> 1034.0 ; returns 0.0 when not a price."""
    s = (text or "").strip()
    s = s.replace("\u20a6", "").replace("N", "").replace(",", "").strip()
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in {"-", "."}:
        return 0.0
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return v if v > 0 else 0.0


def _from_table_pages(html: str) -> dict:
    """
    Generic table reader. Finds the ticker either from a /stocks/<TICKER>
    link in the row or from a cell that looks like a bare ticker, then
    takes the first positive money-looking value in that row that is not
    the market cap (which carries a B/T/M suffix).
    """
    out = {}
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            ticker = ""
            company = ""
            ticker_idx = -1

            link = tr.find("a", href=STOCK_HREF_RE)
            if link:
                m = STOCK_HREF_RE.search(link.get("href", ""))
                if m:
                    ticker = m.group(1).upper()
                    holder = link.find_parent(["td", "th"])
                    if holder in cells:
                        ticker_idx = cells.index(holder)
                    cell_text = " ".join(holder.get_text(" ", strip=True).split()) if holder else ""
                    company = cell_text.replace(link.get_text(strip=True), "", 1).strip(" -\u2013\u2014")
            else:
                for idx in (0, 1):
                    if idx < len(cells):
                        t = cells[idx].get_text(strip=True).upper()
                        if TICKER_RE.match(t):
                            ticker = t
                            ticker_idx = idx
                            other = cells[1 - idx].get_text(" ", strip=True) if len(cells) > 1 else ""
                            company = other
                            break

            if not ticker:
                continue

            # Price must come after the ticker cell — this skips the row-number
            # column. A cell carrying the naira sign wins outright; otherwise
            # take the first plain number that is not a percentage, a magnitude
            # (12.3B / 4.1K) or a share count.
            tail = cells[ticker_idx + 1:] if ticker_idx >= 0 else cells
            price = 0.0
            for c in tail:
                txt = c.get_text(strip=True)
                if not txt or "%" in txt or re.search(r"\d\s*[BTMK]\b", txt):
                    continue
                if "\u20a6" in txt:
                    price = _to_price(txt)
                    if price > 0:
                        break
            if price <= 0:
                for c in tail:
                    txt = c.get_text(strip=True)
                    if not txt or "%" in txt or re.search(r"\d\s*[BTMK]\b", txt):
                        continue
                    v = _to_price(txt)
                    if v > 0:
                        price = v
                        break

            if price > 0:
                out[ticker] = (company[:80], price)

    return out


def _source_kobo_listed():
    r = _get(KOBO_LISTED)
    return _from_table_pages(r.text) if r else {}


def _source_kobo_home():
    r = _get(KOBO_HOME)
    return _from_table_pages(r.text) if r else {}


SOURCES = (
    ("kobo_listed", _source_kobo_listed),
    ("kobo_home", _source_kobo_home),
)


def update_prices(debug: dict) -> int:
    """
    Fetch prices from the first source that validates and write docs/prices.csv.
    Returns the number of rows written. Never overwrites a good file with a
    bad scrape: if every source fails validation, the existing CSV is left
    untouched so the tracker keeps the last known prices.
    """
    dbg = {"source": None, "rows": 0, "attempts": [], "errors": []}
    print("[Prices] Fetching NGX prices", flush=True)

    chosen, data = None, {}
    for name, fn in SOURCES:
        try:
            got = fn()
        except Exception as exc:
            dbg["errors"].append(f"{name}: {exc!r}")
            got = {}
        dbg["attempts"].append({"source": name, "rows": len(got)})
        print(f"[Prices] {name}: {len(got)} tickers", flush=True)
        if len(got) >= MIN_ROWS:
            chosen, data = name, got
            break

    if not chosen:
        msg = f"no source returned >= {MIN_ROWS} tickers; keeping previous prices.csv"
        dbg["errors"].append(msg)
        debug["prices"] = dbg
        print(f"[Prices] {msg}", flush=True)
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    PRICES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with PRICES_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "company", "price", "updated_at"])
        for ticker in sorted(data):
            company, price = data[ticker]
            w.writerow([ticker, company, f"{price:.2f}", stamp])

    dbg["source"] = chosen
    dbg["rows"] = len(data)
    debug["prices"] = dbg
    print(f"[Prices] wrote {len(data)} rows from {chosen}", flush=True)
    return len(data)

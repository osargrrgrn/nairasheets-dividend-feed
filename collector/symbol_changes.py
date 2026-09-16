"""
collector/symbol_changes.py — Patch 54b

NGX ticker renames, learned from NGX's own filings.

When a company changes its trading symbol (WAPCO -> HBMNG, ACCESS ->
ACCESSCORP, FBNH -> FIRSTHOLDCO), every dividend already published under
the old symbol stops matching a portfolio holding the new one. The
dividend is real; the identifier moved.

NGX announces each change as a filing, e.g.
  47447_HBM_NIGERIA_PLC-NOTICE_OF_CHANGE_IN_TRADING_SYMBOL_...pdf

This module finds those filings through the SharePoint REST API (two
server-side name filters), parses OLD -> NEW, and maintains
data/ticker_renames.json. feed_integrity applies the map at publication
time, so historical events re-key to the current symbol automatically and
collapse with any newer row for the same event.

Parsing notes, from the real wording of the WAPCO notice:

    "...the Company's trading symbol on the floor of The Exchange has
     been changed from WAPCO to HBMNG"

- "symbol" and "from" are fourteen words apart, so they cannot be
  required to be adjacent; we search a window after each "symbol".
- The same sentence contains "corporate name from Lafarge Africa Plc to
  HBM Nigeria Plc". Ticker groups are therefore matched CASE-SENSITIVELY
  in uppercase, which admits WAPCO and HBMNG while rejecting Lafarge.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
RENAMES_FILE = ROOT / "data" / "ticker_renames.json"
ALIASES_DYNAMIC = ROOT / "data" / "aliases_dynamic.json"

BASE = "https://doclib.ngxgroup.com"
API = f"{BASE}/_api/web/GetFolderByServerRelativeUrl('/Financial_NewsDocs')/Files"
HEADERS_JSON = {
    "Accept": "application/json;odata=verbose",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}

NAME_FILTERS = ("SYMBOL", "CHANGE_OF_NAME")

RENAME_NAME_RE = re.compile(
    r"CHANGE[_ ](?:IN|OF)[_ ](?:TRADING[_ ])?(?:SYMBOL|NAME)|"
    r"(?:TRADING[_ ])?SYMBOL[_ ]CHANGE|TICKER[_ ]CHANGE|RE[_ ]?NAMING",
    re.I,
)

_T = r"([A-Z][A-Z0-9]{1,11})"
_Q = "[\"'\u201c\u201d]?"

SYMBOL_WORD_RE = re.compile(r"(?i:\b(?:trading\s+|ticker\s+)?symbol\b)")
WINDOW = 320

FROM_TO_RE = re.compile(
    r"(?i:from)\s+" + _Q + _T + _Q + r"\s+(?i:to)\s+" + _Q + _T + _Q
)
REPLACED_RE = re.compile(_T + r"[\s,]+(?i:has\s+replaced)\s+" + _T)
OLD_NEW_RE = re.compile(
    r"(?i:old)\s+(?i:trading\s+)?(?i:symbol)\s*[:\-]?\s*" + _Q + _T + _Q
    + r".{0,160}?(?i:new)\s+(?i:trading\s+)?(?i:symbol)\s*[:\-]?\s*" + _Q + _T + _Q,
    re.S,
)

_STOPWORDS = {
    "NGX", "PLC", "LTD", "THE", "AND", "FOR", "CBN", "SEC", "AGM", "EGM",
    "PDF", "RC", "NIL", "NOT", "NEW", "OLD", "ALL", "ANY", "WITH", "FROM",
    "THIS", "THAT", "DATE", "NAME", "CODE", "LIST", "NOTE", "PAGE", "LAGOS",
    "NIGERIA", "LIMITED", "COMPANY", "SYMBOL", "TICKER", "TRADING", "SHARES",
    "EFFECT", "CHANGE", "NOTICE", "PUBLIC", "MEMBERS", "EXCHANGE", "FLOOR",
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
    "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
}


def _get(url, timeout=(10, 45)):
    try:
        r = requests.get(url, headers=HEADERS_JSON, timeout=timeout)
        return r if r.status_code == 200 else None
    except Exception:
        return None


def _listed_tickers() -> set:
    """Current NGX symbols, from the Kobo-derived alias cache (if present)."""
    try:
        data = json.loads(ALIASES_DYNAMIC.read_text(encoding="utf-8"))
        return {str(v).upper() for v in data.values()}
    except Exception:
        return set()


def _load_renames() -> dict:
    try:
        raw = json.loads(RENAMES_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        return {}


def _load_seen() -> set:
    try:
        raw = json.loads(RENAMES_FILE.read_text(encoding="utf-8"))
        return set(raw.get("_sources") or [])
    except Exception:
        return set()


def _save_renames(mapping: dict, seen: set) -> None:
    RENAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "_comment": "OLD -> NEW NGX trading symbols, parsed from NGX change-of-symbol filings. Auto-generated.",
        "_last_updated": date.today().isoformat(),
        "_sources": sorted(seen)[-500:],
        **dict(sorted(mapping.items())),
    }
    RENAMES_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")


def _plausible(old: str, new: str, listed: set) -> bool:
    old, new = (old or "").upper(), (new or "").upper()
    if not old or not new or old == new:
        return False
    if old in _STOPWORDS or new in _STOPWORDS:
        return False
    if len(old) < 2 or len(new) < 2:
        return False
    # When the current listing is known, the NEW symbol must appear in it.
    # The OLD symbol is deliberately not tested: a listing can lag a rename
    # by weeks and still carry the retired symbol, which would otherwise
    # block the very mapping we need.
    if listed and new not in listed:
        return False
    return True


def _extract_pair(text: str, listed: set):
    """
    Return (old_symbol, new_symbol) or None. Only looks near the word
    "symbol", so a corporate-name change in the same document cannot be
    mistaken for a ticker change.
    """
    text = text or ""

    m = OLD_NEW_RE.search(text)          # tabulated form, unambiguous
    if m and _plausible(m.group(1), m.group(2), listed):
        return m.group(1).upper(), m.group(2).upper()

    for sm in SYMBOL_WORD_RE.finditer(text):
        window = text[sm.start(): sm.start() + WINDOW]

        m = FROM_TO_RE.search(window)
        if m and _plausible(m.group(1), m.group(2), listed):
            return m.group(1).upper(), m.group(2).upper()

        m = REPLACED_RE.search(window)   # groups are (new, old)
        if m and _plausible(m.group(2), m.group(1), listed):
            return m.group(2).upper(), m.group(1).upper()

    return None


def update_ticker_renames(debug: dict) -> int:
    """Find NGX change-of-symbol filings, parse OLD -> NEW, persist the map."""
    dbg = {"candidates": 0, "parsed": 0, "new": 0, "errors": []}
    print("[Renames] Scanning NGX filings for trading-symbol changes", flush=True)

    known = _load_renames()
    seen_urls = _load_seen()
    listed = _listed_tickers()

    candidates = {}
    for needle in NAME_FILTERS:
        url = (
            f"{API}?$select=Name,ServerRelativeUrl,TimeCreated"
            f"&$filter=substringof('{needle}',Name)&$top=5000"
        )
        r = _get(url)
        if r is None:
            dbg["errors"].append(f"filter {needle} failed")
            continue
        try:
            rows = r.json().get("d", {}).get("results", [])
        except Exception:
            continue
        for row in rows:
            name = row.get("Name") or ""
            if not name.lower().endswith(".pdf"):
                continue
            if not RENAME_NAME_RE.search(name):
                continue
            path = row.get("ServerRelativeUrl") or ""
            candidates[BASE + path.replace(" ", "%20")] = name

    dbg["candidates"] = len(candidates)
    print(f"[Renames] {len(candidates)} change-of-symbol filings found", flush=True)
    if candidates:
        print("[Renames] candidates: " + "; ".join(
            n[:60] for n in list(candidates.values())[:8]), flush=True)

    from .pdf_extract import download_pdf_text

    learned = {}
    for url, name in list(candidates.items())[:25]:
        if url in seen_urls:
            continue
        try:
            text = download_pdf_text(url)
        except Exception as exc:
            dbg["errors"].append(f"{name[:40]}: {exc!r}")
            continue
        seen_urls.add(url)
        pair = _extract_pair(text, listed)
        if not pair:
            snippet = ""
            m = SYMBOL_WORD_RE.search(text or "")
            if m:
                snippet = " ".join(text[m.start(): m.start() + 180].split())
            print(f"[Renames] no mapping in {name[:55]}"
                  + (f" | near 'symbol': {snippet}" if snippet else " | no 'symbol' in text"),
                  flush=True)
            continue
        old, new = pair
        dbg["parsed"] += 1
        if known.get(old) != new:
            learned[old] = new
            print(f"[Renames] {old} -> {new}   ({name[:60]})", flush=True)

    known.update(learned)
    _save_renames(known, seen_urls)

    dbg["new"] = len(learned)
    debug["ticker_renames"] = dbg
    print(f"[Renames] {len(learned)} new mapping(s); {len(known)} total", flush=True)
    return len(learned)


def canonical_ticker(ticker: str) -> str:
    """Follow the rename chain to the current NGX symbol (cycle-safe)."""
    t = (ticker or "").upper().strip()
    if not t:
        return t
    mapping = _load_renames()
    seen = {t}
    while t in mapping:
        t = str(mapping[t]).upper()
        if t in seen:
            break
        seen.add(t)
    return t

"""
collector/symbol_changes.py — Patch 54

NGX ticker renames, learned from NGX's own filings.

When a company changes its trading symbol (Lafarge Africa -> HBM,
Access Bank -> ACCESSCORP, FBN Holdings -> FIRSTHOLDCO), every dividend
already published under the old symbol stops matching a portfolio that
now holds the new one. The dividend is still real; the identifier moved.

NGX announces each change as a filing, e.g.
  47447_HBM_NIGERIA_PLC-NOTICE_OF_CHANGE_IN_TRADING_SYMBOL_...pdf

This module finds those filings via the SharePoint REST API (server-side
name filter, so it costs two requests), parses OLD -> NEW out of the text,
and maintains data/ticker_renames.json. feed_integrity applies the map at
publication time, so historical events are re-keyed to the current symbol
automatically and collapse with any newer rows for the same event.

No hand-maintained list. New renames are picked up the run after NGX
files them.
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

# Server-side name filters. Two calls, both cheap.
NAME_FILTERS = ("SYMBOL", "CHANGE_OF_NAME")

# Local confirmation that a filing really is a rename notice.
RENAME_NAME_RE = re.compile(
    r"CHANGE[_ ](?:IN|OF)[_ ](?:TRADING[_ ])?(?:SYMBOL|NAME)|"
    r"(?:TRADING[_ ])?SYMBOL[_ ]CHANGE|TICKER[_ ]CHANGE|RE[_ ]?NAMING",
    re.I,
)

# Ticker-shaped token: NGX symbols are 2-12 chars, upper alnum.
_T = r"([A-Z][A-Z0-9]{1,11})"

# Ordered most-specific first.
PATTERNS = (
    re.compile(rf"symbol\s+from\s+[\"'\u201c]?{_T}[\"'\u201d]?\s+to\s+[\"'\u201c]?{_T}[\"'\u201d]?", re.I),
    re.compile(rf"from\s+[\"'\u201c]?{_T}[\"'\u201d]?\s+to\s+[\"'\u201c]?{_T}[\"'\u201d]?\s+with\s+effect", re.I),
    re.compile(rf"old\s+(?:trading\s+)?symbol[:\s]+[\"'\u201c]?{_T}[\"'\u201d]?.{{0,120}}?new\s+(?:trading\s+)?symbol[:\s]+[\"'\u201c]?{_T}[\"'\u201d]?", re.I | re.S),
    re.compile(rf"formerly\s+(?:known\s+as\s+)?[\"'\u201c]?{_T}[\"'\u201d]?.{{0,80}}?now\s+[\"'\u201c]?{_T}[\"'\u201d]?", re.I | re.S),
)

# Words that look like tickers but never are.
_STOPWORDS = {
    "NGX", "PLC", "LTD", "THE", "AND", "FOR", "CBN", "SEC", "AGM", "EGM",
    "PDF", "RC", "NIL", "NOT", "NEW", "OLD", "ALL", "ANY", "WITH", "FROM",
    "THIS", "THAT", "DATE", "NAME", "CODE", "LIST", "NOTE", "PAGE", "LAGOS",
    "NIGERIA", "LIMITED", "COMPANY", "SYMBOL", "TICKER", "TRADING", "SHARES",
    "EFFECT", "CHANGE", "NOTICE", "PUBLIC", "MEMBERS", "EXCHANGE",
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


def _save_renames(mapping: dict) -> None:
    RENAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "_comment": "OLD -> NEW NGX trading symbols, parsed from NGX change-of-symbol filings. Auto-generated.",
        "_last_updated": date.today().isoformat(),
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
    # If we know the current listing, the NEW symbol should be in it and the
    # OLD one should not. When the listing is unavailable, accept the pair.
    if listed:
        if new not in listed:
            return False
        if old in listed:
            return False
    return True


def _extract_pair(text: str, listed: set):
    for pat in PATTERNS:
        m = pat.search(text or "")
        if m:
            old, new = m.group(1), m.group(2)
            if _plausible(old, new, listed):
                return old.upper(), new.upper()
    return None


def update_ticker_renames(debug: dict) -> int:
    """
    Find NGX change-of-symbol filings, parse OLD -> NEW, persist the map.
    Returns the number of new mappings learned this run.
    """
    dbg = {"candidates": 0, "parsed": 0, "new": 0, "errors": []}
    print("[Renames] Scanning NGX filings for trading-symbol changes", flush=True)

    known = _load_renames()
    seen_urls = set(known.get("_sources", []) if isinstance(known.get("_sources"), list) else [])
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

    from .pdf_extract import download_pdf_text

    learned = {}
    for url, name in list(candidates.items())[:25]:      # cap work per run
        if url in seen_urls:
            continue
        try:
            text = download_pdf_text(url)
        except Exception as exc:
            dbg["errors"].append(f"{name[:40]}: {exc!r}")
            continue
        pair = _extract_pair(text, listed)
        seen_urls.add(url)
        if not pair:
            continue
        old, new = pair
        dbg["parsed"] += 1
        if known.get(old) != new:
            learned[old] = new
            print(f"[Renames] {old} -> {new}   ({name[:60]})", flush=True)

    if learned:
        known.update(learned)
        known["_sources"] = sorted(seen_urls)[-500:]
        _save_renames(known)
    elif seen_urls:
        known["_sources"] = sorted(seen_urls)[-500:]
        _save_renames(known)

    dbg["new"] = len(learned)
    debug["ticker_renames"] = dbg
    print(f"[Renames] {len(learned)} new mapping(s); {len(_load_renames())} total", flush=True)
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

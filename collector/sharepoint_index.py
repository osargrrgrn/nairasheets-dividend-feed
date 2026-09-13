"""
collector/sharepoint_index.py — Patch 53d

Primary discovery source: NGX's own document library.

doclib.ngxgroup.com is a SharePoint site whose REST API is anonymously
readable (verified from GitHub Actions, Sept 2026):

  GET /_api/web/GetFolderByServerRelativeUrl('/Financial_NewsDocs')/Files
      ?$select=Name,ServerRelativeUrl,TimeCreated
      &$filter=TimeCreated ge datetime'YYYY-01-01T00:00:00Z'
      &$top=5000&$skip=N

This enumerates every file NGX has uploaded since the given date, straight
from the source. Aggregators (AbokiForex, NaijaTicker, TRW) only expose
recent filings; this exposes all of them.

Behaviour per run:
- Fetch all files created since 1 January of the previous year, paging by
  5000 (verified to work).
- Keep filenames that look dividend-related.
- Drop anything already in the archive or strongly irrelevant.
- Order so dedicated dividend / corporate-action announcements come first,
  then by document number ascending, because discover.py caps new PDFs per
  run and the parser processes them in that order.

The parser, reconciliation, integrity gate and publication rules are
unchanged. This module only supplies URLs.
"""

from __future__ import annotations

import re
import time
from datetime import date

import requests

BASE = "https://doclib.ngxgroup.com"
FOLDER = "/Financial_NewsDocs"
API = f"{BASE}/_api/web/GetFolderByServerRelativeUrl('{FOLDER}')/Files"

HEADERS = {
    "Accept": "application/json;odata=verbose",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
}

PAGE = 5000
MAX_PAGES = 10          # 50,000 files — library is ~30,000 today
TIMEOUT = (10, 90)

NUM_RE = re.compile(r"^(\d{3,6})_")

# NGX filenames: <number>_<COMPANY>-<TITLE>_CORPORATE_ACTIONS_<MONTH>_<YEAR>.pdf
# The trailing "_CORPORATE_ACTIONS_..." is a *category* present on almost every
# filing, so it must be stripped before judging relevance.
CATEGORY_SUFFIX_RE = re.compile(r"_?CORPORATE[_ ]ACTIONS?[_ ][A-Z]+[_ ]20\d\d(?:\.pdf)?$", re.I)

# Titles that are dividend events or contain the dividend decision.
TIER1_RE = re.compile(
    r"DIVIDEND|DISTRIBUTION|NGX[_ ]NOTIFICATION|QUALIFICATION|"
    r"CORPORATE[_ ]ACTIONS?[_ ]ANNOUNCEMENT|CORPORATE[_ ]ACTION[_ ](?:FY|20\d\d|\d{4})|"
    r"CORPORATE[_ ]ACTION$",
    re.I,
)
TIER2_RE = re.compile(
    r"AGM[_ ]RESOLUTION|RESOLUTIONS?[_ ]PASSED|RESOLUTIONS?[_ ](?:OF|AT)|OUTCOME[_ ]OF|"
    r"NOTICE[_ ]OF[_ ]DECISION|POST[_ ]BOARD|BOARD[_ ]APPROVAL|"
    # Generic "ANNOUNCEMENT" titles (Zenith, GTCO use these for dividends and
    # for everything else). Fetched after explicit dividend titles so they
    # never crowd out the budget; the parser discards the non-dividend ones.
    r"(?:^|[_ ])ANNOUNCEMENT(?:$|[_ ])",
    re.I,
)
# Never dividend events, whatever the category says.
NEGATIVE_RE = re.compile(
    r"BOARD[_ ]CHANGE|TRADING[_ ]SYMBOL|CLOSED?[_ ]PERIOD|RECONSTRUCTION|APPOINTMENT|"
    r"RESIGNATION|RETIREMENT|UNCLAIMED|TENDER[_ ]OFFER|GOVERNANCE|INSIDER|DEALING|"
    r"VOTING|LITIGATION|CHANGE[_ ]OF[_ ]NAME|BOARD[_ ]MEETING|NOTICE[_ ]OF[_ ]BOARD|"
    r"SUSTAINABILITY|COMPLAINT|WHISTLE|RIGHTS[_ ]ISSUE|BONUS[_ ]ISSUE|SHARE[_ ]BUY|"
    r"CAPITAL[_ ]MARKETS[_ ]DAY|PRESS[_ ]RELEASE|EARNINGS|FINANCIAL[_ ]STATEMENT|QUARTER",
    re.I,
)


def _title_of(name: str) -> str:
    base = re.sub(r"\.pdf$", "", name or "", flags=re.I)
    base = CATEGORY_SUFFIX_RE.sub("", base)
    # drop the leading "<number>_" and the company part before the first "-"
    base = re.sub(r"^\d{3,6}_", "", base)
    if "-" in base:
        base = base.split("-", 1)[1]
    return base.strip("_ -")


def _tier(name: str):
    """0 = dividend title, 1 = resolution/outcome, None = not a candidate."""
    title = _title_of(name)
    hay = title if title else name
    if NEGATIVE_RE.search(hay) and not re.search(r"DIVIDEND|DISTRIBUTION", hay, re.I):
        return None
    if TIER1_RE.search(hay):
        return 0
    if TIER2_RE.search(hay):
        return 1
    return None


def _since_iso() -> str:
    """1 January of the previous year — catches late declarations paying in the new year."""
    return f"{date.today().year - 1}-01-01T00:00:00Z"


def _to_url(server_relative: str) -> str:
    """
    Build the public URL from ServerRelativeUrl. Only spaces are encoded so
    the result matches the URL form already used across the archive
    (aggregators leave parentheses and commas unencoded).
    """
    path = (server_relative or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    return BASE + path.replace(" ", "%20")


def _doc_number(name: str) -> int:
    m = NUM_RE.match(name or "")
    return int(m.group(1)) if m else -1  # unnumbered files sort last (descending order)


def _fetch_page(session: requests.Session, since: str, skip: int):
    url = (
        f"{API}?$select=Name,ServerRelativeUrl,TimeCreated"
        f"&$filter=TimeCreated ge datetime'{since}'"
        f"&$top={PAGE}&$skip={skip}"
    )
    r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        return None, r.status_code
    try:
        return r.json().get("d", {}).get("results", []), 200
    except Exception:
        return None, r.status_code


def discover_from_sharepoint(known: set, debug: dict, started: float) -> list:
    """
    Patch 51 — enumerate NGX's document library directly.
    Returns [{"url","title","source"}] for new dividend-looking files.
    """
    dbg = {
        "since": _since_iso(),
        "pages": 0,
        "files_seen": 0,
        "dividend_like": 0,
        "already_known": 0,
        "irrelevant": 0,
        "new_pdfs": 0,
        "errors": [],
    }
    print(f"[SharePoint] Enumerating {FOLDER} since {dbg['since']}", flush=True)

    # Local import to avoid a circular import at module load.
    from .discover import _looks_strongly_irrelevant, _title_from_url

    candidates = []
    try:
        with requests.Session() as s:
            skip = 0
            for _ in range(MAX_PAGES):
                if time.monotonic() - started > 150:
                    dbg["errors"].append("time budget reached during paging")
                    break
                rows, status = _fetch_page(s, dbg["since"], skip)
                if rows is None:
                    dbg["errors"].append(f"HTTP {status} at skip={skip}")
                    break
                dbg["pages"] += 1
                dbg["files_seen"] += len(rows)

                for row in rows:
                    name = row.get("Name") or ""
                    if not name.lower().endswith(".pdf"):
                        continue
                    tier = _tier(name)
                    if tier is None:
                        continue
                    dbg["dividend_like"] += 1

                    url = _to_url(row.get("ServerRelativeUrl") or f"{FOLDER}/{name}")
                    if url in known:
                        dbg["already_known"] += 1
                        continue
                    title = _title_from_url(url)
                    if _looks_strongly_irrelevant(title, url):
                        dbg["irrelevant"] += 1
                        continue
                    candidates.append((name, url, title, row.get("TimeCreated") or "", tier))

                if len(rows) < PAGE:
                    break
                skip += PAGE
    except Exception as exc:
        dbg["errors"].append(repr(exc))

    # Tier-1 names first, then newest upload first (TimeCreated, ISO string),
    # so unnumbered filings such as NIDF_Q1_2026_... are ordered correctly.
    candidates.sort(key=lambda c: c[3], reverse=True)   # newest upload first
    candidates.sort(key=lambda c: c[4])                 # stable: dividend titles before resolutions

    found = []
    for name, url, title, _created, _tier_no in candidates:
        found.append({"url": url, "title": title, "source": "sharepoint_index"})
        known.add(url)

    dbg["new_pdfs"] = len(found)
    debug["sharepoint_index"] = dbg
    print(
        f"[SharePoint] files_seen={dbg['files_seen']} dividend_like={dbg['dividend_like']} "
        f"known={dbg['already_known']} irrelevant={dbg['irrelevant']} new={len(found)}",
        flush=True,
    )
    for name, _, _, _, _ in candidates[:8]:
        print(f"[SharePoint]   {name[:85]}", flush=True)
    return found

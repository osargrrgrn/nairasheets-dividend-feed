"""
collector/sharepoint_index.py — Patch 51

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

# Filenames worth handing to the parser. Case-insensitive.
DIVIDEND_NAME_RE = re.compile(
    r"DIVIDEND|DISTRIBUTION|CORPORATE[_ ]ACTION|NGX[_ ]NOTIFICATION|"
    r"AGM[_ ]RESOLUTION|RESOLUTIONS[_ ]PASSED|OUTCOME[_ ]OF|"
    r"NOTICE[_ ]OF[_ ]DECISION|QUALIFICATION",
    re.I,
)

# Highest-value names go first so they get parsed first.
TIER1_NAME_RE = re.compile(
    r"DIVIDEND[_ ]ANNOUNCEMENT|CORPORATE[_ ]ACTIONS?[_ ]ANNOUNCEMENT|"
    r"DISTRIBUTION[_ ]PAYMENT|NGX[_ ]NOTIFICATION|INTERIM[_ ]DIVIDEND|FINAL[_ ]DIVIDEND",
    re.I,
)


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
    return int(m.group(1)) if m else 10**9  # unnumbered files sort last


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
                    if not DIVIDEND_NAME_RE.search(name):
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
                    candidates.append((name, url, title))

                if len(rows) < PAGE:
                    break
                skip += PAGE
    except Exception as exc:
        dbg["errors"].append(repr(exc))

    # Tier-1 names first, then by document number ascending.
    candidates.sort(key=lambda c: (0 if TIER1_NAME_RE.search(c[0]) else 1, _doc_number(c[0])))

    found = []
    for name, url, title in candidates:
        found.append({"url": url, "title": title, "source": "sharepoint_index"})
        known.add(url)

    dbg["new_pdfs"] = len(found)
    debug["sharepoint_index"] = dbg
    print(
        f"[SharePoint] files_seen={dbg['files_seen']} dividend_like={dbg['dividend_like']} "
        f"known={dbg['already_known']} irrelevant={dbg['irrelevant']} new={len(found)}",
        flush=True,
    )
    for name, _, _ in candidates[:8]:
        print(f"[SharePoint]   {name[:85]}", flush=True)
    return found

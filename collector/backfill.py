"""
collector/backfill.py — Patch 31 / 56
One-time historical discovery backfills, one per year in BACKFILL_YEARS.

It discovers older official NGX PDFs through the same NaijaTicker stock pages
already used by the live collector. It does not publish anything itself.

To go further back, add a year to BACKFILL_YEARS. Each year runs once and is
then marked completed in collector_state.json (backfill_<year>_completed).
Dates before 2024-01-01 are still rejected by feed_integrity and reconcile.
"""

import concurrent.futures
import time

import requests

from .discover import (
    HEADERS,
    _fetch_naija,
    _load_naija_tickers,
    _looks_strongly_irrelevant,
    _naija_relevance_score,
    _title_from_url,
)

BACKFILL_YEARS = ("2026", "2024")
BACKFILL_WORKERS = 12

POSITIVE_HINTS = (
    "dividend",
    "distribution",
    "corporate_action",
    "corporate action",
    "agm",
    "annual_general_meeting",
    "annual general meeting",
    "post_board",
    "post board",
    "outcome_of_board",
    "outcome of board",
    "board_resolution",
    "board resolution",
    "earnings_release",
    "earnings release",
    "audited_results",
    "audited results",
)

NEGATIVE_HINTS = (
    "director_dealing",
    "director dealing",
    "closed_period",
    "close period",
    "total_voting",
    "total voting",
    "rights_issue",
    "rights issue",
    "private_placement",
    "private placement",
    "transaction_in_own_shares",
    "share_buyback",
)


def _eligible(url: str, year: str) -> bool:
    low = (url or "").lower()

    if year not in low:
        return False

    if any(term in low for term in NEGATIVE_HINTS):
        return False

    return (
        any(term in low for term in POSITIVE_HINTS)
        or _naija_relevance_score(url) > 0
    )


def discover_year_backfill(year, known_urls=None):
    known = set(known_urls or [])
    tickers = _load_naija_tickers()

    stats = {
        "companies_targeted": len(tickers),
        "companies_completed": 0,
        "pdfs_seen": 0,
        "already_known": 0,
        "wrong_year": 0,
        "filtered": 0,
        "new_candidates": 0,
        "errors": 0,
    }

    candidates = {}

    print(
        f"[Backfill {year}] scanning {len(tickers)} NaijaTicker company pages",
        flush=True,
    )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=BACKFILL_WORKERS
    ) as pool:
        futures = [pool.submit(_fetch_naija, ticker) for ticker in tickers]

        for fut in concurrent.futures.as_completed(futures):
            result = fut.result()
            stats["companies_completed"] += 1

            if result.get("error"):
                stats["errors"] += 1

            ticker = (result.get("ticker") or "").upper().strip()

            for url in result.get("pdfs", []):
                stats["pdfs_seen"] += 1

                if url in known:
                    stats["already_known"] += 1
                    continue

                if year not in url.lower():
                    stats["wrong_year"] += 1
                    continue

                title = _title_from_url(url)

                if _looks_strongly_irrelevant(title, url):
                    stats["filtered"] += 1
                    continue

                if not _eligible(url, year):
                    stats["filtered"] += 1
                    continue

                score = _naija_relevance_score(url)
                low = title.lower()

                if "dividend" in low or "distribution" in low:
                    score += 100
                elif "corporate action" in low:
                    score += 40
                elif "agm" in low or "annual general meeting" in low:
                    score += 30

                item = {
                    "url": url,
                    "title": title,
                    "source": f"naijaticker_{year}_backfill",
                    "ticker": ticker,
                    "_score": score,
                }

                current = candidates.get(url)
                if current is None or score > current["_score"]:
                    candidates[url] = item

    ranked = sorted(
        candidates.values(),
        key=lambda item: (-item["_score"], item["url"]),
    )

    items = []
    for item in ranked:
        item.pop("_score", None)
        items.append(item)

    stats["new_candidates"] = len(items)

    print(
        f"[Backfill {year}] "
        f"seen={stats['pdfs_seen']} "
        f"known={stats['already_known']} "
        f"wrong_year={stats['wrong_year']} "
        f"filtered={stats['filtered']} "
        f"new_candidates={stats['new_candidates']} "
        f"errors={stats['errors']}",
        flush=True,
    )

    return items, stats


def discover_2026_backfill(known_urls=None):
    return discover_year_backfill("2026", known_urls)


# ---------------------------------------------------------------------------
# NGX official disclosure API backfill
#
# NGX company pages are filled from a JSON feed that returns up to the 100
# newest disclosures per company, each with its upload date. This reaches
# back to 2024 (and earlier) for most companies, which the NaijaTicker pages
# (recent months only) cannot.
# ---------------------------------------------------------------------------

NGX_DISCLOSURE_API = (
    "https://ngxgroup.com/wp-json/ngx-data-widgets/v1/company/disclosures"
    "?isin=&symbol={symbol}&directory=companydirectory&family=company"
)

NGX_API_HINTS = (
    ("dividend", 100),
    ("distribution", 100),
    ("interim", 60),
    ("final_dividend", 40),
    ("agm", 30),
    ("annual_general", 30),
    ("resolution", 30),
    ("outcome_of_board", 25),
    ("board_meeting", 15),
    ("closure", 25),
    ("qualification", 25),
    ("bonus", 20),
    ("earnings_release", 10),
    ("audited", 10),
)


def _fetch_ngx_disclosures(symbol):
    url = NGX_DISCLOSURE_API.format(symbol=symbol)
    last = None

    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(5, 20))
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return symbol, data, None
                last = "unexpected payload"
            else:
                last = f"http {r.status_code}"
        except Exception as exc:
            last = repr(exc)
        time.sleep(1.5 * (attempt + 1))

    return symbol, [], last


def discover_ngx_api_backfill(year, known_urls=None):
    known = set(known_urls or [])
    tickers = _load_naija_tickers()

    stats = {
        "companies_targeted": len(tickers),
        "companies_ok": 0,
        "docs_seen": 0,
        "already_known": 0,
        "wrong_year": 0,
        "filtered": 0,
        "new_candidates": 0,
        "errors": 0,
    }

    candidates = {}

    print(
        f"[NGX-API {year}] reading disclosures for {len(tickers)} companies",
        flush=True,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_fetch_ngx_disclosures, t) for t in tickers]

        for fut in concurrent.futures.as_completed(futures):
            symbol, rows, error = fut.result()

            if error:
                stats["errors"] += 1
                continue

            stats["companies_ok"] += 1

            for row in rows:
                link = (row.get("URL") or {}).get("Url") or ""
                modified = row.get("Modified") or ""

                if not link.lower().endswith(".pdf"):
                    continue

                stats["docs_seen"] += 1

                if not modified.startswith(str(year)):
                    stats["wrong_year"] += 1
                    continue

                if link in known:
                    stats["already_known"] += 1
                    continue

                title = _title_from_url(link)

                if _looks_strongly_irrelevant(title, link):
                    stats["filtered"] += 1
                    continue

                low = link.lower()

                if any(term in low for term in NEGATIVE_HINTS):
                    stats["filtered"] += 1
                    continue

                score = sum(
                    weight for term, weight in NGX_API_HINTS if term in low
                )

                if score <= 0:
                    stats["filtered"] += 1
                    continue

                candidates[link] = {
                    "url": link,
                    "title": title,
                    "source": f"ngx_api_{year}_backfill",
                    "ticker": (symbol or "").upper(),
                    "_score": score,
                }

    if stats["companies_ok"] < max(1, stats["companies_targeted"] // 2):
        raise RuntimeError(
            f"NGX API unreachable for most companies "
            f"(ok={stats['companies_ok']}, errors={stats['errors']})"
        )

    ranked = sorted(
        candidates.values(), key=lambda i: (-i["_score"], i["url"])
    )

    items = []
    for item in ranked:
        item.pop("_score", None)
        items.append(item)

    stats["new_candidates"] = len(items)

    print(
        f"[NGX-API {year}] "
        f"companies_ok={stats['companies_ok']} errors={stats['errors']} "
        f"seen={stats['docs_seen']} known={stats['already_known']} "
        f"wrong_year={stats['wrong_year']} filtered={stats['filtered']} "
        f"new_candidates={stats['new_candidates']}",
        flush=True,
    )

    return items, stats

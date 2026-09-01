"""
collector/backfill.py — Patch 31
One-time 2026 historical discovery backfill.

It discovers older official NGX PDFs through the same NaijaTicker stock pages
already used by the live collector. It does not publish anything itself.
"""

import concurrent.futures

from .discover import (
    _fetch_naija,
    _load_naija_tickers,
    _looks_strongly_irrelevant,
    _naija_relevance_score,
    _title_from_url,
)

BACKFILL_YEAR = "2026"
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


def _eligible(url: str) -> bool:
    low = (url or "").lower()

    if BACKFILL_YEAR not in low:
        return False

    if any(term in low for term in NEGATIVE_HINTS):
        return False

    return (
        any(term in low for term in POSITIVE_HINTS)
        or _naija_relevance_score(url) > 0
    )


def discover_2026_backfill(known_urls=None):
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
        f"[Backfill 2026] scanning {len(tickers)} NaijaTicker company pages",
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

                if BACKFILL_YEAR not in url.lower():
                    stats["wrong_year"] += 1
                    continue

                title = _title_from_url(url)

                if _looks_strongly_irrelevant(title, url):
                    stats["filtered"] += 1
                    continue

                if not _eligible(url):
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
                    "source": "naijaticker_2026_backfill",
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
        "[Backfill 2026] "
        f"seen={stats['pdfs_seen']} "
        f"known={stats['already_known']} "
        f"wrong_year={stats['wrong_year']} "
        f"filtered={stats['filtered']} "
        f"new_candidates={stats['new_candidates']} "
        f"errors={stats['errors']}",
        flush=True,
    )

    return items, stats

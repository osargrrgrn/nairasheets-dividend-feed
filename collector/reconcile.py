"""
collector/reconcile.py — Patch 23

Cross-document dividend evidence reconciliation.

The NGX often publishes one corporate action across several official PDFs:
an earnings release may contain the amount, an AGM resolution the approval,
and a corporate-action notice the qualification/payment dates.

This module merges compatible official evidence conservatively. It never
allows an AGM-only event to become high-confidence without corroboration from
a stronger, non-AGM official source.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping, Optional


DATE_FIELDS = (
    "qualification_date",
    "payment_date",
    "closure_date",
    "announcement_date",
)


def source_kind(title: str) -> str:
    low = (title or "").lower().replace("_", " ")

    if "currency exchange rate" in low or "exchange rate" in low:
        return "exchange_rate"

    if (
        "annual general meeting" in low
        or "agm" in low
        or "agm resolution" in low
        or "notice of annual general meeting" in low
    ):
        return "agm"

    if (
        "financial statement" in low
        or "annual report" in low
        or "audited result" in low
        or "unaudited result" in low
        or "quarter 1" in low
        or "quarter 2" in low
        or "quarter 3" in low
        or "quarter 4" in low
    ):
        return "financial_statement"

    if (
        "corporate action" in low
        or "dividend announcement" in low
        or "distribution announcement" in low
        or "interim dividend" in low
        or "final dividend" in low
    ):
        return "corporate_action"

    if "earnings release" in low or "earnings press release" in low:
        return "earnings_release"

    if "board meeting" in low or "outcome of board" in low:
        return "board"

    return "other"


def source_strength(title: str) -> int:
    kind = source_kind(title)
    return {
        "corporate_action": 100,
        "earnings_release": 70,
        "board": 60,
        "other": 50,
        "agm": 35,
        "financial_statement": 25,
        "exchange_rate": 10,
    }.get(kind, 40)


def _float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _iso_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat((value or "").strip())
    except Exception:
        return None


def amounts_match(a, b) -> bool:
    a = _float(a)
    b = _float(b)

    if a <= 0 or b <= 0:
        return False

    # Tight tolerance: same dividend, not merely a nearby value.
    return abs(a - b) <= max(0.0001, 0.001 * max(abs(a), abs(b)))


def _normalise_type(value: str) -> str:
    low = (value or "").lower().strip()
    if low in {"", "dividend", "distribution"}:
        return ""
    return low


def compatible_type(a, b) -> bool:
    a = _normalise_type(a)
    b = _normalise_type(b)
    return not a or not b or a == b


def date_proximity_ok(a: Mapping, b: Mapping, max_days: int = 180) -> bool:
    pairs = []

    for field in DATE_FIELDS:
        da = _iso_date(a.get(field, ""))
        db = _iso_date(b.get(field, ""))
        if da and db:
            pairs.append(abs((da - db).days))

    # If both rows contain comparable dates, reject obviously different events.
    if pairs:
        return min(pairs) <= max_days

    # No comparable dates is not itself a rejection; amount/type/ticker still
    # provide useful evidence.
    return True


def event_match_score(candidate, row: Mapping) -> int:
    ticker = (getattr(candidate, "ticker", "") or "").upper().strip()
    row_ticker = (row.get("ticker") or "").upper().strip()

    if not ticker or ticker != row_ticker:
        return -1

    if not compatible_type(
        getattr(candidate, "dividend_type", ""),
        row.get("dividend_type", ""),
    ):
        return -1

    candidate_dict = {
        field: getattr(candidate, field, "")
        for field in DATE_FIELDS
    }

    if not date_proximity_ok(candidate_dict, row):
        return -1

    ca = _float(getattr(candidate, "dividend_per_share", 0))
    ra = _float(row.get("dividend_per_share"))

    score = 20

    if ca > 0 and ra > 0:
        if not amounts_match(ca, ra):
            return -1
        score += 120
    elif ca > 0 or ra > 0:
        score += 35

    for field in DATE_FIELDS:
        a = getattr(candidate, field, "")
        b = row.get(field, "")
        if a and b:
            if a == b:
                score += 12
            else:
                da, db = _iso_date(a), _iso_date(b)
                if da and db and abs((da - db).days) <= 7:
                    score += 5
        elif a or b:
            score += 2

    score += source_strength(row.get("source_title", "")) // 10
    return score


def _row_has_strong_source(row: Mapping) -> bool:
    return source_kind(row.get("source_title", "")) in {
        "corporate_action",
        "earnings_release",
        "board",
        "other",
    }


def _candidate_has_strong_source(source_title: str) -> bool:
    return source_kind(source_title) in {
        "corporate_action",
        "earnings_release",
        "board",
        "other",
    }


def reconcile_evidence(candidate, evidence_rows: Iterable[Mapping], source_title: str):
    """
    Merge the best compatible official evidence row into candidate.

    Returns:
      candidate,
      matched_row or None,
      corroborated (bool)

    "corroborated" means the event has independent support from at least one
    non-AGM/non-financial-statement source. That is what allows a source_review
    AGM candidate to become publishable after reconciliation.
    """
    matches = []

    for row in evidence_rows:
        if not isinstance(row, Mapping):
            continue

        score = event_match_score(candidate, row)
        if score >= 0:
            matches.append((score, row))

    if not matches:
        return candidate, None, False

    matches.sort(key=lambda item: item[0], reverse=True)
    _, best = matches[0]

    if _float(getattr(candidate, "dividend_per_share", 0)) <= 0:
        amount = _float(best.get("dividend_per_share"))
        if amount > 0:
            candidate.dividend_per_share = amount

    if not getattr(candidate, "currency", ""):
        candidate.currency = best.get("currency") or "NGN"

    if not getattr(candidate, "dividend_type", ""):
        candidate.dividend_type = best.get("dividend_type") or "dividend"

    for field in (
        "qualification_date",
        "payment_date",
        "closure_date",
        "announcement_date",
        "registrar",
    ):
        if not getattr(candidate, field, "") and best.get(field):
            setattr(candidate, field, best.get(field))

    current_status = (getattr(candidate, "status", "") or "").lower()
    old_status = (best.get("status") or "").lower()
    if current_status in {"", "proposed"} and old_status in {"declared", "approved"}:
        candidate.status = old_status

    # Independent strong-source support can rehabilitate an AGM/review source.
    corroborated = (
        _candidate_has_strong_source(source_title)
        or _row_has_strong_source(best)
    )

    # Exchange-rate notices are supplementary only and never count as
    # independent dividend-declaration evidence.
    if source_kind(source_title) == "exchange_rate":
        corroborated = _row_has_strong_source(best)

    if source_kind(best.get("source_title", "")) == "exchange_rate":
        corroborated = _candidate_has_strong_source(source_title)

    return candidate, best, corroborated


def quarantine_uncorroborated_agm(published_rows, evidence_rows):
    """
    Remove AGM-only rows from the public feed unless another stronger official
    source corroborates the same ticker/amount/type.

    Returns:
      safe_published_rows,
      demoted_rows
    """
    safe = []
    demoted = []

    all_evidence = list(evidence_rows or [])

    for row in published_rows:
        if source_kind(row.get("source_title", "")) != "agm":
            safe.append(row)
            continue

        ticker = (row.get("ticker") or "").upper().strip()
        amount = _float(row.get("dividend_per_share"))

        corroborated = False

        for other in all_evidence + published_rows:
            if other is row:
                continue

            if (other.get("ticker") or "").upper().strip() != ticker:
                continue

            if not compatible_type(
                row.get("dividend_type"),
                other.get("dividend_type"),
            ):
                continue

            if amount > 0 and _float(other.get("dividend_per_share")) > 0:
                if not amounts_match(amount, other.get("dividend_per_share")):
                    continue

            if not _row_has_strong_source(other):
                continue

            if not date_proximity_ok(row, other):
                continue

            corroborated = True
            break

        if corroborated:
            safe.append(row)
        else:
            demoted_row = dict(row)
            demoted_row["confidence"] = "review"
            demoted.append(demoted_row)

    return safe, demoted

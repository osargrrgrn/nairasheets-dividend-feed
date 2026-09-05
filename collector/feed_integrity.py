"""
collector/feed_integrity.py — Patch 38

Final publication gate for the buyer-facing dividend feed.
It validates structural integrity, date logic, currency/amount sanity, and
economic-event uniqueness before dividends.csv is written.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping

EXPECTED_COLUMNS = (
    "event_id", "ticker", "company", "dividend_per_share", "currency",
    "dividend_type", "qualification_date", "payment_date", "closure_date",
    "announcement_date", "registrar", "status", "confidence",
    "source_url", "source_title",
)

CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")

def _text(v):
    return str(v or "").strip()

def _float(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0

def _iso(v):
    try:
        return date.fromisoformat(_text(v))
    except Exception:
        return None

def economic_key(row: Mapping) -> tuple:
    """
    Identity of the cash distribution, independent of source PDF.

    Patch 38: dividend_type is intentionally excluded — one economic event
    may be described as 'dividend' in one document and 'interim' in another.
    Amount is rounded to 4dp to absorb minor floating-point variance across
    documents (e.g. 0.2499999 vs 0.25).
    Dates use the ISO string directly — a mismatch of even one day between
    two documents means they may be different events, so we keep date
    precision but handle the interim/final label ambiguity via type exclusion.
    """
    return (
        _text(row.get("ticker")).upper(),
        _text(row.get("currency") or "NGN").upper(),
        round(_float(row.get("dividend_per_share")), 4),
        _text(row.get("qualification_date")),
        _text(row.get("payment_date")),
    )

def validate_published_feed(rows: Iterable[Mapping]):
    rows = list(rows or [])
    errors = []
    warnings = []
    seen = {}

    for i, row in enumerate(rows, start=1):
        prefix = f"row {i}"

        # Catch unresolved merge debris anywhere in values.
        joined = " ".join(_text(v) for v in row.values())
        if any(marker in joined for marker in CONFLICT_MARKERS):
            errors.append(f"{prefix}: git conflict marker present")

        ticker = _text(row.get("ticker")).upper()
        currency = _text(row.get("currency") or "NGN").upper()
        amount = _float(row.get("dividend_per_share"))
        qd = _iso(row.get("qualification_date"))
        pd = _iso(row.get("payment_date"))
        ad = _iso(row.get("announcement_date"))
        cd = _iso(row.get("closure_date"))

        # Patch 38: detect polluted company names (source titles leaking in)
        company = _text(row.get("company") or "")
        POLLUTION_SIGNALS = (
            "ANNUAL GENERAL MEETING", "AGM", "EARNINGS RELEASE",
            "BOARD MEETING", "NOTICES OF", "CORPORATE ACTIONS",
            "FINANCIAL STATEMENT", "QUARTERLY RESULTS",
        )
        if any(signal in company.upper() for signal in POLLUTION_SIGNALS):
            warnings.append(
                f"{prefix}: company field contains source-title pollution: {company[:60]}"
            )

        if not ticker:
            errors.append(f"{prefix}: blank ticker")
        if amount <= 0:
            errors.append(f"{prefix}: non-positive dividend amount")
        if currency == "NGN" and 0 < amount < 0.01:
            errors.append(f"{prefix}: sub-1-kobo NGN amount {amount}")
        if not qd:
            errors.append(f"{prefix}: missing/invalid qualification_date")
        if not pd:
            errors.append(f"{prefix}: missing/invalid payment_date")
        if qd and pd and pd < qd:
            errors.append(f"{prefix}: payment_date precedes qualification_date")
        if ad and pd and pd < ad:
            warnings.append(f"{prefix}: payment_date precedes announcement_date")
        if cd and pd and pd < cd:
            warnings.append(f"{prefix}: payment_date precedes closure_date")

        key = economic_key(row)
        if key in seen:
            errors.append(
                f"{prefix}: duplicate economic event; first seen at row {seen[key]}"
            )
        else:
            seen[key] = i

    return errors, warnings

def assert_no_conflict_markers(path):
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    markers = [m for m in CONFLICT_MARKERS if m in text]
    if markers:
        raise RuntimeError(
            f"{path} contains unresolved git conflict marker(s): {', '.join(markers)}"
        )

def quality_report(rows, errors, warnings, duplicates_removed=0):
    currencies = {}
    for row in rows:
        cur = _text(row.get("currency") or "NGN").upper()
        currencies[cur] = currencies.get(cur, 0) + 1
    return {
        "published_rows": len(rows),
        "economic_duplicates_removed": int(duplicates_removed or 0),
        "currencies": currencies,
        "errors": list(errors),
        "warnings": list(warnings),
        "status": "PASS" if not errors else "FAIL",
    }

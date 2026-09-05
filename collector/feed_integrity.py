"""
collector/feed_integrity.py — Patch 34

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
    """Identity of the cash distribution, independent of source PDF."""
    return (
        _text(row.get("ticker")).upper(),
        _text(row.get("currency") or "NGN").upper(),
        round(_float(row.get("dividend_per_share")), 6),
        _text(row.get("dividend_type")).lower(),
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

        # Patch 46: reject dates before 2024 — these are historical artifacts
        # from financial statements or OCR errors reading old dates
        from datetime import date as _date
        min_valid_date = _date(2024, 1, 1)
        if qd and qd < min_valid_date:
            errors.append(f"{prefix}: qualification_date {qd} is before 2024 — likely OCR/extraction error")
        if pd and pd < min_valid_date:
            errors.append(f"{prefix}: payment_date {pd} is before 2024 — likely OCR/extraction error")
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

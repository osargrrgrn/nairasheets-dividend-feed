"""
collector/feed_integrity.py — Patch 52

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
    Identity of one cash distribution, independent of the source PDF.

    Patch 52: keyed on ticker, currency, amount and PAYMENT date only.
    dividend_type is excluded (the same payout is labelled 'dividend',
    'final', 'interim' or 'distribution' by different filings) and the
    qualification date is excluded (it is the field most often mis-read,
    and a company never pays the same amount twice on the same day).
    """
    return (
        _text(row.get("ticker")).upper(),
        _text(row.get("currency") or "NGN").upper(),
        round(_float(row.get("dividend_per_share")), 4),
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


MIN_VALID_DATE = date(2024, 1, 1)

_SOURCE_RANK = (
    ("DIVIDEND_ANNOUNCEMENT", 100), ("CORPORATE_ACTION", 95), ("DISTRIBUTION", 90),
    ("NGX_NOTIFICATION", 90), ("AGM_RESOLUTION", 60), ("RESOLUTIONS", 60),
    ("OUTCOME", 55), ("ANNUAL_GENERAL_MEETING", 40), ("EARNINGS", 30),
    ("PRESS_RELEASE", 25), ("FINANCIAL_STATEMENT", 10), ("EXCHANGE_RATE", 5),
)


def _source_rank(row: Mapping) -> int:
    hay = (_text(row.get("source_title")) + " " + _text(row.get("source_url"))).upper().replace(" ", "_").replace("-", "_")
    for sig, score in _SOURCE_RANK:
        if sig in hay:
            return score
    return 20


def _qual_sanity(qd, pd_):
    """Higher is better: a qualification date 1-120 days before payment is plausible."""
    if not qd or not pd_:
        return -1
    gap = (pd_ - qd).days
    if gap < 1:
        return -1
    if gap <= 120:
        return 100 - gap // 10          # closer to payment is slightly better
    return 10                            # possible but suspicious


def _clean_company(row: Mapping) -> str:
    """
    Prefer the curated ticker->name map already in the repo; fall back to the
    longest alias in tickers.py, title-cased with short acronyms preserved.
    Never returns a source-title string.
    """
    ticker = _text(row.get("ticker")).upper()
    try:
        from .pending_resolver import _TICKER_TO_COMPANY
        name = _TICKER_TO_COMPANY.get(ticker)
        if name:
            return name
    except Exception:
        pass
    try:
        from .tickers import ALIASES
        names = [n for n, t in ALIASES.items() if t == ticker]
        if names:
            raw = max(names, key=len)
            small = {"PLC", "OF", "AND", "FOR", "THE", "LIMITED", "LTD"}
            out = []
            for tok in raw.split():
                if tok in small:
                    out.append(tok.capitalize() if tok != "OF" and tok != "AND" and tok != "FOR" and tok != "THE" else tok.lower())
                elif len(tok) <= 4 and tok.isalpha():
                    out.append(tok)  # acronym: MTN, UBA, BUA, IBTC, REIT
                else:
                    out.append(tok.capitalize())
            if out:
                out[0] = out[0][0].upper() + out[0][1:]
            return " ".join(out)
    except Exception:
        pass
    company = _text(row.get("company"))
    # Strip an obvious filing-title suffix if that is all we have.
    for sep in (" - NGX", "-NGX", " - CORPORATE", "-CORPORATE", "- NOTICES", " NOTICES OF", "-INTERIM", "-FINAL", "-DIVIDEND", "-AGM", " AGM", "-RESOLUTION", "-OUTCOME", "-PROPOSED", "-EARNINGS", "-Q1", "-Q2", "-H1"):
        if sep in company.upper():
            company = company[: company.upper().find(sep)].strip(" -")
            break
    return company


def finalize_published_rows(rows: Iterable[Mapping]):
    """
    Patch 52 — last step before the integrity gate.

    1. Reject rows that cannot be a real dividend event and return them so the
       caller can park them in pending instead of publishing:
         - payment_date <= qualification_date
         - any date before 2024-01-01
    2. Replace polluted company names with clean ones.
    3. Collapse rows sharing economic_key() (ticker|currency|amount|payment),
       keeping the strongest source and the sanest qualification date, and
       filling any blank field from the other row.

    Returns (kept_rows, rejected_rows).
    """
    kept = {}
    rejected = []

    for row in rows or []:
        row = dict(row)
        qd = _iso(row.get("qualification_date"))
        pd_ = _iso(row.get("payment_date"))

        bad = None
        if qd and pd_ and pd_ <= qd:
            bad = "payment_date_not_after_qualification_date"
        elif (qd and qd < MIN_VALID_DATE) or (pd_ and pd_ < MIN_VALID_DATE):
            bad = "date_before_2024"
        if bad:
            row["confidence"] = "review"
            row["resolution"] = bad
            rejected.append(row)
            continue

        row["company"] = _clean_company(row) or row.get("company", "")

        key = economic_key(row)
        current = kept.get(key)
        if current is None:
            kept[key] = row
            continue

        # Choose the base row: better qualification date, then stronger source.
        a_score = (_qual_sanity(_iso(current.get("qualification_date")), pd_), _source_rank(current))
        b_score = (_qual_sanity(qd, pd_), _source_rank(row))
        base, other = (row, current) if b_score > a_score else (current, row)
        merged = dict(base)
        for field, value in other.items():
            if not merged.get(field) and value:
                merged[field] = value
        kept[key] = merged

    ordered = sorted(kept.values(), key=lambda r: (_text(r.get("payment_date")), _text(r.get("ticker"))))
    return ordered, rejected

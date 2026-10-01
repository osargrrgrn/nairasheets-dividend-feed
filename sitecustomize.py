"""Runtime safety patches loaded automatically by Python.

Python imports ``sitecustomize`` at startup when the repository root is on
``sys.path`` (as it is in the collector workflow).  Keep narrowly-scoped
normalisations here when an NGX/PDF extraction quirk must be fixed without
waiting for every historical row to be reparsed by hand.
"""

import re

try:
    from collector import parse as _parse
except Exception:
    _parse = None

if _parse is not None:
    _original_normalize = _parse.normalize_ngx_dividend_text

    def _normalize_with_naira_colon(text: str) -> str:
        text = _original_normalize(text)
        # NGX notices sometimes render a decimal colon: N1:00 / ₦12:50.
        # Only touch currency-prefixed amounts with exactly two fractional
        # digits, so times and ordinary punctuation are unaffected.
        return re.sub(
            r"(?i)(?P<prefix>₦|\bN(?=\d)|\bNGN\s*)(?P<whole>\d{1,6}):(?P<frac>\d{2})\b",
            lambda m: f"{m.group('prefix')}{m.group('whole')}.{m.group('frac')}",
            text,
        )

    _parse.normalize_ngx_dividend_text = _normalize_with_naira_colon

    # Defence in depth: if a filing contains an explicit currency-prefixed
    # colon amount but the normal parser still returns a one-kobo-style value,
    # prefer the explicit amount.  This is generic, not ticker-specific.
    _original_parse_dividend_pdf = _parse.parse_dividend_pdf

    def _parse_dividend_pdf_with_colon_guard(*args, **kwargs):
        event = _original_parse_dividend_pdf(*args, **kwargs)
        text = kwargs.get("text")
        if text is None and args:
            text = args[0]
        text = text or ""

        try:
            current = float(getattr(event, "dividend_per_share", 0) or 0)
        except Exception:
            current = 0.0

        if (getattr(event, "currency", "") or "NGN").upper() == "NGN" and current <= 0.01:
            matches = re.findall(
                r"(?i)(?:₦|\bN(?=\d)|\bNGN\s*)(\d{1,6}):(\d{2})\b",
                text,
            )
            if matches:
                values = [float(f"{whole}.{frac}") for whole, frac in matches]
                explicit = max(values)
                if explicit > current:
                    event.dividend_per_share = explicit
        return event

    _parse.parse_dividend_pdf = _parse_dividend_pdf_with_colon_guard

# When a corrected reparse of the same official filing arrives, remove the
# stale one-kobo version before the normal publication finalizer runs.  The
# identity is source URL + ticker + qualification date + payment date, so this
# cannot merge unrelated dividends merely because they share a ticker.
try:
    from collector import feed_integrity as _integrity
except Exception:
    _integrity = None

if _integrity is not None:
    _original_finalize = _integrity.finalize_published_rows

    def _finalize_without_stale_tiny_duplicates(rows):
        rows = [dict(r) for r in (rows or [])]
        best_amount = {}
        for row in rows:
            key = (
                str(row.get("source_url") or "").strip(),
                str(row.get("ticker") or "").upper().strip(),
                str(row.get("qualification_date") or "").strip(),
                str(row.get("payment_date") or "").strip(),
            )
            try:
                amount = float(row.get("dividend_per_share") or 0)
            except Exception:
                amount = 0.0
            best_amount[key] = max(best_amount.get(key, 0.0), amount)

        cleaned = []
        for row in rows:
            key = (
                str(row.get("source_url") or "").strip(),
                str(row.get("ticker") or "").upper().strip(),
                str(row.get("qualification_date") or "").strip(),
                str(row.get("payment_date") or "").strip(),
            )
            try:
                amount = float(row.get("dividend_per_share") or 0)
            except Exception:
                amount = 0.0
            if amount <= 0.01 and best_amount.get(key, 0.0) > 0.01:
                continue
            cleaned.append(row)

        return _original_finalize(cleaned)

    _integrity.finalize_published_rows = _finalize_without_stale_tiny_duplicates

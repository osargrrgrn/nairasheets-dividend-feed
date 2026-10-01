"""Runtime safety patches loaded automatically by Python.

Python imports ``sitecustomize`` at startup when the repository root is on
``sys.path`` (as it is in the collector workflow). Keep narrowly-scoped
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
        # NGX notices sometimes print naira decimals with a colon, e.g.
        # N1:00 or ₦12:50. Convert only explicit currency-prefixed amounts;
        # this cannot alter times or ordinary punctuation.
        return re.sub(
            r"(?i)(?P<prefix>₦|\bN(?=\s*\d)|\bNGN\s*)(?P<whole>\d{1,6})\s*:\s*(?P<frac>\d{2})\b",
            lambda m: f"{m.group('prefix')}{m.group('whole')}.{m.group('frac')}",
            text,
        )

    _parse.normalize_ngx_dividend_text = _normalize_with_naira_colon

    # Patch the amount inference itself rather than only the final event. This
    # matters because parse_dividend_pdf validates the inferred amount before
    # returning an event. It also repairs cases where a naira amount such as
    # N2.05 was accidentally interpreted as 2.05 kobo (= N0.0205).
    _original_infer_currency_and_dps = _parse.infer_currency_and_dps

    _NAIRA_AMOUNT = re.compile(
        r"(?i)(?:₦|\bN(?=\s*\d)|\bNGN\s*)(\d{1,6})(?:\s*([.:])\s*(\d{2}))?\b"
    )

    def _explicit_naira_dividend_amount(text: str):
        text = text or ""
        candidates = []
        for m in _NAIRA_AMOUNT.finditer(text):
            # Require dividend/distribution context near the currency amount so
            # unrelated financial-statement figures cannot become a payout.
            start = max(0, m.start() - 180)
            end = min(len(text), m.end() + 180)
            context = text[start:end].lower()
            if "dividend" not in context and "distribution" not in context:
                continue
            whole = m.group(1)
            frac = m.group(3)
            value = float(f"{whole}.{frac}" if frac is not None else whole)
            if 0 < value <= 500:
                candidates.append(value)
        return max(candidates) if candidates else None

    def _infer_currency_and_dps_with_naira_guard(text: str, doc_type: str = "unknown"):
        normalized = _normalize_with_naira_colon(text)
        currency, value = _original_infer_currency_and_dps(normalized, doc_type)
        explicit = _explicit_naira_dividend_amount(normalized)

        # Prefer a clearly printed naira amount when the normal parser found
        # nothing, or when it produced the exact 1/100-scale signature caused
        # by treating that same naira figure as kobo.
        if explicit is not None:
            try:
                numeric = float(value) if value is not None else None
            except Exception:
                numeric = None
            if numeric is None or numeric <= 0 or abs(numeric * 100.0 - explicit) < 1e-9:
                return "NGN", explicit
        return currency, value

    _parse.infer_currency_and_dps = _infer_currency_and_dps_with_naira_guard

    # Defence in depth for callers that somehow bypass infer_currency_and_dps.
    _original_parse_dividend_pdf = _parse.parse_dividend_pdf

    def _parse_dividend_pdf_with_naira_guard(*args, **kwargs):
        event = _original_parse_dividend_pdf(*args, **kwargs)
        text = kwargs.get("text")
        if text is None and args:
            text = args[0]
        explicit = _explicit_naira_dividend_amount(_normalize_with_naira_colon(text or ""))

        try:
            current = float(getattr(event, "dividend_per_share", 0) or 0)
        except Exception:
            current = 0.0

        if explicit is not None and (
            current <= 0 or abs(current * 100.0 - explicit) < 1e-9
        ):
            event.currency = "NGN"
            event.dividend_per_share = explicit
        return event

    _parse.parse_dividend_pdf = _parse_dividend_pdf_with_naira_guard

try:
    from collector import feed_integrity as _integrity
except Exception:
    _integrity = None

if _integrity is not None:
    _original_finalize = _integrity.finalize_published_rows

    # One already-published row predates the parser repair above. The official
    # NGX filing 47959 writes GTCO's 2026 interim dividend as N1:00 per share;
    # the old parser stored that as N0.01. Migrate that exact official source
    # once during finalization so the buyer-facing feed heals automatically.
    _GTCO_2026_INTERIM_SOURCE = (
        "https://doclib.ngxgroup.com/Financial_NewsDocs/"
        "47959_GUARANTY_TRUST_HOLDING_COMPANY_PLC-2026_INTERIM_DIVIDEND_-_"
        "CORPORATE_ANNOUNCEMENT__CORPORATE_ACTIONS_SEPTEMBER_2026.pdf"
    )

    def _finalize_without_stale_tiny_duplicates(rows):
        rows = [dict(r) for r in (rows or [])]

        for row in rows:
            if (
                str(row.get("source_url") or "").strip() == _GTCO_2026_INTERIM_SOURCE
                and str(row.get("ticker") or "").upper().strip() == "GTCO"
                and str(row.get("qualification_date") or "") == "2026-10-12"
                and str(row.get("payment_date") or "") == "2026-10-20"
            ):
                try:
                    amount = float(row.get("dividend_per_share") or 0)
                except Exception:
                    amount = 0.0
                if amount == 0.01:
                    row["dividend_per_share"] = 1.0

        # If a corrected reparse of any filing arrives alongside its stale
        # one-kobo version, keep only the corrected version of that same event.
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

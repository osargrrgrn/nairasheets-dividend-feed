"""Runtime safety patches loaded automatically by Python.

This module is intentionally tiny. Python imports ``sitecustomize`` at startup
when the repository root is on sys.path (as it is in the collector workflow).
It normalizes a recurring NGX OCR artifact before the dividend parser sees it.

GTCO and similar scanned notices can OCR a naira decimal as ``N1:00`` or
``₦1:00``. In dividend/share context that colon is a decimal separator, not a
time separator. Normalizing it here means new announcements are corrected
automatically without any manual spreadsheet intervention.
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
        # Only touch currency-prefixed amounts with exactly two decimal digits.
        # Examples: N1:00 -> N1.00, ₦12:50 -> ₦12.50, NGN 3:25 -> NGN 3.25.
        return re.sub(
            r"(?i)(?P<prefix>₦|\bN(?=\d)|\bNGN\s*)(?P<whole>\d{1,6}):(?P<frac>\d{2})\b",
            lambda m: f"{m.group('prefix')}{m.group('whole')}.{m.group('frac')}",
            text,
        )

    _parse.normalize_ngx_dividend_text = _normalize_with_naira_colon

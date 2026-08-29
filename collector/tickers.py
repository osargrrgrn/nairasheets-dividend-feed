"""
NairaSheets should ultimately load this mapping from the same master ticker list
used by the tracker. For now this provides a normalization hook.
"""

ALIASES = {
    "NIGERIAN EXCHANGE GROUP PLC": "NGXGROUP",
    "GUARANTY TRUST HOLDING COMPANY PLC": "GTCO",
    "MTN NIGERIA COMMUNICATIONS PLC": "MTNN",
    "SEPLAT ENERGY PLC": "SEPLAT",
}

def resolve_ticker(company: str, title: str = "") -> str:
    hay = f"{company} {title}".upper()

    for name, ticker in ALIASES.items():
        if name in hay:
            return ticker

    # Common filename/title form sometimes starts with a ticker-like code.
    # Conservative fallback: do not guess if not mapped.
    return ""

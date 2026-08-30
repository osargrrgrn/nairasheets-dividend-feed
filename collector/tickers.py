ALIASES = {
    "NIGERIAN EXCHANGE GROUP PLC": "NGXGROUP",
    "GUARANTY TRUST HOLDING COMPANY PLC": "GTCO",
    "MTN NIGERIA COMMUNICATIONS PLC": "MTNN",
    "SEPLAT ENERGY PLC": "SEPLAT",
    "P Z CUSSONS NIGERIA PLC": "PZ",
    "PZ CUSSONS NIGERIA PLC": "PZ",
    "SUNU ASSURANCES NIGERIA PLC": "SUNUASSUR",
    "ACCESS HOLDINGS PLC": "ACCESSCORP",
    "CUTIX PLC": "CUTIX",
    "PRESCO PLC": "PRESCO",
    "AIRTEL AFRICA PLC": "AIRTELAFRI",
    "JAPAUL GOLD AND VENTURES PLC": "JAPAULGOLD",
    "UNIVERSAL INSURANCE PLC": "UNIVINSURE",
}

def normalize(value: str) -> str:
    return " ".join(
        value.upper()
        .replace(".", " ")
        .replace(",", " ")
        .replace("-", " ")
        .split()
    )

def resolve_ticker(company: str, title: str = "") -> str:
    hay = normalize(f"{company} {title}")
    for name, ticker in sorted(ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if normalize(name) in hay:
            return ticker
    return ""

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
    "CUSTODIAN INVESTMENT PLC": "CUSTODIAN",
    "VFD GROUP PLC": "VFDGROUP",
    "UNIVERSITY PRESS PLC": "UPL",
    "THE INITIATES PLC": "TIP",
    "ABBEY MORTGAGE BANK PLC": "ABBEYBDS",
    "ABBEY BANK PLC": "ABBEYBDS",
    "UAC OF NIGERIA PLC": "UACN",
    "STANBIC IBTC HOLDINGS PLC": "STANBIC",
    "BUA FOODS PLC": "BUAFOODS",
    "AIICO INSURANCE PLC": "AIICO",
    "FCMB GROUP PLC": "FCMB",
    "CORNERSTONE INSURANCE PLC": "CORNERST",
    "CHEMICAL AND ALLIED PRODUCTS PLC": "CAP",
    "DANGOTE CEMENT PLC": "DANGCEM",
    "UNILEVER NIGERIA PLC": "UNILEVER",
    "GUINNESS NIGERIA PLC": "GUINNESS",
    "GUINNESS NIG PLC": "GUINNESS",
    "UNITED CAPITAL PLC": "UCAP",
    "ARADEL HOLDINGS PLC": "ARADEL",
    "MTN NIGERIA": "MTNN",
    "NIDF": "NIDF",
    "NIGERIA INFRASTRUCTURE DEBT FUND": "NIDF",
    "OKOMU": "OKOMUOIL",
    "OKOMU OIL PALM PLC": "OKOMUOIL",
    "LEARN AFRICA PLC": "LEARNAFRCA",
    "ACADEMY PRESS PLC": "ACADEMY",
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

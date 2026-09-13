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
    # Patch 43: missing tickers causing OCR extractions to fail ticker resolution
    "UPDC REAL ESTATE INVESTMENT TRUST": "UPDCREIT",
    "UPDC REIT": "UPDCREIT",
    "UPDC REAL ESTATE": "UPDCREIT",
    "HONEYWELL FLOUR MILL PLC": "HONYFLOUR",
    "HONEYWELL FLOUR MILLS PLC": "HONYFLOUR",
    "RED STAR EXPRESS PLC": "REDSTAREX",
    "IKEJA HOTEL PLC": "IKEJAHOTEL",
    "AFRICA PRUDENTIAL PLC": "AFRIPRUD",
    "AFRICA PRUDENTIAL REGISTRARS PLC": "AFRIPRUD",
    "ZENITH BANK PLC": "ZENITHBANK",
    "UNITED BANK FOR AFRICA PLC": "UBA",
    "FIDELITY BANK PLC": "FIDELITYBK",
    "FIRST HOLDCO PLC": "FIRSTHOLDCO",
    "FBN HOLDINGS PLC": "FBNH",
    "DANGOTE SUGAR REFINERY PLC": "DANGSUGAR",
    "BUA CEMENT PLC": "BUACEMENT",
    "NESTLE NIGERIA PLC": "NESTLE",
    "NIGERIAN BREWERIES PLC": "NB",
    "LAFARGE AFRICA PLC": "WAPCO",
    "TRANSCORP PLC": "TRANSCORP",
    "TRANSCORP HOTELS PLC": "TRANSCOHOT",
    "FLOUR MILLS OF NIGERIA PLC": "FLOURMILL",
    "NASCON ALLIED INDUSTRIES PLC": "NASCON",
    "CADBURY NIGERIA PLC": "CADBURY",
    "VITAFOAM NIGERIA PLC": "VITAFOAM",
    "WEMA BANK PLC": "WEMABANK",
    "BETA GLASS PLC": "BETAGLAS",
    "JULIUS BERGER NIGERIA PLC": "JBERGER",
    "MUTUAL BENEFITS ASSURANCE PLC": "MBENEFIT",
    "ECOBANK TRANSNATIONAL INCORPORATED": "ETI",
    "STANBIC IBTC ETF 30": "STANBICETF30",
    "SIAML PENSION ETF 40": "SIAML40ETF",
    "CHAPEL HILL DENHAM": "NIDF",
    "TOTALENERGIES MARKETING NIGERIA PLC": "TOTAL",
    # Listed funds / REITs — often absent from equities-only listings
    "CORONATION INFRASTRUCTURE FUND": "CNIF",
    "CORONATION INFRASTRUCTURE DEBT FUND": "CNIF",
    "CNIF": "CNIF",
    "CHAPEL HILL DENHAM NIGERIA INFRASTRUCTURE DEBT FUND": "NIDF",
    "CHAPEL HILL DENHAM NIG INFRAS DEBT FUND": "NIDF",
    "UPDC REAL ESTATE INVESTMENT TRUST": "UPDCREIT",
    "UPDC REIT": "UPDCREIT",
    "SFS REAL ESTATE INVESTMENT TRUST": "SFSREIT",
    "SFS REIT": "SFSREIT",
    "MOFI REAL ESTATE INVESTMENT FUND": "MREIF",
    "MREIF": "MREIF",
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


# ---------------------------------------------------------------------------
# Patch 53: dynamic aliases from Kobo Terminal's NGX listed-companies table.
# The static ALIASES above are a floor; this keeps the map current without
# anyone hand-editing it. Cached to data/aliases_dynamic.json so imports
# never touch the network; discover.py calls refresh_dynamic_aliases() once
# per run.
# ---------------------------------------------------------------------------
import json as _json
import re as _re
from pathlib import Path as _Path

_DYNAMIC_FILE = _Path(__file__).resolve().parents[1] / "data" / "aliases_dynamic.json"
KOBO_LISTED_URL = "https://koboterminal.com/ngx-listed-companies"


def _load_dynamic_aliases() -> None:
    try:
        if _DYNAMIC_FILE.exists():
            extra = _json.loads(_DYNAMIC_FILE.read_text(encoding="utf-8"))
            for name, ticker in extra.items():
                ALIASES.setdefault(name, ticker)
    except Exception:
        pass


def refresh_dynamic_aliases() -> int:
    """
    Fetch Kobo Terminal's listed-companies table (Company | Ticker | Sector |
    Market cap | Price), derive NAME -> TICKER aliases, merge into ALIASES
    in memory and persist to data/aliases_dynamic.json. Returns count added.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except Exception:
        return 0
    try:
        r = requests.get(
            KOBO_LISTED_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"},
            timeout=(8, 20),
        )
        if r.status_code != 200:
            return 0
        soup = BeautifulSoup(r.text, "html.parser")
        found = {}
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue
                company, ticker = cells[0], cells[1]
                if not _re.fullmatch(r"[A-Z0-9]{2,12}", ticker or ""):
                    continue
                base = normalize(company)
                if len(base) < 3:
                    continue
                found[base] = ticker
                # Also register a PLC-suffixed form and a form without corporate suffixes,
                # because NGX filenames vary ("X PLC", "X NIGERIA PLC", "X").
                stripped = _re.sub(r"\b(PLC|LIMITED|LTD|NIGERIA|NIG)\b", " ", base)
                stripped = " ".join(stripped.split())
                if len(stripped) >= 4:
                    found.setdefault(stripped, ticker)
                    found.setdefault(stripped + " PLC", ticker)
        if not found:
            return 0
        added = 0
        for name, ticker in found.items():
            if name not in ALIASES:
                ALIASES[name] = ticker
                added += 1
        _DYNAMIC_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DYNAMIC_FILE.write_text(_json.dumps(found, indent=2, sort_keys=True), encoding="utf-8")
        print(f"[Aliases] Kobo listed companies: {len(found)} names, {added} new aliases", flush=True)
        return added
    except Exception as exc:
        print(f"[Aliases] refresh failed: {exc!r}", flush=True)
        return 0


_load_dynamic_aliases()

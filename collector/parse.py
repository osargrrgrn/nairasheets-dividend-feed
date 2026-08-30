import re
import hashlib
from datetime import datetime, timezone
from dateutil import parser as dateparser
from .schema import DividendEvent

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)

DIVIDEND_WORD_RE = re.compile(r"\b(dividend|distribution|cash distribution)\b", re.I)

PER_SHARE_RE = re.compile(
    r"(?:₦|N|NGN|US\$|USD|cents?|kobo).{0,80}(?:per\s+(?:ordinary\s+)?share|/share)"
    r"|(?:per\s+(?:ordinary\s+)?share|/share).{0,80}(?:₦|N|NGN|US\$|USD|cents?|kobo)",
    re.I | re.S,
)

DATE_CONTEXT_RE = re.compile(
    r"\b(qualification\s+date|record\s+date|payment\s+date|closure\s+of\s+register|"
    r"register\s+of\s+members|close\s+of\s+business)\b",
    re.I,
)

APPROVAL_CONTEXT_RE = re.compile(
    r"\b(recommended|declared|approved|proposed|payable|will\s+be\s+paid)\b",
    re.I,
)

def has_dividend_evidence(text: str) -> bool:
    if not DIVIDEND_WORD_RE.search(text):
        return False

    supporting = 0
    if PER_SHARE_RE.search(text):
        supporting += 1
    if DATE_CONTEXT_RE.search(text):
        supporting += 1
    if APPROVAL_CONTEXT_RE.search(text):
        supporting += 1

    return supporting >= 1

def iso_date(value: str) -> str:
    if not value:
        return ""

    value = value.strip(" .,:;\n\t")

    try:
        dt = dateparser.parse(value, dayfirst=True, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return ""

def first_match(patterns, text, flags=re.I | re.S):
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return m.group(1).strip()
    return ""

def infer_currency_and_dps(text: str):
    naira_patterns = [
        r"(?:final|interim|special|proposed)?\s*dividend"
        r"[^.\n]{0,260}?(?:that\s+is|equivalent\s+to|amounting\s+to)?\s*"
        r"(?:₦|NGN|N)\s*([0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:\([^)]*\)\s*)?per\s+(?:ordinary\s+)?share",

        r"(?:₦|NGN|N)\s*([0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:\([^)]*\)\s*)?per\s+(?:ordinary\s+)?share"
        r"[^.\n]{0,160}?(?:dividend|distribution)",
    ]

    for pattern in naira_patterns:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            return "NGN", float(m.group(1))

    kobo_patterns = [
        r"(?:final|interim|special|proposed)?\s*dividend"
        r"[^.\n]{0,260}?([0-9]+(?:\.[0-9]+)?)\s*kobo"
        r"\s*(?:\([^)]*\)\s*)?per\s+(?:ordinary\s+)?share",

        r"([0-9]+(?:\.[0-9]+)?)\s*kobo"
        r"\s*(?:\([^)]*\)\s*)?per\s+(?:ordinary\s+)?share",
    ]

    for pattern in kobo_patterns:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            return "NGN", float(m.group(1)) / 100.0

    usd_patterns = [
        r"(?:final|interim|special)?\s*dividend"
        r"[^.\n]{0,260}?(?:USD|US\$|US\s*)?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:US\s*)?cents?"
        r"\s*(?:\([^)]*\)\s*)?per\s+(?:ordinary\s+)?share",

        r"([0-9]+(?:\.[0-9]+)?)\s*(?:US\s*)?cents?"
        r"\s*(?:\([^)]*\)\s*)?per\s+(?:ordinary\s+)?share",
    ]

    for pattern in usd_patterns:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            return "USD", float(m.group(1)) / 100.0

    return "", None

def infer_dividend_type(text: str) -> str:
    low = text.lower()

    if "special dividend" in low and "interim dividend" in low:
        return "interim+special"
    if "special dividend" in low and "final dividend" in low:
        return "final+special"
    if "special dividend" in low:
        return "special"
    if "interim dividend" in low:
        return "interim"
    if "final dividend" in low:
        return "final"
    if "distribution" in low:
        return "distribution"

    return "dividend"

def extract_labeled_date(label: str, text: str) -> str:
    pat = rf"{label}\s*:?\s*(?:is|of|on)?\s*" \
          rf"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*" \
          rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}}" \
          rf"|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}" \
          rf"|(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+\d{{4}})"

    return iso_date(first_match([pat], text))

def extract_qualification_date(text: str) -> str:
    # 1) Best case: the filing explicitly labels the date.
    explicit = extract_labeled_date(
        r"(?:qualification\s+date|record\s+date)",
        text
    )
    if explicit:
        return explicit

    # 2) Fallback only when the wording is explicitly about dividend entitlement.
    # This avoids treating "financial year ended 31 May 2026" or balance-sheet
    # "shareholders as at 31 May 2026" wording as a qualification date.
    entitlement_patterns = [
        rf"(?:dividend|distribution)[\s\S]{{0,500}}?"
        rf"(?:shareholders?\s+whose\s+names\s+appear\s+in\s+the\s+register"
        rf"|register\s+of\s+members)[\s\S]{{0,260}}?"
        rf"(?:close\s+of\s+business\s+on|as\s+at|on)\s+"
        rf"(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",

        rf"(?:shareholders?\s+whose\s+names\s+appear\s+in\s+the\s+register"
        rf"|register\s+of\s+members)[\s\S]{{0,260}}?"
        rf"(?:close\s+of\s+business\s+on|as\s+at|on)\s+"
        rf"(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})"
        rf"[\s\S]{{0,400}}?(?:dividend|distribution)",
    ]

    return iso_date(first_match(entitlement_patterns, text))

def clean_company_name(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip(" :-")

    raw = re.split(
        r"\s*-\s*(?:POST BOARD MEETING|ANNUAL GENERAL MEETING|AGM|ANNOUNCEMENT|"
        r"BOARD APPROVAL|RESOLUTION|DIVIDEND|CORPORATE ACTION|QUARTER\s+\d+)",
        raw,
        maxsplit=1,
        flags=re.I,
    )[0]

    return raw[:160]

def infer_company(text: str, source_title: str) -> str:
    if source_title:
        candidate = clean_company_name(source_title.replace("_", " ").strip())
        if len(candidate) >= 3:
            return candidate

    candidate = first_match([
        r"^\s*([A-Z][A-Z0-9&().,'’ \-]{3,100}?(?:PLC|LIMITED))\b",
        r"([A-Z][A-Z0-9&().,'’ \-]{3,100}?(?:PLC|LIMITED))\s+(?:hereby|announces?|has announced)",
    ], text, flags=re.I | re.M)

    return clean_company_name(candidate)

def make_event_id(ticker: str, company: str, qual: str, pay: str, dps, dtype: str) -> str:
    canonical = "|".join([
        ticker.upper().strip(),
        company.upper().strip(),
        qual,
        pay,
        f"{dps:.6f}" if dps is not None else "",
        dtype.lower().strip(),
    ])

    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]

def infer_status(text: str) -> str:
    low = text.lower()

    if "cancelled dividend" in low or "dividend has been cancelled" in low:
        return "cancelled"

    if (
        "subject to shareholders' approval" in low
        or "subject to shareholders’ approval" in low
        or "for approval at the company" in low
        or "for approval at the annual general meeting" in low
        or "proposed dividend" in low
        or "recommended dividend" in low
    ):
        return "proposed"

    if "revised dividend" in low or "amended dividend" in low:
        return "amended"

    return "declared"

def parse_dividend_pdf(text: str, source_url: str, source_title: str = "", ticker: str = ""):
    currency, dps = infer_currency_and_dps(text)

    qualification = extract_qualification_date(text)

    payment = extract_labeled_date(
        r"(?:payment\s+date|dividend\s+payment\s+date)",
        text
    )

    closure = extract_labeled_date(
        r"(?:closure\s+of\s+register|closure\s+date)",
        text
    )

    company = infer_company(text, source_title)
    dtype = infer_dividend_type(text)
    status = infer_status(text)

    confidence = "high"
    if dps is None or not qualification or not payment:
        confidence = "review"

    event_id = make_event_id(
        ticker,
        company,
        qualification,
        payment,
        dps or 0.0,
        dtype
    )

    return DividendEvent(
        event_id=event_id,
        ticker=ticker.upper().strip(),
        company=company,
        dividend_per_share=float(dps or 0.0),
        currency=currency or "NGN",
        dividend_type=dtype,
        qualification_date=qualification,
        payment_date=payment,
        closure_date=closure,
        status=status,
        source_url=source_url,
        source_title=source_title,
        last_verified=datetime.now(timezone.utc).date().isoformat(),
        confidence=confidence,
    )

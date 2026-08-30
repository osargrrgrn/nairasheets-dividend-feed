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
    r"(?:₦|N|NGN|US\\$|USD|cents?|kobo).{0,60}(?:per\\s+(?:ordinary\\s+)?share|/share)"
    r"|(?:per\\s+(?:ordinary\\s+)?share|/share).{0,60}(?:₦|N|NGN|US\\$|USD|cents?|kobo)",
    re.I | re.S,
)
DATE_CONTEXT_RE = re.compile(
    r"\b(qualification\\s+date|record\\s+date|payment\\s+date|closure\\s+of\\s+register|"
    r"register\\s+of\\s+members|close\\s+of\\s+business)\b",
    re.I,
)
APPROVAL_CONTEXT_RE = re.compile(
    r"\b(recommended|declared|approved|proposed|payable|will\\s+be\\s+paid)\b",
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
    value = value.strip(" .,:;\\n\\t")
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
    m = re.search(
        r"(?:proposed\\s+dividend|interim\\s+dividend|final\\s+dividend|special\\s+dividend|"
        r"dividend(?:\\s+of)?)[^.\\n]{0,180}?(?:₦|NGN|N)\\s*([0-9]+(?:\\.[0-9]+)?)"
        r"\\s*(?:per\\s+(?:ordinary\\s+)?share|/share)?",
        text, re.I | re.S
    )
    if m:
        return "NGN", float(m.group(1))

    m = re.search(
        r"(?:proposed\\s+dividend|interim\\s+dividend|final\\s+dividend|special\\s+dividend|"
        r"dividend(?:\\s+of)?)[^.\\n]{0,180}?([0-9]+(?:\\.[0-9]+)?)\\s*kobo"
        r"(?:\\s+per\\s+(?:ordinary\\s+)?share)?",
        text, re.I | re.S
    )
    if m:
        return "NGN", float(m.group(1)) / 100.0

    m = re.search(
        r"(?:dividend(?:\\s+of)?)[^.\\n]{0,220}?(?:USD|US\\$|US\\s*)?\\s*"
        r"([0-9]+(?:\\.[0-9]+)?)\\s*(?:US\\s*)?cents?"
        r"(?:\\s+per\\s+(?:ordinary\\s+)?share)?",
        text, re.I | re.S
    )
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
    pat = rf"{label}\\s*:?\\s*(?:is|of|on)?\\s*((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\\s*\\d{{1,2}}(?:st|nd|rd|th)?\\s+(?:{MONTHS})\\s+\\d{{4}}|\\d{{1,2}}[/-]\\d{{1,2}}[/-]\\d{{2,4}}|(?:{MONTHS})\\s+\\d{{1,2}}(?:st|nd|rd|th)?[,]?\\s+\\d{{4}})"
    return iso_date(first_match([pat], text))

def clean_company_name(raw: str) -> str:
    raw = re.sub(r"\\s+", " ", raw).strip(" :-")
    raw = re.split(
        r"\\s*-\\s*(?:POST BOARD MEETING|ANNUAL GENERAL MEETING|AGM|ANNOUNCEMENT|"
        r"BOARD APPROVAL|RESOLUTION|DIVIDEND|CORPORATE ACTION)",
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
        r"^\\s*([A-Z][A-Z0-9&().,'’ \\-]{3,100}?(?:PLC|LIMITED))\\b",
        r"([A-Z][A-Z0-9&().,'’ \\-]{3,100}?(?:PLC|LIMITED))\\s+(?:hereby|announces?|has announced)",
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

def parse_dividend_pdf(text: str, source_url: str, source_title: str = "", ticker: str = ""):
    currency, dps = infer_currency_and_dps(text)

    qualification = extract_labeled_date(r"(?:qualification\\s+date|record\\s+date)", text)
    payment = extract_labeled_date(r"(?:payment\\s+date|dividend\\s+payment\\s+date)", text)
    closure = extract_labeled_date(r"(?:closure\\s+of\\s+register|closure\\s+date)", text)

    if not qualification:
        qualification = iso_date(first_match([
            rf"(?:register\\s+of\\s+members|shareholders?)[^.\\n]{{0,220}}?"
            rf"(?:close\\s+of\\s+business\\s+on|as\\s+at)\\s+"
            rf"(\\d{{1,2}}(?:st|nd|rd|th)?\\s+(?:{MONTHS})\\s+\\d{{4}})"
        ], text))

    company = infer_company(text, source_title)
    dtype = infer_dividend_type(text)

    low = text.lower()
    status = "declared"
    if "cancelled dividend" in low or "dividend has been cancelled" in low:
        status = "cancelled"
    elif "revised" in low or "amended" in low:
        status = "amended"

    confidence = "high"
    if dps is None or not qualification or not payment:
        confidence = "review"

    event_id = make_event_id(ticker, company, qualification, payment, dps or 0.0, dtype)

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

import re
import hashlib
from datetime import datetime, timezone
from dateutil import parser as dateparser
from .schema import DividendEvent

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)

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
    """
    Conservative: prefer phrases tied to dividend wording.
    Returns (currency, amount) or ("", None).
    """
    patterns = [
        # ₦2 / N2 / N 2.00 per share
        r"(?:proposed\s+dividend[^.\n]{0,160}?(?:a\s+)?dividend\s+of|dividend\s+of|dividend)[^.\n]{0,80}?[₦N]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\([^)]*\)\s*)?per\s+(?:ordinary\s+)?share",
        # 20 kobo per share
        r"(?:proposed\s+dividend|dividend(?:\s+of)?)[^.\n]{0,120}?([0-9]+(?:\.[0-9]+)?)\s*kobo\s+per\s+(?:ordinary\s+)?share",
        # USD 8.3 cents per share / 8.3 US cents per share
        r"(?:dividend(?:\s+of)?)[^.\n]{0,160}?(?:USD|US\$|US\s*)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:US\s*)?cents?\s+per\s+share",
    ]

    m = re.search(patterns[0], text, re.I | re.S)
    if m:
        return "NGN", float(m.group(1))

    m = re.search(patterns[1], text, re.I | re.S)
    if m:
        return "NGN", float(m.group(1)) / 100.0

    m = re.search(patterns[2], text, re.I | re.S)
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
    return "dividend"

def extract_labeled_date(label: str, text: str) -> str:
    # Capture common NGX date styles following a label.
    pat = rf"{label}\s*:?\s*(?:is|of|on)?\s*((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}}|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}|(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+\d{{4}})"
    return iso_date(first_match([pat], text))

def clean_company_name(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip(" :-")
    return raw[:160]

def infer_company(text: str, source_title: str) -> str:
    # Source title is normally the safest company-name hint.
    if source_title:
        title = source_title.replace("_", " ").strip()
        title = re.split(r"\s+-\s+|-(?:CORPORATE|DIVIDEND|YEAR|QUARTER)", title, maxsplit=1, flags=re.I)[0]
        if len(title) >= 3:
            return clean_company_name(title)

    # Fallback to common NGX announcement wording.
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

def parse_dividend_pdf(text: str, source_url: str, source_title: str = "", ticker: str = ""):
    currency, dps = infer_currency_and_dps(text)

    qualification = extract_labeled_date(r"(?:qualification\s+date|record\s+date)", text)
    payment = extract_labeled_date(r"(?:payment\s+date|dividend\s+payment\s+date)", text)
    closure = extract_labeled_date(r"(?:closure\s+of\s+register|closure\s+date)", text)

    # Some announcements use prose instead of a table.
    if not qualification:
        qualification = iso_date(first_match([
            rf"(?:register\s+of\s+members|shareholders?)[^.\n]{{0,180}}?(?:close\s+of\s+business\s+on|as\s+at)\s+(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})"
        ], text))

    company = infer_company(text, source_title)
    dtype = infer_dividend_type(text)

    status = "declared"
    low = text.lower()
    if "cancelled dividend" in low or "dividend has been cancelled" in low:
        status = "cancelled"
    elif "revised" in low or "amended" in low:
        status = "amended"

    # Don't publish events missing the three fields NairaSheets requires.
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

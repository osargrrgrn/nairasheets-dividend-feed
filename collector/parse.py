import re
import hashlib
from datetime import datetime, timezone
from dateutil import parser as dateparser
from .schema import DividendEvent

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)

CORPORATE_ACTION_TITLE_HINTS = (
    "corporate action",
    "dividend announcement",
    "interim dividend",
    "final dividend",
    "distribution announcement",
)

FINANCIAL_STATEMENT_TITLE_HINTS = (
    "financial statement",
    "financial statements",
    "quarter 1",
    "quarter 2",
    "quarter 3",
    "quarter 4",
    "half year",
    "full year",
    "annual report",
    "audited results",
    "unaudited results",
)

AGM_TITLE_HINTS = (
    "annual general meeting",
    "agm",
    "notice of annual general meeting",
    "notice of meeting",
)

CORPORATE_ACTION_BODY_HINTS = (
    "qualification date",
    "payment date",
    "closure of register",
    "register of members",
    "dividend announcement",
    "corporate action",
)

CURRENT_ACTION_HINTS = (
    "recommended",
    "declared",
    "approved",
    "proposed",
    "payable",
    "will be paid",
    "shall be paid",
    "payment will be made",
)


def classify_document(source_title: str, text: str) -> str:
    title = (source_title or "").lower().replace("_", " ")
    body = (text[:6000] or "").lower()

    title_ca = sum(2 for hint in CORPORATE_ACTION_TITLE_HINTS if hint in title)
    title_fs = sum(2 for hint in FINANCIAL_STATEMENT_TITLE_HINTS if hint in title)
    title_agm = sum(2 for hint in AGM_TITLE_HINTS if hint in title)

    body_ca = sum(1 for hint in CORPORATE_ACTION_BODY_HINTS if hint in body)
    body_fs = sum(
        1 for hint in (
            "statement of financial position",
            "statement of comprehensive income",
            "profit before tax",
            "earnings per share",
            "total assets",
            "revenue",
        )
        if hint in body
    )
    body_agm = sum(
        1 for hint in (
            "annual general meeting",
            "proxy form",
            "ordinary business",
            "special business",
        )
        if hint in body
    )

    ca_score = title_ca + body_ca
    fs_score = title_fs + body_fs
    agm_score = title_agm + body_agm

    if ca_score >= 3 and ca_score > fs_score and ca_score > agm_score:
        return "corporate_action"
    if fs_score >= 3 and fs_score >= ca_score:
        return "financial_statement"
    if agm_score >= 2 and agm_score > ca_score:
        return "agm"
    if ca_score > 0:
        return "mixed"
    return "unknown"


def dividend_context_windows(text: str):
    anchors = (
        r"\binterim\s+dividend\b",
        r"\bfinal\s+dividend\b",
        r"\bspecial\s+dividend\b",
        r"\bdividend\s+announcement\b",
        r"\bcorporate\s+action\b",
        r"\bqualification\s+date\b",
        r"\bpayment\s+date\b",
        r"\bclosure\s+of\s+register\b",
        r"\bregister\s+of\s+members\b",
        r"\bper\s+(?:ordinary\s+)?share\b",
        r"\bper\s+unit\b",
        r"\bkobo\s+per\s+(?:ordinary\s+)?share\b",
        r"\bkobo\s+per\s+unit\b",
        r"\bincome\s+distribution\b",
        r"\bquarterly\s+distribution\b",
        r"\bapproved\s+(?:a\s+)?(?:final\s+|interim\s+)?dividend\b",
        r"\bresolved\s+(?:that\s+)?(?:a\s+)?(?:final\s+|interim\s+)?dividend\b",
        r"\brecommended\s+(?:a\s+)?(?:final\s+|interim\s+)?dividend\b",
        r"\bdividend\s+of\b",
        r"\bdistribution\s+of\b",
    )

    windows = []
    seen = set()

    for pattern in anchors:
        for match in re.finditer(pattern, text, re.I):
            start = max(0, match.start() - 500)
            end = min(len(text), match.end() + 500)
            bucket = start // 250
            if bucket in seen:
                continue
            seen.add(bucket)
            windows.append(text[start:end])

    if text:
        windows.insert(0, text[:2500])

    return windows


def window_has_current_dividend_evidence(window: str) -> bool:
    low = window.lower()

    if "dividend" not in low and "distribution" not in low:
        return False

    payout_value_language = bool(re.search(
        r"(?:per\s+(?:ordinary\s+)?share"
        r"|for\s+every\s+(?:ordinary\s+)?share"
        r"|per\s+unit"
        r"|for\s+every\s+unit"
        r"|\bkobo\b"
        r"|(?:₦|ngn|naira)\s*\d)",
        low,
        re.I,
    ))

    action_language = any(
        phrase in low
        for phrase in (
            "approved",
            "resolved",
            "resolution",
            "recommended",
            "declared",
            "proposed",
            "qualification date",
            "record date",
            "payment date",
            "closure of register",
            "register of members",
            "book closure",
            "payable on",
            "will be paid",
            "payment will be made",
        )
    )

    return payout_value_language and action_language


DIVIDEND_WORD_RE = re.compile(r"\b(dividend|distribution|cash distribution)\b", re.I)

# Allow NGX wording such as:
# "N26 per 2 kobo ordinary share"
# as well as the simpler "N2.50 per ordinary share".
PER_SHARE_RE = re.compile(
    r"(?:₦|N|NGN|US\$|USD|cents?|kobo).{0,100}"
    r"(?:per\s+(?:\d+(?:\.\d+)?\s*kobo\s+)?(?:ordinary\s+)?share|/share)"
    r"|(?:per\s+(?:\d+(?:\.\d+)?\s*kobo\s+)?(?:ordinary\s+)?share|/share)"
    r".{0,100}(?:₦|N|NGN|US\$|USD|cents?|kobo)",
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

def infer_currency_and_dps(text: str, doc_type: str = "unknown"):
    """
    Patch 24: score payout candidates by context instead of accepting the first
    regex match. Kobo is converted exactly once.
    """
    windows = dividend_context_windows(text)

    if doc_type in ("financial_statement", "agm"):
        windows = [
            window for window in windows
            if window_has_current_dividend_evidence(window)
        ]

    if not windows:
        return "", None

    # NGX uses several equivalent ways to describe the security unit:
    #   "per ordinary share"
    #   "per 50 kobo ordinary share"
    #   "per ordinary share of 50 kobo each"
    #   "for every share of 50 kobo"
    # The old regex covered only the first form.
    recipient = (
        r"(?:"
        r"per\s+(?:(?:\d+(?:\.\d+)?)\s*kobo\s+)?(?:ordinary\s+)?share"
        r"(?:\s+of\s+(?:\d+(?:\.\d+)?)\s*kobo(?:\s+each)?)?"
        r"|for\s+every\s+(?:ordinary\s+)?share"
        r"(?:\s+of\s+(?:\d+(?:\.\d+)?)\s*kobo(?:\s+each)?)?"
        r"|per\s+unit"
        r"|for\s+every\s+unit"
        r")"
    )
    payout = r"(?:dividend|distribution)"

    candidates = []

    def add(currency, value, score, window):
        try:
            value = float(value)
        except Exception:
            return
        if value <= 0 or value > 500:
            return

        low = window.lower()
        if "qualification date" in low or "record date" in low:
            score += 20
        if "payment date" in low or "payable on" in low:
            score += 20
        if "approved" in low or "resolved" in low or "declared" in low:
            score += 20
        if "recommended" in low or "proposed" in low:
            score += 12
        if "interim dividend" in low or "final dividend" in low:
            score += 18
        if "per ordinary share" in low or "per share" in low or "per unit" in low:
            score += 15

        candidates.append((score, currency, value))

    naira_patterns = [
        (
            125,
            rf"(?:final|interim|special|gross)?\s*{payout}"
            rf"\s+(?:of\s+)?(?:₦|NGN|N)\s*([0-9]+(?:\.[0-9]+)?)"
            rf"\s*(?:\([^)]*\)\s*)?{recipient}",
        ),
        (
            110,
            rf"(?:approved|resolved|recommended|declared|proposed)?"
            rf"[^.\n]{{0,140}}?(?:final|interim|special|gross)?\s*{payout}"
            rf"[^.\n]{{0,220}}?(?:of|at|being|equivalent\s+to|amounting\s+to)?\s*"
            rf"(?:₦|NGN|N)\s*([0-9]+(?:\.[0-9]+)?)"
            rf"\s*(?:\([^)]*\)\s*)?{recipient}",
        ),
        (
            95,
            rf"(?:₦|NGN|N)\s*([0-9]+(?:\.[0-9]+)?)"
            rf"\s*(?:\([^)]*\)\s*)?{recipient}"
            rf"[^.\n]{{0,180}}?{payout}",
        ),
    ]

    kobo_patterns = [
        (
            125,
            rf"(?:final|interim|special|gross)?\s*{payout}"
            rf"\s+(?:of\s+)?([0-9]+(?:\.[0-9]+)?)\s*kobo"
            rf"\s*(?:\([^)]*\)\s*)?{recipient}",
        ),
        (
            120,
            rf"(?:approved|resolved|recommended|declared|proposed)"
            rf"[^.\n]{{0,180}}?{payout}"
            rf"[^.\n]{{0,180}}?([0-9]+(?:\.[0-9]+)?)\s*kobo"
            rf"\s*(?:\([^)]*\)\s*)?{recipient}",
        ),
        (
            115,
            rf"(?:approved|resolved|recommended|declared|proposed)?"
            rf"[^.\n]{{0,140}}?(?:final|interim|special|gross)?\s*{payout}"
            rf"[^.\n]{{0,220}}?([0-9]+(?:\.[0-9]+)?)\s*kobo"
            rf"\s*(?:\([^)]*\)\s*)?{recipient}",
        ),
        (
            100,
            rf"([0-9]+(?:\.[0-9]+)?)\s*kobo"
            rf"\s*(?:\([^)]*\)\s*)?{recipient}"
            rf"[^.\n]{{0,180}}?{payout}",
        ),
        (
            90,
            rf"{payout}[^.\n]{{0,240}}?"
            rf"([0-9]+(?:\.[0-9]+)?)\s*kobo"
            rf"[^.\n]{{0,100}}?{recipient}",
        ),
    ]

    usd_patterns = [
        (
            110,
            rf"(?:approved|resolved|recommended|declared|proposed)?"
            rf"[^.\n]{{0,140}}?(?:final|interim|special|gross)?\s*{payout}"
            rf"[^.\n]{{0,220}}?(?:USD|US\$|US\s*)?"
            rf"([0-9]+(?:\.[0-9]+)?)\s*(?:US\s*)?cents?"
            rf"\s*(?:\([^)]*\)\s*)?{recipient}",
        ),
    ]

    for window in windows:
        for score, pattern in naira_patterns:
            for m in re.finditer(pattern, window, re.I | re.S):
                add("NGN", m.group(1), score, window)

        for score, pattern in kobo_patterns:
            for m in re.finditer(pattern, window, re.I | re.S):
                raw_kobo = float(m.group(1))

                # Patch 25: values below 1 kobo are too easy to confuse with
                # percentages, par values, or damaged PDF text. Do not turn
                # them into a high-confidence naira payout automatically.
                # A separate official source can still supply the correct
                # amount through reconciliation.
                if raw_kobo < 1:
                    continue

                add("NGN", raw_kobo / 100.0, score, window)

        for score, pattern in usd_patterns:
            for m in re.finditer(pattern, window, re.I | re.S):
                add("USD", float(m.group(1)) / 100.0, score, window)

    if not candidates:
        return "", None

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, currency, value = candidates[0]
    return currency, value

def infer_dividend_type(text: str) -> str:
    low = text.lower()
    if (
        "distribution announcement" in low
        or "income distribution" in low
        or "quarterly distribution" in low
        or ("distribution" in low and "per unit" in low)
    ):
        return "distribution"
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
    explicit = extract_labeled_date(
        r"(?:qualification\s+date|record\s+date)",
        text
    )
    if explicit:
        return explicit

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


def extract_payment_date(text: str) -> str:
    explicit = extract_labeled_date(
        r"(?:payment\s+date|dividend\s+payment\s+date)",
        text
    )
    if explicit:
        return explicit

    patterns = [
        rf"(?:dividend|distribution)[\s\S]{{0,500}}?"
        rf"(?:will\s+be\s+paid|shall\s+be\s+paid|is\s+payable|payable|payment\s+will\s+be\s+made)"
        rf"[\s\S]{{0,100}}?(?:on\s+)?"
        rf"(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",

        rf"(?:will\s+be\s+paid|shall\s+be\s+paid|is\s+payable|payable|payment\s+will\s+be\s+made)"
        rf"[\s\S]{{0,100}}?(?:on\s+)?"
        rf"(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})"
        rf"[\s\S]{{0,300}}?(?:dividend|distribution)",

        rf"(?:dividend|distribution)[\s\S]{{0,500}}?"
        rf"(?:will\s+be\s+paid|shall\s+be\s+paid|is\s+payable|payable|payment\s+will\s+be\s+made)"
        rf"[\s\S]{{0,100}}?"
        rf"((?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+\d{{4}})",
    ]

    return iso_date(first_match(patterns, text))


def extract_announcement_date(text: str) -> str:
    head = text[:2500]

    patterns = [
        rf"(?:^|\n)\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})\s*(?:\n|$)",
        rf"(?:^|\n)\s*((?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+\d{{4}})\s*(?:\n|$)",
        r"(?:^|\n)\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})\s*(?:\n|$)",
    ]

    return iso_date(first_match(patterns, head, flags=re.I | re.M))

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

    proposed_patterns = [
        r"dividend[\s\S]{0,400}?proposed\s+to\s+(?:the\s+)?members",
        r"dividend[\s\S]{0,400}?for\s+approval\s+at",
        r"dividend[\s\S]{0,400}?subject\s+to\s+shareholders?[’']?\s+approval",
        r"recommended\s+(?:a\s+)?(?:final|interim|special)?\s*dividend",
        r"proposed\s+(?:final|interim|special)?\s*dividend",
    ]

    if any(re.search(p, low, re.I | re.S) for p in proposed_patterns):
        return "proposed"

    if "revised dividend" in low or "amended dividend" in low:
        return "amended"

    return "declared"

def parse_dividend_pdf(text: str, source_url: str, source_title: str = "", ticker: str = ""):
    doc_type = classify_document(source_title, text)
    currency, dps = infer_currency_and_dps(text, doc_type)

    qualification = extract_qualification_date(text)

    payment = extract_payment_date(text)

    closure = extract_labeled_date(
        r"(?:closure\s+of\s+register|closure\s+date)",
        text
    )

    announcement = extract_announcement_date(text)

    company = infer_company(text, source_title)
    dtype = infer_dividend_type(text)
    status = infer_status(text)

    confidence = "high"
    if dps is None or not qualification or not payment:
        confidence = "review"
    elif doc_type in ("financial_statement", "agm"):
        confidence = "source_review"
    elif doc_type == "mixed":
        confidence = "medium"

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
        announcement_date=announcement,
        status=status,
        source_url=source_url,
        source_title=source_title,
        last_verified=datetime.now(timezone.utc).date().isoformat(),
        confidence=confidence,
    )

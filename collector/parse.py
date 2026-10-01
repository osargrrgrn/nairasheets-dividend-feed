import re
import hashlib
from datetime import datetime, timezone
from dateutil import parser as dateparser
from .schema import DividendEvent

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)

# ---------------------------------------------------------------------------
# Patch 56: tolerant date tokens.
# ---------------------------------------------------------------------------

_WEEKDAY = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day"
_D = r"\d\s?\d?"
_Y = r"(?:19|20)\s?\d\s?\d"
_DATE_DMY = (rf"(?P<d1>{_D})(?:st|nd|rd|th)?\s*(?:day\s+)?(?:of\s+)?" rf"(?P<m1>{MONTHS})\s*,?\s*(?P<y1>{_Y})")
_DATE_MDY = (rf"(?P<m2>{MONTHS})\s*,?\s*(?P<d2>{_D})(?:st|nd|rd|th)?\s*,?\s*(?P<y2>{_Y})")
_DATE_NUM = r"(?P<n>\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
DATE_TOKEN = rf"(?:{_WEEKDAY})?\s*,?\s*(?:{_DATE_DMY}|{_DATE_MDY}|{_DATE_NUM})"
_MONTH_NUM = {m.lower(): i for i, m in enumerate(MONTHS.split("|"), start=1)}

def _date_digits(value):
    return re.sub(r"\s+", "", value or "")

def date_from_match(m):
    groups = m.groupdict()
    if groups.get("n"):
        parts = re.split(r"[/-]", groups["n"])
        if len(parts) != 3: return ""
        day, month, year = parts
        if len(year) != 4: year = "20" + year
        try: day_i, month_i, year_i = int(day), int(month), int(year)
        except Exception: return ""
    else:
        if groups.get("m1"): day, month, year = groups["d1"], groups["m1"], groups["y1"]
        else: day, month, year = groups["d2"], groups["m2"], groups["y2"]
        try:
            day_i = int(_date_digits(day)); month_i = _MONTH_NUM[month.lower()]; year_i = int(_date_digits(year))
        except Exception: return ""
    if not (1 <= day_i <= 31 and 1 <= month_i <= 12 and 1900 <= year_i <= 2100): return ""
    return f"{year_i:04d}-{month_i:02d}-{day_i:02d}"

def first_date(patterns, text):
    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if m:
            found = date_from_match(m)
            if found: return found
    return ""

CORPORATE_ACTION_TITLE_HINTS = ("corporate action","dividend announcement","interim dividend","final dividend","distribution announcement")
FINANCIAL_STATEMENT_TITLE_HINTS = ("financial statement","financial statements","quarter 1","quarter 2","quarter 3","quarter 4","half year","full year","annual report","audited results","unaudited results")
AGM_TITLE_HINTS = ("annual general meeting","agm","notice of annual general meeting","notice of meeting")
CORPORATE_ACTION_BODY_HINTS = ("qualification date","payment date","closure of register","register of members","dividend announcement","corporate action")
CURRENT_ACTION_HINTS = ("recommended","declared","approved","proposed","payable","will be paid","shall be paid","payment will be made")

def classify_document(source_title: str, text: str) -> str:
    title=(source_title or "").lower().replace("_"," "); body=(text[:6000] or "").lower()
    title_ca=sum(2 for h in CORPORATE_ACTION_TITLE_HINTS if h in title); title_fs=sum(2 for h in FINANCIAL_STATEMENT_TITLE_HINTS if h in title); title_agm=sum(2 for h in AGM_TITLE_HINTS if h in title)
    body_ca=sum(1 for h in CORPORATE_ACTION_BODY_HINTS if h in body)
    body_fs=sum(1 for h in ("statement of financial position","statement of comprehensive income","profit before tax","earnings per share","total assets","revenue") if h in body)
    body_agm=sum(1 for h in ("annual general meeting","proxy form","ordinary business","special business") if h in body)
    ca_score=title_ca+body_ca; fs_score=title_fs+body_fs; agm_score=title_agm+body_agm
    if ca_score>=3 and ca_score>fs_score and ca_score>agm_score: return "corporate_action"
    if fs_score>=3 and fs_score>=ca_score: return "financial_statement"
    if agm_score>=2 and agm_score>ca_score: return "agm"
    if ca_score>0: return "mixed"
    return "unknown"

def normalize_ngx_dividend_text(text: str) -> str:
    """Normalize common NGX/PDF extraction and OCR quirks."""
    text=text or ""
    text=re.sub(r'(\d{1,2})[\u201c\u201d\u2018\u2019"\']+\s*(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)',lambda m:m.group(1)+"th "+m.group(2),text,flags=re.I)
    text=re.sub(r'(\d{1,2})[\u201c\u201d\u2018\u2019"\']+\s+day\s+of',lambda m:m.group(1)+"th day of",text,flags=re.I)
    text=re.sub(r"(?i)\b(\d)\s+(\d)\s+(kobo|kobos|k)\b",lambda m:f"{m.group(1)}{m.group(2)} {m.group(3)}",text)
    text=re.sub(r"(?i)\b(\d+(?:\.\d+)?)\s*k\b",r"\1 kobo",text)
    text=re.sub(r"((?:January|February|March|April|May|June|July|August|September|October|November|December)),?\s*\n\s*(\d{4})",r"\1 \2",text,flags=re.I)
    text=re.sub(r'\$8(0\.\d+)',r'₦\1',text); text=re.sub(r'\$80\.',r'₦0.',text)

    # Patch 57: NGX/OCR amount repairs belong in the real parser, not sitecustomize.
    # GTCO scans can render a decimal colon: "N1:00 per Ordinary Share".
    text=re.sub(r"(?i)(?<![A-Z0-9])(?P<cur>₦|NGN|N)\s*(?P<whole>\d{1,3})\s*:\s*(?P<frac>\d{1,2})(?=\s|\b)",lambda m:f"{m.group('cur')}{m.group('whole')}.{m.group('frac')}",text)
    # If an explicit naira marker precedes a decimal amount, a trailing "Kobo"
    # is contradictory OCR/filing noise. Keep the explicit currency semantics:
    # "N2.05 Kobo per share" -> "N2.05 per share", not 0.0205 naira.
    text=re.sub(r"(?i)(?<![A-Z0-9])(?P<cur>₦|NGN|N)\s*(?P<amount>\d+(?:\.\d+)?)\s+kobo\b",lambda m:f"{m.group('cur')}{m.group('amount')}",text)
    return text

def dividend_context_windows(text: str):
    anchors=(r"\binterim\s+dividend\b",r"\bfinal\s+dividend\b",r"\bspecial\s+dividend\b",r"\bdividend\s+announcement\b",r"\bcorporate\s+action\b",r"\bqualification\s+date\b",r"\bpayment\s+date\b",r"\bclosure\s+of\s+register\b",r"\bregister\s+of\s+members\b",r"\bper\s+(?:ordinary\s+)?share\b",r"\bper\s+unit\b",r"\bkobo\s+per\s+(?:ordinary\s+)?share\b",r"\bkobo\s+per\s+unit\b",r"\bincome\s+distribution\b",r"\bquarterly\s+distribution\b",r"\bapproved\s+(?:a\s+)?(?:final\s+|interim\s+)?dividend\b",r"\bresolved\s+(?:that\s+)?(?:a\s+)?(?:final\s+|interim\s+)?dividend\b",r"\brecommended\s+(?:a\s+)?(?:final\s+|interim\s+)?dividend\b",r"\bdividend\s+of\b",r"\bdistribution\s+of\b")
    windows=[]; seen=set()
    for pattern in anchors:
        for match in re.finditer(pattern,text,re.I):
            start=max(0,match.start()-500); end=min(len(text),match.end()+500); bucket=start//250
            if bucket in seen: continue
            seen.add(bucket); windows.append(text[start:end])
    if text: windows.insert(0,text[:2500])
    return windows

def window_has_current_dividend_evidence(window: str) -> bool:
    low=window.lower()
    if "dividend" not in low and "distribution" not in low: return False
    payout_value_language=bool(re.search(r"(?:per\s+(?:ordinary\s+)?share|for\s+every\s+(?:ordinary\s+)?share|per\s+unit|for\s+every\s+unit|\bkobo\b|(?:₦|ngn|naira)\s*\d)",low,re.I))
    action_language=any(p in low for p in ("approved","resolved","resolution","recommended","declared","proposed","qualification date","record date","payment date","closure of register","register of members","book closure","payable on","will be paid","payment will be made"))
    return payout_value_language and action_language

DIVIDEND_WORD_RE=re.compile(r"\b(dividend|distribution|cash distribution)\b",re.I)
PER_SHARE_RE=re.compile(r"(?:₦|N|NGN|US\$|USD|cents?|kobo).{0,100}(?:per\s+(?:\d+(?:\.\d+)?\s*kobo\s+)?(?:ordinary\s+)?share|/share)|(?:per\s+(?:\d+(?:\.\d+)?\s*kobo\s+)?(?:ordinary\s+)?share|/share).{0,100}(?:₦|N|NGN|US\$|USD|cents?|kobo)",re.I|re.S)
DATE_CONTEXT_RE=re.compile(r"\b(qualification\s+date|record\s+date|payment\s+date|closure\s+of\s+register|register\s+of\s+members|close\s+of\s+business)\b",re.I)
APPROVAL_CONTEXT_RE=re.compile(r"\b(recommended|declared|approved|proposed|payable|will\s+be\s+paid)\b",re.I)

def has_dividend_evidence(text: str) -> bool:
    if not DIVIDEND_WORD_RE.search(text): return False
    supporting=sum([bool(PER_SHARE_RE.search(text)),bool(DATE_CONTEXT_RE.search(text)),bool(APPROVAL_CONTEXT_RE.search(text))])
    return supporting>=1

def iso_date(value: str) -> str:
    if not value: return ""
    value=value.strip(" .,:;\n\t")
    try: return dateparser.parse(value,dayfirst=True,fuzzy=True).date().isoformat()
    except Exception: return ""

def first_match(patterns,text,flags=re.I|re.S):
    for pattern in patterns:
        m=re.search(pattern,text,flags)
        if m: return m.group(1).strip()
    return ""

def infer_currency_and_dps(text: str, doc_type: str="unknown"):
    windows=dividend_context_windows(text)
    if doc_type in ("financial_statement","agm"): windows=[w for w in windows if window_has_current_dividend_evidence(w)]
    if not windows: return "",None
    recipient=(r"(?:per\s+(?:(?:\d+(?:\.\d+)?)\s*kobo\s+)?(?:ordinary\s+)?share(?:\s+of\s+(?:\d+(?:\.\d+)?)\s*kobo(?:\s+each)?)?|for\s+every\s+(?:ordinary\s+)?share(?:\s+of\s+(?:\d+(?:\.\d+)?)\s*kobo(?:\s+each)?)?|per\s+unit|for\s+every\s+unit)")
    payout=r"(?:dividend|distribution)"; candidates=[]
    def add(currency,value,score,window):
        try: value=float(value)
        except Exception: return
        if value<=0 or value>500: return
        low=window.lower()
        if "qualification date" in low or "record date" in low: score+=20
        if "payment date" in low or "payable on" in low: score+=20
        if "approved" in low or "resolved" in low or "declared" in low: score+=20
        if "recommended" in low or "proposed" in low: score+=12
        if "interim dividend" in low or "final dividend" in low: score+=18
        if "per ordinary share" in low or "per share" in low or "per unit" in low: score+=15
        candidates.append((score,currency,value))
    naira_patterns=[(125,rf"(?:final|interim|special|gross)?\s*{payout}\s+(?:of\s+)?(?:₦|NGN|N)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\([^)]*\)\s*)?{recipient}"),(110,rf"(?:approved|resolved|recommended|declared|proposed)?[^.\n]{{0,140}}?(?:final|interim|special|gross)?\s*{payout}[^.\n]{{0,220}}?(?:of|at|being|equivalent\s+to|amounting\s+to)?\s*(?:₦|NGN|N)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\([^)]*\)\s*)?{recipient}"),(95,rf"(?:₦|NGN|N)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\([^)]*\)\s*)?{recipient}[^.\n]{{0,180}}?{payout}")]
    kobo_patterns=[(130,rf"(?:dividend|distribution)[^.\n]{{0,180}}?([0-9](?:\s+[0-9])+)\s*kobo"),(125,rf"(?:final|interim|special|gross)?\s*{payout}\s+(?:of\s+)?([0-9]+(?:\.[0-9]+)?)\s*kobo\s*(?:\([^)]*\)\s*)?{recipient}"),(120,rf"(?:approved|resolved|recommended|declared|proposed)[^.\n]{{0,180}}?{payout}[^.\n]{{0,180}}?([0-9]+(?:\.[0-9]+)?)\s*kobo\s*(?:\([^)]*\)\s*)?{recipient}"),(115,rf"(?:approved|resolved|recommended|declared|proposed)?[^.\n]{{0,140}}?(?:final|interim|special|gross)?\s*{payout}[^.\n]{{0,220}}?([0-9]+(?:\.[0-9]+)?)\s*kobo\s*(?:\([^)]*\)\s*)?{recipient}"),(100,rf"([0-9]+(?:\.[0-9]+)?)\s*kobo\s*(?:\([^)]*\)\s*)?{recipient}[^.\n]{{0,180}}?{payout}"),(90,rf"{payout}[^.\n]{{0,240}}?([0-9]+(?:\.[0-9]+)?)\s*kobo[^.\n]{{0,100}}?{recipient}")]
    usd_patterns=[(110,rf"(?:approved|resolved|recommended|declared|proposed)?[^.\n]{{0,140}}?(?:final|interim|special|gross)?\s*{payout}[^.\n]{{0,220}}?(?:USD|US\$|US\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:US\s*)?cents?\s*(?:\([^)]*\)\s*)?{recipient}")]
    for window in windows:
        for score,pattern in naira_patterns:
            for m in re.finditer(pattern,window,re.I|re.S): add("NGN",m.group(1),score,window)
        for score,pattern in kobo_patterns:
            for m in re.finditer(pattern,window,re.I|re.S):
                raw_value=m.group(1)
                if re.search(r"\s",raw_value): raw_value=re.sub(r"\s+","",raw_value)
                raw_kobo=float(raw_value)
                if raw_kobo<1: continue
                add("NGN",raw_kobo/100.0,score,window)
        for score,pattern in usd_patterns:
            for m in re.finditer(pattern,window,re.I|re.S): add("USD",float(m.group(1))/100.0,score,window)
    if not candidates: return "",None
    candidates.sort(key=lambda item:item[0],reverse=True); _,currency,value=candidates[0]
    return currency,value

def infer_dividend_type(text: str)->str:
    low=text.lower()
    if "distribution announcement" in low or "income distribution" in low or "quarterly distribution" in low or ("distribution" in low and "per unit" in low): return "distribution"
    if "special dividend" in low and "interim dividend" in low: return "interim+special"
    if "special dividend" in low and "final dividend" in low: return "final+special"
    if "special dividend" in low: return "special"
    if "interim dividend" in low: return "interim"
    if "final dividend" in low: return "final"
    if "distribution" in low: return "distribution"
    return "dividend"

def extract_labeled_date(label: str,text: str)->str:
    return first_date([rf"{label}\s*[:\-]?\s*(?:is|of|on)?\s*(?:or\s+before\s+)?{DATE_TOKEN}",rf"{label}[^.]{{0,80}}?(?:will\s+be|shall\s+be|is)\s+{DATE_TOKEN}"],text)

def extract_qualification_date(text: str)->str:
    explicit=extract_labeled_date(r"(?:qualification\s+date|record\s+date)",text)
    if explicit:return explicit
    entitlement=first_date([rf"(?:register\s+of\s+(?:members|shareholders|unit\s*holders)|names\s+are\s+registered)[^.]{{0,120}}?(?:close\s+of\s+business\s+on|as\s+at|as\s+of|on)\s+(?:the\s+)?{DATE_TOKEN}"],text)
    if entitlement:return entitlement
    entitlement_patterns=[rf"(?:dividend|distribution)[\s\S]{{0,500}}?(?:shareholders?\s+whose\s+names\s+appear\s+in\s+the\s+register|shareholders?\s+whose\s+names\s+are\s+registered|names\s+are\s+registered|register\s+of\s+members)[\s\S]{{0,320}}?(?:close\s+of\s+business\s+on|as\s+at\s+the\s+close\s+of\s+business\s+on|as\s+at|on)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*,?\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",rf"(?:shareholders?\s+whose\s+names\s+appear\s+in\s+the\s+register|shareholders?\s+whose\s+names\s+are\s+registered|names\s+are\s+registered|register\s+of\s+members)[\s\S]{{0,320}}?(?:close\s+of\s+business\s+on|as\s+at\s+the\s+close\s+of\s+business\s+on|as\s+at|on)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*,?\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})[\s\S]{{0,400}}?(?:dividend|distribution)"]
    result=iso_date(first_match(entitlement_patterns,text))
    if result:return result
    wider_patterns=[rf"(?:shareholders?\s+whose\s+names\s+appear\s+in\s+the\s+register|register\s+of\s+members\s+as\s+at|register\s+of\s+members\s+at\s+the\s+close)[\s\S]{{0,200}}?(?:close\s+of\s+business\s+on|as\s+at|on)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*,?\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",rf"paid\s+to\s+shareholders?[\s\S]{{0,200}}?as\s+of\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*,?\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",rf"qualification\s+date[\s|:·]+\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*,?\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",rf"(?:close\s+of\s+business\s+on|close\s+of\s+business)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*,?\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})"]
    day_of_patterns=[rf"close\s+of\s+business\s+on\s+(\d{{1,2}}(?:st|nd|rd|th)?\s+day\s+of\s+(?:{MONTHS})\s+\d{{4}})",rf"(\d{{1,2}}(?:st|nd|rd|th)?\s+day\s+of\s+(?:{MONTHS})\s+\d{{4}})",rf"qualification\s+date[\s\n]+.{{0,30}}?(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",rf"qualification\s+date[\s\n]+.{{0,30}}?(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS}))"]
    result=iso_date(first_match(day_of_patterns,text))
    if result:return result
    return iso_date(first_match(wider_patterns,text))

def extract_payment_date(text: str)->str:
    patterns=[rf"\bon\s+(?:or\s+before\s+)?{DATE_TOKEN}\s*,?\s*(?:the\s+)?(?:cash\s+|final\s+|interim\s+|total\s+)*(?:dividends?|distributions?)\s+(?:will\s+be|shall\s+be)\s+paid",rf"(?:payment|distribution)\s+date[^.]{{0,100}}?(?:will\s+be|shall\s+be|is)\s+{DATE_TOKEN}",rf"(?:cash\s+dividend\s+)?(?:payment|distribution)\s+date\s*[:\-]?\s*(?:on\s+)?(?:or\s+before\s+)?{DATE_TOKEN}",rf"payable\s+on\s+{DATE_TOKEN}"]
    return first_date(patterns,text)

def extract_announcement_date(text: str)->str:
    head=text[:2500]; patterns=[rf"(?:^|\n)\s*(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})\s*(?:\n|$)",rf"(?:^|\n)\s*((?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+\d{{4}})\s*(?:\n|$)",r"(?:^|\n)\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})\s*(?:\n|$)"]
    return iso_date(first_match(patterns,head,flags=re.I|re.M))

def clean_company_name(raw: str)->str:
    raw=re.sub(r"\s+"," ",raw).strip(" :-")
    raw=re.split(r"\s*-\s*(?:POST BOARD MEETING|ANNUAL GENERAL MEETING|AGM|ANNOUNCEMENT|BOARD APPROVAL|RESOLUTION|DIVIDEND|CORPORATE ACTION|QUARTER\s+\d+)",raw,maxsplit=1,flags=re.I)[0]
    return raw[:160]

_TICKER_TO_COMPANY={"GTCO":"Guaranty Trust Holding Company Plc","ZENITHBANK":"Zenith Bank Plc","MTNN":"MTN Nigeria Communications Plc","ACCESSCORP":"Access Holdings Plc","UBA":"United Bank for Africa Plc","DANGCEM":"Dangote Cement Plc","BUAFOODS":"BUA Foods Plc","BUACEMENT":"BUA Cement Plc","SEPLAT":"Seplat Energy Plc","AIRTELAFRI":"Airtel Africa Plc","STANBIC":"Stanbic IBTC Holdings Plc","FIDELITYBK":"Fidelity Bank Plc","FCMB":"FCMB Group Plc","FIRSTHOLDCO":"First HoldCo Plc","FBNH":"FBN Holdings Plc","OKOMUOIL":"Okomu Oil Palm Plc","PRESCO":"Presco Plc","NESTLE":"Nestle Nigeria Plc","GUINNESS":"Guinness Nigeria Plc","NB":"Nigerian Breweries Plc","UPDCREIT":"UPDC Real Estate Investment Trust","NIDF":"Nigeria Infrastructure Debt Fund","AFRIPRUD":"Africa Prudential Plc","HONYFLOUR":"Honeywell Flour Mill Plc","DANGSUGAR":"Dangote Sugar Refinery Plc","WAPCO":"Lafarge Africa Plc","UACN":"UAC of Nigeria Plc","TRANSCORP":"Transcorp Plc","CUSTODIAN":"Custodian Investment Plc","UCAP":"United Capital Plc","VFDGROUP":"VFD Group Plc","CAP":"Chemical and Allied Products Plc","BETAGLAS":"Beta Glass Plc","UNILEVER":"Unilever Nigeria Plc","FLOURMILL":"Flour Mills of Nigeria Plc","NASCON":"NASCON Allied Industries Plc","CADBURY":"Cadbury Nigeria Plc","VITAFOAM":"Vitafoam Nigeria Plc","CUTIX":"Cutix Plc","WEMABANK":"Wema Bank Plc","IKEJAHOTEL":"Ikeja Hotel Plc","REDSTAREX":"Red Star Express Plc","UPL":"University Press Plc","LEARNAFRCA":"Learn Africa Plc","ACADEMY":"Academy Press Plc","ARADEL":"Aradel Holdings Plc","AIICO":"AIICO Insurance Plc","TIP":"The Initiates Plc","CORNERST":"Cornerstone Insurance Plc","MANSARD":"AXA Mansard Insurance Plc","JAIZBANK":"Jaiz Bank Plc","STERLINGNG":"Sterling Financial Holdings Company Plc","MAYBAKER":"May & Baker Nigeria Plc","FIDSON":"Fidson Healthcare Plc","PZ":"P.Z. Cussons Nigeria Plc","NGXGROUP":"Nigerian Exchange Group Plc","TRANSCOHOT":"Transcorp Hotels Plc","JBERGER":"Julius Berger Nigeria Plc","TOTAL":"TotalEnergies Marketing Nigeria Plc"}

def infer_company(text: str,source_title: str,ticker: str="")->str:
    if ticker:
        clean=_TICKER_TO_COMPANY.get(ticker.upper().strip(),"")
        if clean:return clean
    if source_title:
        candidate=clean_company_name(source_title.replace("_"," ").strip())
        if len(candidate)>=3:return candidate
    candidate=first_match([r"^\s*([A-Z][A-Z0-9&().,'' \-]{3,100}?(?:PLC|LIMITED))\b",r"([A-Z][A-Z0-9&().,'' \-]{3,100}?(?:PLC|LIMITED))\s+(?:hereby|announces?|has announced)"],text,flags=re.I|re.M)
    return clean_company_name(candidate)

def make_event_id(ticker: str,company: str,qual: str,pay: str,dps,dtype: str)->str:
    canonical="|".join([ticker.upper().strip(),company.upper().strip(),qual,pay,f"{dps:.6f}" if dps is not None else "",dtype.lower().strip()])
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]

def infer_status(text: str)->str:
    low=text.lower()
    if "cancelled dividend" in low or "dividend has been cancelled" in low:return "cancelled"
    proposed_patterns=[r"dividend[\s\S]{0,400}?proposed\s+to\s+(?:the\s+)?members",r"dividend[\s\S]{0,400}?for\s+approval\s+at",r"dividend[\s\S]{0,400}?subject\s+to\s+shareholders?[’']?\s+approval",r"recommended\s+(?:a\s+)?(?:final|interim|special)?\s*dividend",r"proposed\s+(?:final|interim|special)?\s*dividend"]
    if any(re.search(p,low,re.I|re.S) for p in proposed_patterns):return "proposed"
    if "revised dividend" in low or "amended dividend" in low:return "amended"
    return "declared"

def parse_dividend_pdf(text: str,source_url: str,source_title: str="",ticker: str=""):
    text=normalize_ngx_dividend_text(text); doc_type=classify_document(source_title,text); currency,dps=infer_currency_and_dps(text,doc_type)
    qualification=extract_qualification_date(text); payment=extract_payment_date(text); closure=extract_labeled_date(r"(?:closure\s+of\s+register|closure\s+date)",text); announcement=extract_announcement_date(text)
    company=infer_company(text,source_title,ticker=ticker); dtype=infer_dividend_type(text); status=infer_status(text)
    confidence="high"
    if dps is None or not qualification or not payment:confidence="review"
    elif doc_type in ("financial_statement","agm"):confidence="source_review"
    elif doc_type=="mixed":confidence="medium"
    event_id=make_event_id(ticker,company,qualification,payment,dps or 0.0,dtype)
    return DividendEvent(event_id=event_id,ticker=ticker.upper().strip(),company=company,dividend_per_share=float(dps or 0.0),currency=currency or "NGN",dividend_type=dtype,qualification_date=qualification,payment_date=payment,closure_date=closure,announcement_date=announcement,status=status,source_url=source_url,source_title=source_title,last_verified=datetime.now(timezone.utc).date().isoformat(),confidence=confidence)

from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class DividendEvent:
    event_id: str
    ticker: str
    company: str
    dividend_per_share: float
    currency: str
    dividend_type: str
    qualification_date: str
    payment_date: str
    closure_date: str = ""
    announcement_date: str = ""
    registrar: str = ""
    status: str = "declared"
    source_url: str = ""
    source_title: str = ""
    last_verified: str = ""
    confidence: str = "high"

    def to_dict(self):
        return asdict(self)

CSV_FIELDS = [
    "event_id",
    "ticker",
    "company",
    "dividend_per_share",
    "currency",
    "dividend_type",
    "qualification_date",
    "payment_date",
    "closure_date",
    "announcement_date",
    "registrar",
    "status",
    "source_url",
    "source_title",
    "last_verified",
    "confidence",
]

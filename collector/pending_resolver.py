"""
collector/pending_resolver.py — Patch 45 (Tiered Publication Rules)

Tiered publication system replacing the old conservative dual-source requirement.

Tier 1 — Auto-publish (no corroboration needed):
  Corporate Action Announcement / Dividend Announcement / Distribution Payment
  + complete dates (qual + pay) + amount > 0.01 NGN

Tier 2 — Auto-publish (no corroboration needed):
  AGM Resolution / AGM Notice with complete dividend data
  + complete dates + amount > 0.01 NGN

Tier 3 — Hold for corroboration:
  Financial statements, earnings releases, board meeting notices
  → require two independent sources

Tier 4 — Safety hold (always):
  Amount < 0.01 NGN or conflicting field values

Auto-reprocess: URLs marked not_dividend with dividend keywords in filename
are reset automatically every 30 days (handled in run.py).
"""

from typing import Mapping

from .reconcile import (
    amounts_match,
    compatible_type,
    date_proximity_ok,
    independent_source,
    source_strength,
    suspicious_tiny_ngn,
)

# Clean company names keyed by ticker — avoids source-title pollution
_TICKER_TO_COMPANY = {
    "UPDCREIT": "UPDC Real Estate Investment Trust",
    "SEPLAT": "Seplat Energy Plc",
    "ACADEMY": "Academy Press Plc",
    "ARADEL": "Aradel Holdings Plc",
    "UACN": "UAC of Nigeria Plc",
    "MTNN": "MTN Nigeria Communications Plc",
    "GTCO": "Guaranty Trust Holding Company Plc",
    "ZENITHBANK": "Zenith Bank Plc",
    "ACCESSCORP": "Access Holdings Plc",
    "UBA": "United Bank for Africa Plc",
    "DANGCEM": "Dangote Cement Plc",
    "BUAFOODS": "BUA Foods Plc",
    "BUACEMENT": "BUA Cement Plc",
    "AIRTELAFRI": "Airtel Africa Plc",
    "STANBIC": "Stanbic IBTC Holdings Plc",
    "FIDELITYBK": "Fidelity Bank Plc",
    "FCMB": "FCMB Group Plc",
    "FIRSTHOLDCO": "First HoldCo Plc",
    "FBNH": "FBN Holdings Plc",
    "OKOMUOIL": "Okomu Oil Palm Plc",
    "PRESCO": "Presco Plc",
    "NESTLE": "Nestle Nigeria Plc",
    "GUINNESS": "Guinness Nigeria Plc",
    "NB": "Nigerian Breweries Plc",
    "NIDF": "Nigeria Infrastructure Debt Fund",
    "AFRIPRUD": "Africa Prudential Plc",
    "HONYFLOUR": "Honeywell Flour Mill Plc",
    "DANGSUGAR": "Dangote Sugar Refinery Plc",
    "WAPCO": "Lafarge Africa Plc",
    "TRANSCORP": "Transcorp Plc",
    "CUSTODIAN": "Custodian Investment Plc",
    "UCAP": "United Capital Plc",
    "VFDGROUP": "VFD Group Plc",
    "CAP": "Chemical and Allied Products Plc",
    "BETAGLAS": "Beta Glass Plc",
    "UNILEVER": "Unilever Nigeria Plc",
    "FLOURMILL": "Flour Mills of Nigeria Plc",
    "NASCON": "NASCON Allied Industries Plc",
    "CADBURY": "Cadbury Nigeria Plc",
    "VITAFOAM": "Vitafoam Nigeria Plc",
    "CUTIX": "Cutix Plc",
    "WEMABANK": "Wema Bank Plc",
    "IKEJAHOTEL": "Ikeja Hotel Plc",
    "REDSTAREX": "Red Star Express Plc",
    "UPL": "University Press Plc",
    "LEARNAFRCA": "Learn Africa Plc",
    "AIICO": "AIICO Insurance Plc",
    "TIP": "The Initiates Plc",
    "CORNERST": "Cornerstone Insurance Plc",
    "MANSARD": "AXA Mansard Insurance Plc",
    "JAIZBANK": "Jaiz Bank Plc",
    "MAYBAKER": "May and Baker Nigeria Plc",
    "FIDSON": "Fidson Healthcare Plc",
    "PZ": "P.Z. Cussons Nigeria Plc",
    "NGXGROUP": "Nigerian Exchange Group Plc",
    "TRANSCOHOT": "Transcorp Hotels Plc",
    "JBERGER": "Julius Berger Nigeria Plc",
    "TOTAL": "TotalEnergies Marketing Nigeria Plc",
    "ETI": "Ecobank Transnational Incorporated",
    "MBENEFIT": "Mutual Benefits Assurance Plc",
    "SUNUASSUR": "Sunu Assurances Nigeria Plc",
    "JAPAULGOLD": "Japaul Gold and Ventures Plc",
    "UNIVINSURE": "Universal Insurance Plc",
    "ABBEYBDS": "Abbey Mortgage Bank Plc",
}

DATE_FIELDS = (
    "qualification_date",
    "payment_date",
    "closure_date",
    "announcement_date",
)

MERGE_FIELDS = DATE_FIELDS + ("registrar",)

# ---------------------------------------------------------------------------
# Tier 1: Corporate action / dividend announcement sources
# These are the most authoritative NGX filings — single source is enough.
# ---------------------------------------------------------------------------
TIER1_SIGNALS = (
    "CORPORATE_ACTION_ANNOUNCEMENT",
    "CORPORATE_ACTIONS_ANNOUNCEMENT",
    "DIVIDEND_ANNOUNCEMENT",
    "INTERIM_DIVIDEND_ANNOUNCEMENT",
    "FINAL_DIVIDEND_ANNOUNCEMENT",
    "NGX_NOTIFICATION",
    "DISTRIBUTION_PAYMENT",
    "NGX_DIV_ANNOUNCEMENT",
    "CORPORATE_DISCLOSURE",
)

# ---------------------------------------------------------------------------
# Tier 2: AGM resolutions — official shareholder approval of dividend
# ---------------------------------------------------------------------------
TIER2_SIGNALS = (
    "AGM_RESOLUTION",
    "AGM_RESOLUTIONS",
    "RESOLUTIONS_PASSED_AT",
    "RESOLUTIONS_OF_THE",
    "ANNUAL_GENERAL_MEETING_RESOLUTION",
    "OUTCOME_OF_THE",
    "OUTCOME_OF_BOARD",
    "POST_BOARD_MEETING",
)

# ---------------------------------------------------------------------------
# Tier 3: Sources that require corroboration before publishing
# ---------------------------------------------------------------------------
TIER3_SIGNALS = (
    "FINANCIAL_STATEMENT",
    "EARNINGS_RELEASE",
    "PRESS_RELEASE",
    "EARNINGS_PRESS_RELEASE",
    "QUARTER_",
    "HALF_YEAR",
    "FULL_YEAR",
    "ANNUAL_REPORT",
    "UNAUDITED_RESULTS",
    "AUDITED_RESULTS",
    "UFS",
)


def _float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _ticker(row: Mapping) -> str:
    return (row.get("ticker") or "").upper().strip()


def _currency(row: Mapping) -> str:
    return (row.get("currency") or "NGN").upper().strip()


def _row_source(row: Mapping) -> str:
    return (row.get("source_url") or "").strip()


def _normalize_for_matching(row: Mapping) -> str:
    """Combine source_title and source_url, normalize to uppercase underscores."""
    title = (row.get("source_title") or "")
    url = (row.get("source_url") or "")
    combined = f"{title} {url}".upper()
    combined = combined.replace(" ", "_").replace("-", "_").replace("/", "_")
    return combined


def _get_tier(row: Mapping) -> int:
    """
    Classify a source document into a publication tier.
    Returns 1 (most authoritative), 2, or 3 (needs corroboration).
    """
    normalized = _normalize_for_matching(row)

    if any(signal in normalized for signal in TIER1_SIGNALS):
        return 1

    if any(signal in normalized for signal in TIER2_SIGNALS):
        return 2

    # Check for AGM notice with dividend data — treat as Tier 2
    if "ANNUAL_GENERAL_MEETING" in normalized or "NOTICES_OF" in normalized:
        return 2

    if any(signal in normalized for signal in TIER3_SIGNALS):
        return 3

    # Default: treat as Tier 2 if we can't classify
    return 2


def _same_candidate_event(a: Mapping, b: Mapping) -> bool:
    if not _ticker(a) or _ticker(a) != _ticker(b):
        return False

    if _currency(a) != _currency(b):
        return False

    aa = _float(a.get("dividend_per_share"))
    bb = _float(b.get("dividend_per_share"))

    if aa <= 0 or bb <= 0 or not amounts_match(aa, bb):
        return False

    if not compatible_type(
        a.get("dividend_type", ""),
        b.get("dividend_type", ""),
    ):
        return False

    if not date_proximity_ok(a, b, max_days=180):
        return False

    return True


def _has_exact_date_anchor(a: Mapping, b: Mapping) -> bool:
    for field in DATE_FIELDS:
        av = (a.get(field) or "").strip()
        bv = (b.get(field) or "").strip()
        if av and bv and av == bv:
            return True
    return False


def _published_duplicate(row: Mapping, published_rows) -> bool:
    for pub in published_rows or []:
        if not isinstance(pub, Mapping):
            continue
        if not _same_candidate_event(row, pub):
            continue
        if (
            _row_source(row)
            and _row_source(pub)
            and _row_source(row) == _row_source(pub)
        ):
            return True
        if _has_exact_date_anchor(row, pub):
            return True
    return False


def _connected_components(rows):
    rows = list(rows)
    seen = set()
    components = []

    for i in range(len(rows)):
        if i in seen:
            continue
        stack = [i]
        seen.add(i)
        component = []
        while stack:
            idx = stack.pop()
            component.append(rows[idx])
            for j in range(len(rows)):
                if j in seen:
                    continue
                if _same_candidate_event(rows[idx], rows[j]):
                    seen.add(j)
                    stack.append(j)
        components.append(component)

    return components


def _conflicting_values(rows, field):
    values = {
        str(row.get(field) or "").strip()
        for row in rows
        if str(row.get(field) or "").strip()
    }
    return values if len(values) > 1 else set()


def _merge_component(rows):
    """Build one merged row, taking the best value for each field."""
    ranked = sorted(
        rows,
        key=lambda row: (
            _get_tier(row),  # Lower tier number = higher quality
            -source_strength(row.get("source_title", "")),
            -sum(bool(row.get(field)) for field in MERGE_FIELDS),
        ),
    )

    merged = dict(ranked[0])
    conflicts = []

    for field in MERGE_FIELDS:
        conflict = _conflicting_values(rows, field)
        if conflict:
            # For date fields, only flag as conflict if dates differ significantly
            if field in DATE_FIELDS:
                dates = [v for v in conflict if v]
                if len(dates) > 1:
                    # Allow if dates are within 3 days of each other
                    try:
                        from datetime import date
                        parsed = [date.fromisoformat(d) for d in dates]
                        if max(parsed) - min(parsed) > __import__('datetime').timedelta(days=3):
                            conflicts.append(field)
                            continue
                        # Use earliest date for qualification, latest for payment
                        if field == "qualification_date":
                            merged[field] = min(dates)
                        else:
                            merged[field] = max(dates)
                    except Exception:
                        conflicts.append(field)
                        continue
            else:
                conflicts.append(field)
                continue

        if not merged.get(field):
            for row in ranked:
                if row.get(field):
                    merged[field] = row.get(field)
                    break

    statuses = [(row.get("status") or "").lower().strip() for row in rows]
    if "approved" in statuses:
        merged["status"] = "approved"
    elif "declared" in statuses:
        merged["status"] = "declared"

    return merged, conflicts


def _unique_sources(rows):
    return {_row_source(row) for row in rows if _row_source(row)}


def _has_independent_pair(rows):
    for i, left in enumerate(rows):
        for right in rows[i + 1:]:
            if independent_source(left, right):
                return True
    return False


def _best_tier(component):
    """Return the best (lowest) tier number among all rows in a component."""
    return min(_get_tier(r) for r in component)


def resolve_pending_events(pending_rows, published_rows=None):
    """
    Resolve pending rows using tiered publication rules.

    Returns:
      promoted_rows, remaining_pending_rows, stats
    """
    published_rows = list(published_rows or [])
    clean = []
    stale_published_duplicates = 0

    for row in pending_rows or []:
        if not isinstance(row, Mapping):
            continue
        row = dict(row)
        if _published_duplicate(row, published_rows):
            stale_published_duplicates += 1
            continue
        clean.append(row)

    clusterable = []
    passthrough = []

    for row in clean:
        amount = _float(row.get("dividend_per_share"))
        if not _ticker(row) or amount <= 0 or not row.get("dividend_type"):
            passthrough.append(row)
        else:
            clusterable.append(row)

    components = _connected_components(clusterable)

    promoted = []
    remaining = list(passthrough)
    blocked_conflict = 0
    blocked_single_source = 0
    blocked_incomplete = 0
    blocked_safety = 0

    for component in components:
        merged, conflicts = _merge_component(component)

        # Safety hold: conflicting non-date fields
        if conflicts:
            blocked_conflict += 1
            remaining.extend(component)
            continue

        currency = _currency(merged)
        amount = _float(merged.get("dividend_per_share"))

        # Safety hold: sub-1-kobo NGN amounts
        if suspicious_tiny_ngn(amount, currency):
            blocked_safety += 1
            remaining.extend(component)
            continue

        # Determine best tier among all sources in this cluster
        best_tier = _best_tier(component)
        unique_src = _unique_sources(component)
        has_multi = len(unique_src) >= 2 and _has_independent_pair(component)

        # Tier 3 sources always require corroboration
        if best_tier >= 3 and not has_multi:
            blocked_single_source += 1
            remaining.extend(component)
            continue

        # Check completeness — must have both dates
        complete = (
            bool(_ticker(merged))
            and amount > 0
            and bool(merged.get("dividend_type"))
            and bool(merged.get("qualification_date"))
            and bool(merged.get("payment_date"))
        )

        if not complete:
            blocked_incomplete += 1
            remaining.extend(component)
            continue

        # Tier 1 and 2: publish with single source if complete
        # Tier 3: already handled above (needs multi-source)
        merged["confidence"] = "high" if best_tier == 1 else "medium"
        merged["resolution"] = f"tier{best_tier}_auto_publish"
        # Ensure numeric types
        try:
            merged["dividend_per_share"] = float(merged.get("dividend_per_share") or 0)
        except Exception:
            merged["dividend_per_share"] = 0.0
        # Clean company name from ticker lookup
        clean_name = _TICKER_TO_COMPANY.get(_ticker(merged), "")
        if clean_name:
            merged["company"] = clean_name
        promoted.append(merged)

    stats = {
        "input_rows": len(list(pending_rows or [])),
        "stale_published_duplicates_removed": stale_published_duplicates,
        "clusters_considered": len(components),
        "promoted": len(promoted),
        "blocked_conflict": blocked_conflict,
        "blocked_single_source": blocked_single_source,
        "blocked_incomplete": blocked_incomplete,
        "blocked_safety": blocked_safety,
        "remaining_rows": len(remaining),
    }

    return promoted, remaining, stats

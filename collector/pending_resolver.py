"""
collector/pending_resolver.py — Patch 39

Conservative resolver for already-discovered pending dividend evidence.

It never discovers new documents and never relaxes publication safety.
A pending event is promoted only when independent official PDFs corroborate
the same ticker/amount/type and their populated fields do not conflict.
"""

from collections import defaultdict
from typing import Mapping

from .reconcile import (
    amounts_match,
    compatible_type,
    date_proximity_ok,
    has_strong_corroboration,
    independent_source,
    source_strength,
    suspicious_tiny_ngn,
)

DATE_FIELDS = (
    "qualification_date",
    "payment_date",
    "closure_date",
    "announcement_date",
)

MERGE_FIELDS = DATE_FIELDS + ("registrar",)


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
    """
    Remove stale pending duplicates only when there is a strong identity anchor:
    same source URL or at least one exact matching event date.
    """
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
    """
    Build one conservative merged row.

    Returns:
      merged_row, conflict_fields
    """
    ranked = sorted(
        rows,
        key=lambda row: (
            source_strength(row.get("source_title", "")),
            sum(bool(row.get(field)) for field in MERGE_FIELDS),
        ),
        reverse=True,
    )

    merged = dict(ranked[0])
    conflicts = []

    for field in MERGE_FIELDS:
        conflict = _conflicting_values(rows, field)
        if conflict:
            conflicts.append(field)
            continue

        if not merged.get(field):
            for row in ranked:
                if row.get(field):
                    merged[field] = row.get(field)
                    break

    # Prefer a stronger status where available.
    statuses = [
        (row.get("status") or "").lower().strip()
        for row in rows
    ]
    if "approved" in statuses:
        merged["status"] = "approved"
    elif "declared" in statuses:
        merged["status"] = "declared"

    return merged, conflicts


HIGH_QUALITY_SOURCE_SIGNALS = (
    "CORPORATE_ACTION_ANNOUNCEMENT",
    "CORPORATE_ACTIONS_ANNOUNCEMENT",
    "DIVIDEND_ANNOUNCEMENT",
    "INTERIM_DIVIDEND_ANNOUNCEMENT",
    "FINAL_DIVIDEND_ANNOUNCEMENT",
    "NGX_NOTIFICATION",
    "DISTRIBUTION_PAYMENT",
)


def _is_high_quality_source(row: Mapping) -> bool:
    """True when the source is a dedicated corporate action/dividend document."""
    title = (row.get("source_title") or row.get("source_url") or "").upper()
    return any(signal in title for signal in HIGH_QUALITY_SOURCE_SIGNALS)


def _unique_sources(rows):
    return {
        _row_source(row)
        for row in rows
        if _row_source(row)
    }


def _has_independent_pair(rows):
    for i, left in enumerate(rows):
        for right in rows[i + 1:]:
            if independent_source(left, right):
                return True
    return False


def resolve_pending_events(pending_rows, published_rows=None):
    """
    Resolve duplicate/complementary pending rows.

    Returns:
      promoted_rows,
      remaining_pending_rows,
      stats
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

    # Cluster only records with usable identity fields.
    clusterable = []
    passthrough = []

    for row in clean:
        amount = _float(row.get("dividend_per_share"))
        if (
            not _ticker(row)
            or amount <= 0
            or not row.get("dividend_type")
        ):
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

        if conflicts:
            blocked_conflict += 1
            remaining.extend(component)
            continue

        currency = _currency(merged)
        amount = _float(merged.get("dividend_per_share"))

        if suspicious_tiny_ngn(amount, currency):
            blocked_safety += 1
            remaining.extend(component)
            continue

        # Patch 39: Relaxed corroboration rule.
        # Primary rule: require 2 independent sources (original conservative behaviour).
        # Fallback: allow single-source promotion when:
        #   1. The source is a high-quality corporate action/dividend document
        #   2. The event is complete (ticker, amount, qual date, pay date)
        #   3. The amount passes sanity checks (already done above)
        unique_src = _unique_sources(component)
        has_multi = len(unique_src) >= 2 and _has_independent_pair(component)
        has_hq_single = (
            len(unique_src) == 1
            and any(_is_high_quality_source(r) for r in component)
        )

        if not has_multi and not has_hq_single:
            blocked_single_source += 1
            remaining.extend(component)
            continue

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

        # At least one independent strong source must support the merged event.
        if not has_strong_corroboration(merged, component):
            blocked_single_source += 1
            remaining.extend(component)
            continue

        merged["confidence"] = "high"
        merged["resolution"] = "pending_cross_document"
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

from types import SimpleNamespace

from collector.run import (
    qualification_payment_gap_suspect,
    QUALIFICATION_TO_PAYMENT_GAP_LIMIT_DAYS,
)


def _event(qualification_date, payment_date):
    return SimpleNamespace(
        qualification_date=qualification_date,
        payment_date=payment_date,
    )


def test_flags_two_filings_merged_into_one_event():
    # Real case: AFRIPRUD's 0.10/share interim carried a qualification date
    # from a July 2025 filing against a payment actually made in July 2026.
    # The payment date and amount were both correct; only the qualification
    # date was wrong, having been pulled from a different filing.
    event = _event("2025-08-08", "2026-07-20")
    assert qualification_payment_gap_suspect(event) is True


def test_does_not_flag_legitimate_reit_year_end_gap():
    # UHOMREIT's year-end record date genuinely runs ~164 days ahead of
    # payment. This must stay well clear of the limit so it never gets
    # caught by a future lowering of the threshold without a deliberate
    # decision to do so.
    event = _event("2024-12-31", "2025-06-13")
    assert qualification_payment_gap_suspect(event) is False
    gap_days = 164
    assert gap_days < QUALIFICATION_TO_PAYMENT_GAP_LIMIT_DAYS


def test_does_not_flag_typical_short_gap():
    event = _event("2026-08-20", "2026-09-07")
    assert qualification_payment_gap_suspect(event) is False


def test_missing_dates_are_never_flagged_here():
    # Missing-date detection belongs to validate_event / review_reason_codes;
    # this check must not manufacture a second failure mode for the same
    # underlying problem.
    assert qualification_payment_gap_suspect(_event("", "2026-09-07")) is False
    assert qualification_payment_gap_suspect(_event("2026-08-20", "")) is False
    assert qualification_payment_gap_suspect(_event("", "")) is False


def test_unparsable_dates_are_not_flagged():
    assert qualification_payment_gap_suspect(_event("not-a-date", "2026-09-07")) is False


def test_gap_exactly_at_limit_is_not_flagged():
    event = _event("2026-01-01", "2026-06-30")  # 180 days
    gap_days = (
        __import__("datetime").date(2026, 6, 30)
        - __import__("datetime").date(2026, 1, 1)
    ).days
    assert gap_days == QUALIFICATION_TO_PAYMENT_GAP_LIMIT_DAYS
    assert qualification_payment_gap_suspect(event) is False


def test_gap_one_day_past_limit_is_flagged():
    event = _event("2026-01-01", "2026-07-01")  # 181 days
    assert qualification_payment_gap_suspect(event) is True

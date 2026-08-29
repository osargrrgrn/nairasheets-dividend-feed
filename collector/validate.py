from datetime import date

def validate_event(event):
    errors = []

    if not event.ticker:
        errors.append("missing ticker")
    if not event.company:
        errors.append("missing company")
    if event.dividend_per_share <= 0:
        errors.append("missing/invalid dividend per share")
    if event.currency not in {"NGN", "USD", "GBP", "EUR"}:
        errors.append("unknown currency")
    if not event.qualification_date:
        errors.append("missing qualification date")
    if not event.payment_date:
        errors.append("missing payment date")

    if event.qualification_date and event.payment_date:
        if event.payment_date < event.qualification_date:
            errors.append("payment date before qualification date")

    # Flag absurd values rather than silently publishing them.
    if event.currency == "NGN" and event.dividend_per_share > 1000:
        errors.append("suspiciously large NGN dividend")
    if event.currency == "USD" and event.dividend_per_share > 10:
        errors.append("suspiciously large USD dividend")

    return errors

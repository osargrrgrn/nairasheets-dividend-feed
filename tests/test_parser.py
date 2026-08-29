from collector.parse import parse_dividend_pdf

def test_ngxgroup_style_notice():
    text = """
    NIGERIAN EXCHANGE GROUP PLC hereby announce as follows:
    Proposed Dividend
    A Dividend of ₦2 (Two Naira) per ordinary share of 0.50 kobo each,
    subject to shareholders' approval and deduction of appropriate withholding tax,
    will be paid to shareholders whose names appear in the Register of Members as at
    the close of business on 10 April 2026.
    Closure of Register
    13 April 2026
    Qualification Date
    10 April 2026
    Payment Date
    29 April 2026
    """
    event = parse_dividend_pdf(
        text,
        "https://doclib.ngxgroup.com/Financial_NewsDocs/example.pdf",
        "NIGERIAN EXCHANGE GROUP PLC - DIVIDEND ANNOUNCEMENT",
        "NGXGROUP",
    )
    assert event.dividend_per_share == 2.0
    assert event.currency == "NGN"
    assert event.qualification_date == "2026-04-10"
    assert event.payment_date == "2026-04-29"

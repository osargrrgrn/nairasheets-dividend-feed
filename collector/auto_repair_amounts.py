"""Automatically repair stale published dividend amounts from source PDFs.

This is deliberately evidence-based: no ticker-specific values and no blind
x100 correction.  Published rows whose amount looks like a likely scale error
are reparsed from their official NGX source.  A row is replaced only when the
fresh parse is complete, passes validation, matches the same corporate action
by ticker + qualification/payment dates, and the new amount is a plausible
10x/100x/1000x scale correction.
"""

from pathlib import Path
import math

from .pdf_extract import download_pdf_text, compact
from .parse import parse_dividend_pdf, make_event_id
from .tickers import resolve_ticker
from .validate import validate_event
from .publish import read_csv, write_csv, write_html
from .feed_integrity import validate_published_feed, finalize_published_rows

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "docs" / "dividends.csv"
HTML = ROOT / "docs" / "index.html"

# Tiny NGN values are the signature left by the old Naira/Kobo scale bug.
# Reparse all such rows; replacement still requires strict source evidence.
SUSPICIOUS_NGN_MAX = 0.099999


def _num(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _same_action(old, new):
    return (
        (old.get("ticker") or "").upper().strip()
        == (new.get("ticker") or "").upper().strip()
        and (old.get("qualification_date") or "").strip()
        == (new.get("qualification_date") or "").strip()
        and (old.get("payment_date") or "").strip()
        == (new.get("payment_date") or "").strip()
    )


def _scale_correction(old_amount, new_amount):
    if old_amount <= 0 or new_amount <= old_amount:
        return False
    ratio = new_amount / old_amount
    return any(math.isclose(ratio, scale, rel_tol=0.015, abs_tol=0.015)
               for scale in (10.0, 100.0, 1000.0))


def _reparse(row):
    url = (row.get("source_url") or "").strip()
    title = row.get("source_title") or ""
    text = compact(download_pdf_text(url))
    event = parse_dividend_pdf(text=text, source_url=url, source_title=title, ticker="")
    event.ticker = resolve_ticker(event.company, title) or (row.get("ticker") or "").upper().strip()
    event.event_id = make_event_id(
        event.ticker, event.company, event.qualification_date, event.payment_date,
        event.dividend_per_share, event.dividend_type,
    )
    errors = validate_event(event)
    if errors:
        raise ValueError(errors)
    return event.to_dict()


def main():
    rows = read_csv(FEED)
    suspicious = [
        r for r in rows
        if (r.get("currency") or "NGN").upper() == "NGN"
        and 0 < _num(r.get("dividend_per_share")) <= SUSPICIOUS_NGN_MAX
        and (r.get("source_url") or "").strip()
    ]
    print(f"[AutoAmountRepair] suspicious published rows: {len(suspicious)}")

    replacements = {}
    for old in suspicious:
        url = (old.get("source_url") or "").strip()
        try:
            fresh = _reparse(old)
            old_amt = _num(old.get("dividend_per_share"))
            new_amt = _num(fresh.get("dividend_per_share"))
            if not _same_action(old, fresh):
                print(f"[AutoAmountRepair] HOLD {old.get('ticker')}: source reparse changed event dates")
                continue
            if not _scale_correction(old_amt, new_amt):
                continue
            replacements[url] = fresh
            print(f"[AutoAmountRepair] REPAIR {old.get('ticker')}: {old_amt:g} -> {new_amt:g} NGN")
        except Exception as exc:
            print(f"[AutoAmountRepair] HOLD {old.get('ticker')}: {exc!r}")

    if not replacements:
        print("[AutoAmountRepair] no evidence-backed repairs")
        return

    updated = [r for r in rows if (r.get("source_url") or "").strip() not in replacements]
    updated.extend(replacements.values())
    updated, rejected = finalize_published_rows(updated)
    rejected_sources = {(r.get("source_url") or "").strip() for r in rejected}
    bad = set(replacements) & rejected_sources
    if bad:
        raise RuntimeError(f"repaired rows rejected by finalizer: {sorted(bad)}")

    errors, warnings = validate_published_feed(updated)
    if errors:
        raise RuntimeError(f"feed integrity failed after amount repair: {errors}")

    write_csv(FEED, updated)
    write_html(HTML, updated)
    print(f"[AutoAmountRepair] published repairs: {len(replacements)} warnings={len(warnings)}")


if __name__ == "__main__":
    main()

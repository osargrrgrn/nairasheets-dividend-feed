"""
collector/price_history.py — Patch 57

Records one closing price per ticker per trading day.

docs/prices.csv holds only "now" and is overwritten every half hour, so there
is no history to chart. This runs once, after the NGX close, reads whatever
prices.csv currently holds and files it as that day's close.

Writes two files:

  docs/price_history.csv    date,ticker,close        — the archive, one row per
                            ticker per day, trimmed to KEEP_DAYS trading days.

  docs/price_sparkline.csv  ticker,c1..cN            — the same data pivoted so
                            each ticker is one row of closes, oldest first.
                            This is the one the tracker imports: a spreadsheet
                            can hand a single row straight to SPARKLINE(),
                            which it cannot do with a hundred thousand rows.

Two things it refuses to record, because a wrong closing price is worse than
a missing one:

  * a stale prices.csv — if the price job failed, its timestamp is from an
    earlier day and today's close is simply unknown.
  * a non-trading day — on a public holiday the scrape still returns the last
    session's prices with today's timestamp. Those are caught by comparing
    against the previous stored day: if essentially every ticker is unchanged,
    no session happened.

Usage:
    python -u -m collector.price_history
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRICES_CSV = ROOT / "docs" / "prices.csv"
HISTORY_CSV = ROOT / "docs" / "price_history.csv"
SPARK_CSV = ROOT / "docs" / "price_sparkline.csv"

# How much archive to keep. ~250 trading days a year.
KEEP_DAYS = 400

# How many closes each sparkline row carries, oldest first.
SPARK_DAYS = 90

# A day on which this share of tickers is unchanged from the previous stored
# day is treated as a non-trading day.
UNCHANGED_HOLIDAY_RATIO = 0.98

# Below this many tickers the file is assumed broken and nothing is recorded.
MIN_TICKERS = 50


def _read_prices():
    """Return (as_of_date, {ticker: 'price string'}) from docs/prices.csv."""
    if not PRICES_CSV.exists():
        return None, {}

    prices = {}
    as_of = None
    with PRICES_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            ticker = (row.get("ticker") or "").strip().upper()
            raw = (row.get("price") or "").strip()
            if not ticker or not raw:
                continue
            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                continue
            if value <= 0:
                continue
            prices[ticker] = f"{value:.2f}"

            if as_of is None:
                stamp = (row.get("updated_at") or "").strip()
                # "2026-09-21 15:51 UTC"
                if len(stamp) >= 10:
                    as_of = stamp[:10]

    return as_of, prices


def _read_history():
    """Return {date: {ticker: 'close'}} from the archive."""
    history = {}
    if not HISTORY_CSV.exists():
        return history

    with HISTORY_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            day = (row.get("date") or "").strip()
            ticker = (row.get("ticker") or "").strip().upper()
            close = (row.get("close") or "").strip()
            if not day or not ticker or not close:
                continue
            history.setdefault(day, {})[ticker] = close

    return history


def _looks_like_a_holiday(today_prices, previous_prices):
    """True when nearly every ticker is unchanged from the previous session."""
    if not previous_prices:
        return False

    shared = set(today_prices) & set(previous_prices)
    if len(shared) < MIN_TICKERS:
        return False

    same = sum(1 for t in shared if today_prices[t] == previous_prices[t])
    return (same / len(shared)) >= UNCHANGED_HOLIDAY_RATIO


def _write_history(history):
    HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    days = sorted(history)[-KEEP_DAYS:]

    with HISTORY_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "ticker", "close"])
        for day in days:
            for ticker in sorted(history[day]):
                writer.writerow([day, ticker, history[day][ticker]])

    return len(days)


def _write_sparkline(history):
    """
    Pivot the archive so each ticker is one row of closes, oldest first.

    A ticker with no trade on a given day carries its previous close forward,
    which is what a price line should do. Days before a ticker's first close
    are left blank so the sparkline starts where the data does.
    """
    days = sorted(history)[-SPARK_DAYS:]
    if not days:
        return 0, 0

    tickers = sorted({t for day in days for t in history[day]})

    SPARK_CSV.parent.mkdir(parents=True, exist_ok=True)
    with SPARK_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ticker"] + days)
        for ticker in tickers:
            row = [ticker]
            last = ""
            for day in days:
                close = history[day].get(ticker, "")
                if close:
                    last = close
                row.append(last)
            writer.writerow(row)

    return len(tickers), len(days)


def main() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    as_of, prices = _read_prices()

    if not prices:
        print("[History] docs/prices.csv is missing or unreadable — nothing recorded", flush=True)
        return

    if len(prices) < MIN_TICKERS:
        print(f"[History] only {len(prices)} tickers in prices.csv "
              f"(need {MIN_TICKERS}) — nothing recorded", flush=True)
        return

    if as_of != today:
        print(f"[History] prices.csv is stamped {as_of}, today is {today} — "
              f"the price job did not run, nothing recorded", flush=True)
        return

    history = _read_history()
    previous_days = sorted(d for d in history if d < today)
    previous = history.get(previous_days[-1], {}) if previous_days else {}

    if _looks_like_a_holiday(prices, previous):
        print(f"[History] every price matches {previous_days[-1]} — "
              f"no trading session today, nothing recorded", flush=True)
        return

    replacing = today in history
    history[today] = prices

    days_kept = _write_history(history)
    spark_tickers, spark_days = _write_sparkline(history)

    verb = "replaced" if replacing else "recorded"
    print(f"[History] {verb} {len(prices)} closes for {today}", flush=True)
    print(f"[History] archive now holds {days_kept} trading days", flush=True)
    print(f"[History] sparkline file: {spark_tickers} tickers x {spark_days} days", flush=True)


if __name__ == "__main__":
    main()

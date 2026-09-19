"""
collector/publish_prices.py — Patch 55

Entry point for the price-only workflow:  python -u -m collector.publish_prices

Kept separate from collector.run so prices can refresh through the trading
day on a five-minute job without touching the dividend pipeline.
"""

from .prices import update_prices


def main() -> None:
    debug = {}
    rows = update_prices(debug)
    if rows == 0:
        print("[Prices] nothing written this run", flush=True)


if __name__ == "__main__":
    main()

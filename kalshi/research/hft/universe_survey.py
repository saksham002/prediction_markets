"""
Survey the tradable universe for the HFT passive/aggressive strategy:
all open two-market "A vs B" sports pairs, annotated with series fee type
(maker fees or not), 24h volume, and current top-of-book spread per leg.

Output: CSV table + per-series summary to decide venue selection.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.signals.pairs import build_pair_metadata
from research.hft.paths import HFT_DATA
from src.utils.api import fetch_series_fee, paginate
from src.utils.timestamp import Timestamp

OUTPUT_DIR = HFT_DATA / "universe"


def leg_quotes(market: dict):
    """(yes_bid, yes_ask, spread) in dollars from a nested REST market object."""
    yb = market["yes_bid_dollars"]
    ya = market["yes_ask_dollars"]
    if not yb or not ya:
        return None, None, None
    yb, ya = float(yb), float(ya)
    if yb <= 0 or ya >= 1:
        return yb, ya, None
    return yb, ya, round(ya - yb, 6)


def main():
    parser = argparse.ArgumentParser(description = "Survey open sports pairs for HFT venue selection")
    parser.add_argument("-c", "--category", type = str, default = "Sports", help = "Event category")
    args = parser.parse_args()

    print("Fetching active events with nested markets...")
    events = paginate(
        "events",
        params = {"status": "open", "with_nested_markets": True},
        key = "events",
        max_per_page = 200,
    )
    print(f"  {len(events)} active events found")

    series_fee_cache: dict[str, tuple[float, str]] = {}
    rows = []
    for event in events:
        if event.get("category") != args.category:
            continue
        pair = build_pair_metadata(event["event_ticker"], event["title"], event["markets"])
        if pair is None:
            continue

        series = pair["first_ticker"].split("-", 1)[0]
        if series not in series_fee_cache:
            series_fee_cache[series] = fetch_series_fee(series)
        multiplier, fee_type = series_fee_cache[series]

        markets_by_ticker = {m["ticker"]: m for m in event["markets"]}
        f_yb, f_ya, f_spread = leg_quotes(markets_by_ticker[pair["first_ticker"]])
        s_yb, s_ya, s_spread = leg_quotes(markets_by_ticker[pair["second_ticker"]])
        total_volume = sum(float(m["volume_24h_fp"]) for m in event["markets"])

        rows.append({
            "event_ticker": pair["event_ticker"],
            "series": series,
            "title": pair["title"],
            "fee_type": fee_type,
            "fee_multiplier": multiplier,
            "volume_24h": total_volume,
            "first_yes_bid": f_yb,
            "first_yes_ask": f_ya,
            "first_spread": f_spread,
            "second_yes_bid": s_yb,
            "second_yes_ask": s_ya,
            "second_spread": s_spread,
            "first_close_time": pair["first_close_time"],
        })

    OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
    ts_str = Timestamp.now().et.strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"universe_{ts_str}.csv"
    with open(out_path, "w", newline = "") as f:
        writer = csv.DictWriter(f, fieldnames = list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} pairs to {out_path}\n")

    # Per-series summary: pair count, volume, how many pairs are tight (1 tick both legs)
    by_series = defaultdict(list)
    for r in rows:
        by_series[r["series"]].append(r)

    print(f"{'series':<22} {'fee_type':<28} {'pairs':>5} {'vol24h':>12} {'tight(1c both)':>14}")
    print("-" * 90)
    for series, srows in sorted(by_series.items(), key = lambda kv: -sum(r["volume_24h"] for r in kv[1])):
        vol = sum(r["volume_24h"] for r in srows)
        tight = sum(
            1 for r in srows
            if r["first_spread"] is not None and r["second_spread"] is not None
            and r["first_spread"] <= 0.011 and r["second_spread"] <= 0.011
        )
        fee_type = srows[0]["fee_type"]
        print(f"{series:<22} {fee_type:<28} {len(srows):>5} {vol:>12.0f} {tight:>14}")

    print("\nTop 20 pairs by 24h volume:")
    print(f"{'event_ticker':<38} {'series':<16} {'fee_type':<28} {'vol24h':>10} {'sprd1':>7} {'sprd2':>7}")
    print("-" * 110)
    for r in sorted(rows, key = lambda r: -r["volume_24h"])[:20]:
        s1 = f"{r['first_spread']:.2f}" if r["first_spread"] is not None else "-"
        s2 = f"{r['second_spread']:.2f}" if r["second_spread"] is not None else "-"
        print(f"{r['event_ticker']:<38} {r['series']:<16} {r['fee_type']:<28} {r['volume_24h']:>10.0f} {s1:>7} {s2:>7}")


if __name__ == "__main__":
    main()

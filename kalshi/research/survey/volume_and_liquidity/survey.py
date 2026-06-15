"""
Survey of Kalshi exchange: active market/event/series counts,
volume distributions, and spread statistics bucketed by volume.

Flags:
  --top-n N   Print top N markets by volume and exit (default 10).
              Skips full survey when set.
"""

import argparse
import json
import time
from pathlib import Path

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
OUTPUT_DIR = Path(__file__).parent / "data"
RATE_LIMIT_DELAY = 0.15


def api_get(url, params = None, timeout = 30):
    """GET with retry on 429."""
    for attempt in range(5):
        resp = requests.get(url, params = params, timeout = timeout)
        if resp.status_code == 429:
            wait = 2 ** attempt
            print(f"    Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()


def paginate(endpoint, params = None, key = None, max_per_page = 1000):
    """Fetch all pages from a paginated Kalshi endpoint."""
    if key is None:
        key = endpoint.strip("/").split("/")[-1]
    params = dict(params or {})
    params["limit"] = max_per_page
    all_items = []
    cursor = None
    page = 0

    while True:
        if cursor:
            params["cursor"] = cursor
        resp = api_get(f"{BASE_URL}/{endpoint}", params = params)
        data = resp.json()
        items = data.get(key, [])
        all_items.extend(items)
        page += 1
        if page % 50 == 0:
            print(f"    ... {len(all_items)} items fetched so far")
        cursor = data.get("cursor", "")
        if not cursor or not items:
            break
        time.sleep(RATE_LIMIT_DELAY)

    return all_items


def fetch_active_markets(mve_filter = None):
    params = {"status": "open"}
    if mve_filter:
        params["mve_filter"] = mve_filter
    return paginate("markets", params = params, key = "markets")


def fetch_active_events():
    return paginate("events", params = {"status": "open"}, key = "events", max_per_page = 200)


def fetch_series():
    resp = api_get(f"{BASE_URL}/series", params = {"include_volume": True})
    return resp.json()["series"]


def compute_stats(values):
    if not values:
        return {}
    s = sorted(values)
    n = len(s)
    total = sum(s)
    mean = total / n
    median = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2
    quantiles = {}
    for q in [0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]:
        idx = int(q * (n - 1))
        quantiles[f"p{int(q * 100)}"] = round(s[idx], 4)

    return {
        "count": n,
        "total": round(total, 2),
        "mean": round(mean, 4),
        "median": round(median, 4),
        "min": round(s[0], 4),
        "max": round(s[-1], 4),
        "quantiles": quantiles,
    }


def compute_spread(market):
    """Compute yes and no spreads in dollars from market bid/ask."""
    yes_bid = market["yes_bid_dollars"]
    yes_ask = market["yes_ask_dollars"]
    no_bid = market["no_bid_dollars"]
    no_ask = market["no_ask_dollars"]

    yes_spread = None
    no_spread = None

    yb, ya = float(yes_bid), float(yes_ask)
    nb, na = float(no_bid), float(no_ask)

    if yb > 0 and ya > 0:
        yes_spread = round(ya - yb, 6)
    if nb > 0 and na > 0:
        no_spread = round(na - nb, 6)

    return yes_spread, no_spread


def bucket_volume(volume):
    """Assign a market to a volume bucket."""
    if volume <= 100:
        return "0-100"
    elif volume <= 1_000:
        return "101-1K"
    elif volume <= 10_000:
        return "1K-10K"
    elif volume <= 100_000:
        return "10K-100K"
    else:
        return "100K+"


BUCKET_ORDER = ["0-100", "101-1K", "1K-10K", "10K-100K", "100K+"]


def compute_volume_and_spread(markets, label):
    """Compute volume stats and spread-by-bucket for a list of markets."""
    market_volumes = [float(m["volume_fp"]) for m in markets]

    event_volumes = {}
    for m in markets:
        et = m["event_ticker"]
        event_volumes[et] = event_volumes.get(et, 0) + float(m["volume_fp"])

    market_vol_stats = compute_stats(market_volumes)
    event_vol_stats = compute_stats(list(event_volumes.values()))

    buckets = {}
    for m in markets:
        vol = float(m["volume_fp"])
        bucket = bucket_volume(vol)
        if bucket not in buckets:
            buckets[bucket] = {"yes_spreads": [], "no_spreads": [], "count": 0}
        buckets[bucket]["count"] += 1
        yes_spread, no_spread = compute_spread(m)
        if yes_spread is not None:
            buckets[bucket]["yes_spreads"].append(yes_spread)
        if no_spread is not None:
            buckets[bucket]["no_spreads"].append(no_spread)

    spread_stats = {}
    for bucket_name in BUCKET_ORDER:
        if bucket_name not in buckets:
            continue
        b = buckets[bucket_name]
        spread_stats[bucket_name] = {
            "n_markets": b["count"],
            "n_with_yes_spread": len(b["yes_spreads"]),
            "n_with_no_spread": len(b["no_spreads"]),
            "yes_spread": compute_stats(b["yes_spreads"]),
            "no_spread": compute_stats(b["no_spreads"]),
        }

    return market_vol_stats, event_vol_stats, spread_stats


def print_volume_stats(label, market_vol_stats, event_vol_stats):
    print(f"\n--- {label}: Market Volume (contracts) ---")
    for k, v in market_vol_stats.items():
        if k != "quantiles":
            print(f"  {k}: {v}")
    if "quantiles" in market_vol_stats:
        for q, v in market_vol_stats["quantiles"].items():
            print(f"  {q}: {v}")

    print(f"\n--- {label}: Event Volume (contracts) ---")
    for k, v in event_vol_stats.items():
        if k != "quantiles":
            print(f"  {k}: {v}")
    if "quantiles" in event_vol_stats:
        for q, v in event_vol_stats["quantiles"].items():
            print(f"  {q}: {v}")


def print_spread_stats(label, spread_stats):
    print(f"\n--- {label}: Spread by Volume Bucket (dollars) ---")
    for bucket_name in BUCKET_ORDER:
        if bucket_name not in spread_stats:
            continue
        stats = spread_stats[bucket_name]
        n = stats["n_markets"]
        yes_med = stats["yes_spread"].get("median", "N/A")
        no_med = stats["no_spread"].get("median", "N/A")
        yes_mean = stats["yes_spread"].get("mean", "N/A")
        no_mean = stats["no_spread"].get("mean", "N/A")
        print(f"  [{bucket_name:>8}] n={n:<6}  YES spread: median={yes_med:<8} mean={yes_mean:<8}  |  NO spread: median={no_med:<8} mean={no_mean:<8}")


def run_survey():
    print("Fetching non-multivariate markets...")
    standard_markets = fetch_active_markets(mve_filter = "exclude")
    print(f"  {len(standard_markets)} standard markets")

    print("Fetching multivariate markets...")
    mve_markets = fetch_active_markets(mve_filter = "only")
    print(f"  {len(mve_markets)} multivariate markets")

    all_markets = standard_markets + mve_markets

    print("Fetching active events...")
    events = fetch_active_events()
    print(f"  {len(events)} active events")

    print("Fetching series...")
    series = fetch_series()
    active_series_tickers = set()
    for e in events:
        if "series_ticker" in e:
            active_series_tickers.add(e["series_ticker"])
    print(f"  {len(series)} total series, {len(active_series_tickers)} with active events")

    # --- Compute stats for each category ---
    all_mkt_vol, all_evt_vol, all_spread = compute_volume_and_spread(all_markets, "All")
    std_mkt_vol, std_evt_vol, std_spread = compute_volume_and_spread(standard_markets, "Standard")
    mve_mkt_vol, mve_evt_vol, mve_spread = compute_volume_and_spread(mve_markets, "Multivariate")

    # --- Assemble results ---
    results = {
        "counts": {
            "active_markets_total": len(all_markets),
            "active_markets_standard": len(standard_markets),
            "active_markets_multivariate": len(mve_markets),
            "active_events": len(events),
            "total_series": len(series),
            "active_series": len(active_series_tickers),
        },
        "all": {
            "market_volume_stats": all_mkt_vol,
            "event_volume_stats": all_evt_vol,
            "spread_by_volume_bucket": all_spread,
        },
        "standard": {
            "market_volume_stats": std_mkt_vol,
            "event_volume_stats": std_evt_vol,
            "spread_by_volume_bucket": std_spread,
        },
        "multivariate": {
            "market_volume_stats": mve_mkt_vol,
            "event_volume_stats": mve_evt_vol,
            "spread_by_volume_bucket": mve_spread,
        },
    }

    OUTPUT_DIR.mkdir(parents = True, exist_ok = True)

    with open(OUTPUT_DIR / "survey_results.json", "w") as f:
        json.dump(results, f, indent = 2)
    print(f"\nResults saved to {OUTPUT_DIR / 'survey_results.json'}")

    with open(OUTPUT_DIR / "raw_markets.json", "w") as f:
        json.dump(all_markets, f, indent = 2)
    print(f"Raw market data saved to {OUTPUT_DIR / 'raw_markets.json'}")

    # --- Print summary ---
    print("\n" + "=" * 60)
    print("SURVEY SUMMARY")
    print("=" * 60)

    print(f"\nTotal active markets:       {len(all_markets)}")
    print(f"  Standard (non-MVE):       {len(standard_markets)}")
    print(f"  Multivariate (MVE):       {len(mve_markets)}")
    print(f"Active events:              {len(events)}")
    print(f"Active series:              {len(active_series_tickers)} (of {len(series)} total)")

    print_volume_stats("Standard", std_mkt_vol, std_evt_vol)
    print_spread_stats("Standard", std_spread)

    print_volume_stats("Multivariate", mve_mkt_vol, mve_evt_vol)
    print_spread_stats("Multivariate", mve_spread)

    print_volume_stats("All", all_mkt_vol, all_evt_vol)
    print_spread_stats("All", all_spread)


def _print_market_table(markets, label):
    print(f"\n{label}:")
    print(f"{'#':<4} {'Ticker':<35} {'Volume':>14} {'24h Volume':>14}  Title")
    print("-" * 125)
    for i, m in enumerate(markets, 1):
        vol = float(m["volume_fp"])
        vol_24h = float(m.get("volume_24h_fp", "0"))
        title = m.get("title", "")
        print(f"{i:<4} {m['ticker']:<35} {vol:>14,.0f} {vol_24h:>14,.0f}  {title}")


def print_top_markets(n: int):
    """Fetch all active markets and print top N by total and 24h volume."""
    print(f"Fetching active markets...")
    markets = fetch_active_markets()
    print(f"  {len(markets)} active markets found")

    by_total = sorted(markets, key = lambda m: float(m["volume_fp"]), reverse = True)[:n]
    by_24h = sorted(markets, key = lambda m: float(m.get("volume_24h_fp", "0")), reverse = True)[:n]

    _print_market_table(by_total, f"Top {n} by total volume")
    _print_market_table(by_24h, f"Top {n} by 24h volume")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description = "Kalshi exchange survey")
    parser.add_argument("--top-n", type = int, default = None, help = "Print top N markets by volume and exit (default 10)")
    args = parser.parse_args()

    if args.top_n is not None:
        print_top_markets(args.top_n)
    else:
        run_survey()

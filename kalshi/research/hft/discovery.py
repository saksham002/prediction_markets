"""
League-driven event discovery (robust replacement for title parsing).

The only criterion for interest is the SERIES (league). For every open event
in a target series:
  - 2 markets  -> a "pair" (two-outcome game). Leg orientation is the
    lexicographic ticker order — a pure sign convention for pair alphas,
    deterministic across processes, no title parsing involved.
  - 3..8 markets -> an "event" traded per-market (soccer win/draw/win etc).

Title formats ("A vs B", "Game 5: A at B", anything else) no longer matter.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.utils.api import paginate


def discover_league_events(series_csv: str, top_n: int):
    """Returns (pairs, events): top_n events by 24h volume across the given
    comma-separated series, classified by market count."""
    series_set = {s.strip() for s in series_csv.split(",") if s.strip()}
    raw = paginate(
        "events",
        params = {"status": "open", "with_nested_markets": True},
        key = "events",
        max_per_page = 200,
    )
    candidates = []
    for ev in raw:
        event_ticker = ev["event_ticker"]
        series = event_ticker.split("-", 1)[0]
        if series not in series_set:
            continue
        markets = ev.get("markets", [])
        if not 2 <= len(markets) <= 8:
            continue
        volume = sum(float(m.get("volume_24h_fp", 0) or 0) for m in markets)
        candidates.append((volume, ev, series, markets))
    candidates.sort(key = lambda c: -c[0])

    pairs, events = [], []
    for volume, ev, series, markets in candidates[:top_n]:
        tickers = sorted(m["ticker"] for m in markets)
        base = {
            "event_ticker": ev["event_ticker"],
            "title": ev.get("title", ev["event_ticker"]),
            "series": series,
            "volume": volume,
        }
        if len(markets) == 2:
            by_ticker = {m["ticker"]: m for m in markets}
            pairs.append({
                **base,
                "first_ticker": tickers[0],
                "second_ticker": tickers[1],
                "first_team": by_ticker[tickers[0]].get("yes_sub_title", tickers[0]),
                "second_team": by_ticker[tickers[1]].get("yes_sub_title", tickers[1]),
                "first_close_time": by_ticker[tickers[0]]["close_time"],
                "second_close_time": by_ticker[tickers[1]]["close_time"],
            })
        else:
            events.append({**base, "tickers": tickers})

    for p in pairs:
        print(f"  [pair]  {p['event_ticker']} vol={p['volume']:.0f} {p['title'][:48]}")
    for e in events:
        print(f"  [multi] {e['event_ticker']} vol={e['volume']:.0f} {e['title'][:48]} ({len(e['tickers'])} mkts)")
    return pairs, events

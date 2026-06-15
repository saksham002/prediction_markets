"""
Shared pair-discovery utilities for Kalshi two-market sports events.

A "pair" is an event with exactly two winner markets whose title parses as
"Team A vs Team B" or "Team A at Team B". The first-listed team determines
the positive direction for paired signals.
"""

import re

from src.utils.api import paginate


def _normalize_team_name(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())


def parse_first_team(title: str) -> str | None:
    # Finals-style titles carry a prefix: "Game 5: Vegas at Carolina"
    title = re.sub(r"^Game \d+:\s*", "", title)
    for sep in (" vs ", " at "):
        if sep in title:
            return title.split(sep, 1)[0].strip()
    return None


def build_pair_metadata(event_ticker: str, title: str, markets: list[dict]) -> dict | None:
    if len(markets) != 2:
        return None

    first_team = parse_first_team(title)
    if first_team is None:
        return None

    normalized_first = _normalize_team_name(first_team)
    first_market = None
    second_market = None

    for market in markets:
        team = market["yes_sub_title"]
        if _normalize_team_name(team) == normalized_first:
            first_market = market
        else:
            second_market = market

    if first_market is None or second_market is None:
        return None

    return {
        "event_ticker": event_ticker,
        "title": title,
        "first_team": first_team,
        "second_team": second_market["yes_sub_title"],
        "first_ticker": first_market["ticker"],
        "second_ticker": second_market["ticker"],
        "first_close_time": first_market["close_time"],
        "second_close_time": second_market["close_time"],
    }


def discover_top_pairs(n: int, category: str = "Sports", series: str | None = None) -> list[dict]:
    """series: optional comma-separated series tickers to keep (e.g. "KXMLBGAME,KXNBAGAME")."""
    series_set = None
    if series is not None:
        series_set = {s.strip() for s in series.split(",") if s.strip()}
    print("Fetching active events with nested markets...")
    events = paginate(
        "events",
        params = {"status": "open", "with_nested_markets": True},
        key = "events",
        max_per_page = 200,
    )
    print(f"  {len(events)} active events found")

    scored = []
    for event in events:
        # Some live events come back without a category field
        if event.get("category") != category:
            continue
        pair = build_pair_metadata(event["event_ticker"], event["title"], event["markets"])
        if pair is None:
            continue
        if series_set is not None and pair["first_ticker"].split("-", 1)[0] not in series_set:
            continue
        total_volume = sum(float(m["volume_24h_fp"]) for m in event["markets"])
        pair["volume"] = total_volume
        scored.append(pair)

    scored.sort(key = lambda p: p["volume"], reverse = True)
    top = scored[:n]

    for pair in top:
        print(
            f"  [{pair['event_ticker']}] vol={pair['volume']:.0f} "
            f"{pair['first_team']} vs {pair['second_team']}"
        )
    return top

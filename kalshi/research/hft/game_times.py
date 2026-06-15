"""
Game start-time estimation for liquidity/window gating.

Primary source: HHMM embedded in the event ticker (MLB/NFL style,
e.g. KXMLBGAME-26JUN111310STLNYM -> Jun 11 13:10 ET). Fallback for series
without embedded times (WC/NBA/NHL): the market's expected_expiration_time
minus a nominal league duration.
"""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.utils.api import api_get, BASE_URL

ET = timezone(timedelta(hours = -4))  # EDT (validation window is June)

# Nominal game durations used with expected_expiration_time fallback
LEAGUE_DURATION_H = {
    "KXMLBGAME": 3.2,
    "KXNBAGAME": 2.6,
    "KXNHLGAME": 3.0,
    "KXNFLGAME": 3.3,
    "KXWCGAME": 2.2,
    "KXINTLFRIENDLYGAME": 2.2,
}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

_cache: dict[str, float | None] = {}


def start_from_ticker(event_ticker: str) -> float | None:
    """Epoch seconds of game start if the ticker embeds YYMONDDHHMM (ET)."""
    m = re.match(r"[A-Z]+-(\d{2})([A-Z]{3})(\d{2})(\d{4})[A-Z]", event_ticker)
    if not m:
        return None
    yy, mon, dd, hhmm = m.groups()
    dt = datetime(2000 + int(yy), _MONTHS[mon], int(dd),
                  int(hhmm[:2]), int(hhmm[2:]), tzinfo = ET)
    return dt.timestamp()


def start_from_api(market_ticker: str) -> float | None:
    """expected_expiration_time minus nominal duration for the series."""
    series = market_ticker.split("-", 1)[0]
    duration_h = LEAGUE_DURATION_H.get(series)
    if duration_h is None:
        return None
    market = api_get(f"{BASE_URL}/markets/{market_ticker}").json()["market"]
    exp = market.get("expected_expiration_time")
    if not exp:
        return None
    dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
    return dt.timestamp() - duration_h * 3600


def game_start(event_ticker: str, any_market_ticker: str) -> float | None:
    """Cached game-start epoch estimate for an event."""
    if event_ticker in _cache:
        return _cache[event_ticker]
    start = start_from_ticker(event_ticker)
    if start is None:
        try:
            start = start_from_api(any_market_ticker)
        except Exception:
            start = None
    _cache[event_ticker] = start
    return start

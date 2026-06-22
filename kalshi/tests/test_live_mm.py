"""Explicit tradeable universe (load_universe): the live driver can be handed an
exact set of pairs/events from a JSON file instead of auto-discovering."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json

from research.hft.live_mm import load_universe


def test_load_universe_explicit(tmp_path):
    p = tmp_path / "universe.json"
    p.write_text(json.dumps({
        "pairs": [{"event_ticker": "KXNBAGAME-26JUN18LALBOS",
                   "first_ticker": "KXNBAGAME-26JUN18LALBOS-LAL",
                   "second_ticker": "KXNBAGAME-26JUN18LALBOS-BOS"}],
        "events": [{"event_ticker": "KXWCGAME-26JUN11KORCZE",
                    "tickers": ["KXWCGAME-26JUN11KORCZE-KOR",
                                "KXWCGAME-26JUN11KORCZE-CZE",
                                "KXWCGAME-26JUN11KORCZE-TIE"]}],
    }))
    pairs, events = load_universe(str(p))
    assert len(pairs) == 1 and len(events) == 1
    assert pairs[0]["first_ticker"] == "KXNBAGAME-26JUN18LALBOS-LAL"
    assert events[0]["series"] == "KXWCGAME"           # derived from the ticker prefix
    assert events[0]["tickers"] == ["KXWCGAME-26JUN11KORCZE-KOR",
                                    "KXWCGAME-26JUN11KORCZE-CZE",
                                    "KXWCGAME-26JUN11KORCZE-TIE"]


def test_load_universe_events_only(tmp_path):
    # a file with only events (no "pairs" key) -> empty pairs, events parsed + series derived
    p = tmp_path / "u2.json"
    p.write_text(json.dumps({"events": [{"event_ticker": "KXWCGAME-X",
                                         "tickers": ["KXWCGAME-X-A", "KXWCGAME-X-B", "KXWCGAME-X-TIE"]}]}))
    pairs, events = load_universe(str(p))
    assert pairs == []
    assert events[0]["series"] == "KXWCGAME"

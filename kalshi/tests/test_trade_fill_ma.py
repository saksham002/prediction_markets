import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.signals.trade_fill_ma import TradeFillMA


def make_trade(market_ticker: str, taker_side: str, count_fp: float, ts: int = 100) -> dict:
    return {
        "market_ticker": market_ticker,
        "taker_side": taker_side,
        "count_fp": str(count_fp),
        "ts": ts,
    }


def test_single_ticker_trade_fill_ma_signs():
    alpha = TradeFillMA("A-YES", half_life_fills = {"w1": 1})
    alpha.on_message("trade", make_trade("A-YES", "no", 5))
    alpha.on_message("trade", make_trade("A-YES", "yes", 2, ts = 101))

    assert alpha.value is not None
    assert alpha.value > 0


def test_pair_trade_fill_ma_aligns_first_team_flow():
    alpha = TradeFillMA(pair_tickers = ("A", "B"), half_life_fills = {"w1": 1})

    alpha.on_message("trade", make_trade("A", "no", 4))
    value_after_a_no = alpha.value
    alpha.on_message("trade", make_trade("B", "yes", 4, ts = 101))
    value_after_b_yes = alpha.value

    assert value_after_a_no is not None
    assert value_after_a_no > 0
    assert value_after_b_yes is not None
    assert value_after_b_yes > value_after_a_no


def test_pair_trade_fill_ma_mirrors_opposite_flow():
    alpha = TradeFillMA(pair_tickers = ("A", "B"), half_life_fills = {"w1": 1})

    alpha.on_message("trade", make_trade("A", "yes", 3))
    alpha.on_message("trade", make_trade("B", "no", 3, ts = 101))

    assert alpha.value is not None
    assert alpha.value < 0


def test_pair_trade_fill_ma_balances_mirrored_qty():
    alpha = TradeFillMA(pair_tickers = ("A", "B"), half_life_fills = {"w1000": 1000})

    alpha.on_message("trade", make_trade("A", "no", 5))
    alpha.on_message("trade", make_trade("A", "yes", 5, ts = 101))

    assert alpha.value is not None
    assert math.isclose(alpha.value, 0.0, abs_tol = 0.01)

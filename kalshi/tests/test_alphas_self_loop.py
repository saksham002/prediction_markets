"""Self-loop guard: OUR own resting orders / fills must not feed the alphas we
trade on. obi/mom computed on the book MINUS our registered resting qty; the
public print of our own fill (matched by trade_id) and our own orderbook_delta
(tagged with client_order_id) skip the trade/flow alphas. All no-ops with
nothing registered -> replay/sim path is unchanged."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collections import defaultdict

from src.utils.orderbook import MarketBook
from research.hft.alphas import SingleAlphaEngine, market_obi


def make_books():
    return defaultdict(MarketBook)


def trade_msg(ticker, taker_side, yes_price, qty, ts = 1000.0, trade_id = "t1"):
    return {
        "market_ticker": ticker,
        "taker_side": taker_side,
        "yes_price_dollars": f"{yes_price:.4f}",
        "no_price_dollars": f"{1.0 - yes_price:.4f}",
        "count_fp": f"{qty:.2f}",
        "ts": int(ts),
        "ts_ms": int(ts * 1000),
        "trade_id": trade_id,
    }


def test_register_resting_excludes_own_qty_from_obi():
    books = make_books()
    books["M"].yes.load_snapshot([["0.5000", "100.00"]])
    books["M"].no.load_snapshot([["0.5000", "100.00"]])
    eng = SingleAlphaEngine("M", books)
    assert abs(eng._obi()) < 1e-9                    # balanced book -> obi 0

    books["M"].yes.apply_delta("0.5000", 100.0)      # yes shows 200, no 100
    assert eng._obi() > 0.3                           # raw obi favors bid (incl. us)

    eng.register_resting("M", "yes", 0.50, 100.0)     # tell engine 100 is ours
    assert abs(eng._obi()) < 1e-9                      # obi recomputed minus our qty

    eng.register_resting("M", "yes", 0.50, 0.0)       # cancel -> clear our level
    assert eng._obi() > 0.3


def test_register_resting_excludes_own_from_mid():
    books = make_books()
    books["M"].yes.load_snapshot([["0.4000", "100.00"]])
    books["M"].no.load_snapshot([["0.4000", "100.00"]])
    eng = SingleAlphaEngine("M", books)
    base_mid = eng._mid()

    books["M"].yes.apply_delta("0.4500", 50.0)        # our improving bid moves raw mid
    assert eng._mid() > base_mid

    eng.register_resting("M", "yes", 0.45, 50.0)       # ours -> mid ignores it
    assert abs(eng._mid() - base_mid) < 1e-9


def test_own_trade_skipped_in_tfma():
    books = make_books()
    books["M"].yes.load_snapshot([["0.5000", "100.00"]])
    books["M"].no.load_snapshot([["0.5000", "100.00"]])
    eng = SingleAlphaEngine("M", books)

    eng.mark_own_fill("own1")
    eng.on_trade(1000.0, trade_msg("M", "yes", 0.50, 80.0, ts = 1000.0, trade_id = "own1"))
    assert eng.value_of("tfma_pw_30s", 1000.0) is None   # our fill ignored -> no data

    eng.on_trade(1001.0, trade_msg("M", "yes", 0.50, 80.0, ts = 1001.0, trade_id = "other"))
    v = eng.value_of("tfma_pw_30s", 1001.0)
    assert v is not None and abs(v) > 0                   # third-party trade moves tfma


def test_own_delta_skipped_in_aggflow():
    books = make_books()
    books["M"].yes.load_snapshot([["0.5000", "100.00"]])
    books["M"].no.load_snapshot([["0.5000", "100.00"]])
    eng = SingleAlphaEngine("M", books, track_agg = True)
    assert eng.value_of("agg_pw_30s", 1000.0) is None

    own = {"ts_ms": 1000000, "ts": 1000, "side": "yes", "price_dollars": "0.5000",
           "delta_fp": "50.00", "client_order_id": "c1"}
    assert eng._is_own_delta(own)
    eng.on_delta(1000.0, "M", own)
    assert eng.value_of("agg_pw_30s", 1000.0) is None    # our delta ignored -> no data

    other = {"ts_ms": 1001000, "ts": 1001, "side": "yes", "price_dollars": "0.5000",
             "delta_fp": "50.00"}
    assert not eng._is_own_delta(other)
    eng.on_delta(1001.0, "M", other)
    assert eng.value_of("agg_pw_30s", 1001.0) is not None  # third-party delta registers


def test_replay_unaffected_when_nothing_registered():
    books = make_books()
    books["M"].yes.load_snapshot([["0.5000", "200.00"]])
    books["M"].no.load_snapshot([["0.5000", "100.00"]])
    eng = SingleAlphaEngine("M", books)
    assert eng._obi() == market_obi(books["M"])          # identical to plain path

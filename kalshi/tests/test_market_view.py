"""MarketView: the single market-only book read surface (book MINUS our resting
orders) plus own-message identification. Reads always subtract the own-resting
ledger; with an empty ledger (analysis callers that never place) reads return the
RAW book — behaviour-neutral."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collections import defaultdict

from src.utils.orderbook import MarketBook
from research.hft.market_view import MarketView, market_obi


def make_books():
    b = defaultdict(MarketBook)
    b["M"].yes.load_snapshot([["0.5000", "100.00"]])
    b["M"].no.load_snapshot([["0.5000", "100.00"]])
    return b


def test_empty_ledger_equals_raw_live():
    books = make_books()
    view = MarketView(books)
    assert view.depth("M", "yes", "0.5000") == 100.0
    assert view.obi("M") == market_obi(books["M"])
    assert view.best_bid("M", "yes") == books["M"].yes.best_bid()


def test_depth_subtracts_own():
    books = make_books()
    view = MarketView(books)
    view.register("M", "yes", "0.5000", 60.0)
    assert abs(view.depth("M", "yes", "0.5000") - 40.0) < 1e-9
    view.release("M", "yes", "0.5000")
    assert view.depth("M", "yes", "0.5000") == 100.0


def test_live_obi_market_only():
    books = make_books()
    books["M"].yes.apply_delta("0.5000", 100.0)        # yes shows 200, of which 100 ours
    view = MarketView(books)
    assert view.obi("M") > 0.3                          # raw favors bid (incl. us)
    view.register("M", "yes", "0.5000", 100.0)
    assert abs(view.obi("M")) < 1e-9                     # ours removed -> balanced


def test_live_best_bid_skips_fully_own_level():
    books = make_books()
    books["M"].yes.apply_delta("0.5500", 30.0)         # our improving bid on top
    view = MarketView(books)
    assert view.best_bid("M", "yes")[0] == 0.55         # raw best
    view.register("M", "yes", "0.5500", 30.0)
    assert view.best_bid("M", "yes")[0] == 0.50         # ours removed -> next level


def test_apply_delta_always_applies():
    # D2: apply_delta ALWAYS applies (incl. our own) so book = market + ours at all
    # times; market-only reads subtract the LEDGER (not via is_own).
    books = make_books()
    view = MarketView(books)
    view.apply_delta("M", "yes", "0.5000", 50.0, is_own = True)    # applied to the book
    assert view.books["M"].yes.levels["0.5000"] == 150.0
    assert view.depth("M", "yes", "0.5000") == 150.0               # empty ledger -> raw
    view.register("M", "yes", "0.5000", 50.0)                      # now 50 is ours
    assert abs(view.depth("M", "yes", "0.5000") - 100.0) < 1e-9    # ledger subtracts


def test_market_levels_excludes_own_live():
    books = make_books()
    books["M"].yes.apply_delta("0.5500", 30.0)
    view = MarketView(books)
    view.register("M", "yes", "0.5500", 30.0)           # fully ours -> level drops out
    levels = view.market_levels("M", "yes")
    assert "0.5500" not in levels
    assert abs(levels["0.5000"] - 100.0) < 1e-9


def test_own_message_identification():
    books = make_books()
    view = MarketView(books)
    assert view.is_own_delta({"client_order_id": "c1", "side": "yes"})
    assert not view.is_own_delta({"side": "yes"})
    view.mark_own_fill("t1")
    assert view.is_own_trade({"trade_id": "t1"})
    assert not view.is_own_trade({"trade_id": "t2"})


def test_top_market_only():
    books = make_books()
    books["M"].yes.apply_delta("0.5500", 30.0)
    view = MarketView(books)
    view.register("M", "yes", "0.5500", 30.0)
    tob = view.top("M")
    assert tob.yes_bid == 0.50                           # our 0.55 bid excluded
    assert tob.yes_ask == round(1.0 - 0.50, 6)           # ask from no best bid 0.50

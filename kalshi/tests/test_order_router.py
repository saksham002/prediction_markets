"""OrderRouter in-flight state machine, driven by a minimal fake backend so the
router logic is tested in isolation (the integrated SimExchange path is covered by
test_exchange.py). State: IDLE -> PLACE_INFLIGHT -> RESTING -> CANCEL_INFLIGHT ->
IDLE; fills reduce RESTING. Confirmations (ack / public own-delta / private fill)
arrive via the handlers; the lock blocks a 2nd same-side action while in flight."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collections import defaultdict

import pytest

from src.utils.orderbook import MarketBook
from research.hft.market_view import MarketView
from research.hft.order_router import OrderRouter, IDLE, PLACE_INFLIGHT, RESTING, CANCEL_INFLIGHT
from research.hft.passive_fill import RestingOrder, price_key
from research.hft.exchange import own_delta_msg, ack_msg, private_fill_msg


class FakeBackend:
    """Schedules nothing — the test feeds confirmations to the router by hand."""
    def __init__(self):
        self.orders: dict[int, RestingOrder] = {}
        self._n = 0
        self.coid_by_feoid = {}

    def place(self, lts, ticker, side, price, qty):
        self._n += 1
        fe_oid, coid = self._n, f"C{self._n}"
        self.orders[fe_oid] = RestingOrder(order_id = fe_oid, ticker = ticker, side = side,
                                           price = price_key(price), qty = qty,
                                           placed_lts = lts, queue_ahead = 0.0)
        self.coid_by_feoid[fe_oid] = coid
        return fe_oid, coid

    def cancel(self, lts, coid):
        for oid, c in list(self.coid_by_feoid.items()):
            if c == coid:
                self.orders.pop(oid, None)

    def drain(self, lts):
        pass        # no auto-confirmation; test drives the handlers


def _setup():
    books = defaultdict(MarketBook)
    books["M"].yes.load_snapshot([["0.5000", "100.00"]])
    view = MarketView(books)
    be = FakeBackend()
    reduces = []
    router = OrderRouter(be, view,
                         on_reduce = lambda t, s, q, px, a, r: reduces.append((t, s, q, px, a, r)))
    return books, view, be, router, reduces


def test_place_then_ack_goes_resting():
    books, view, be, router, _ = _setup()
    assert router.can_place("M", "yes")
    coid = router.place(1.0, "M", "yes", 0.50, 10.0)
    assert router.state("M", "yes") == PLACE_INFLIGHT and not router.can_place("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))
    assert router.state("M", "yes") == RESTING and router.can_cancel("M", "yes")
    assert router.resting_order("M", "yes").order_id == 1


def test_public_delta_registers_then_releases_ledger():
    books, view, be, router, _ = _setup()
    coid = router.place(1.0, "M", "yes", 0.50, 10.0)
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))
    router.on_public_own_delta(own_delta_msg("M", "yes", 0.50, 10.0, coid, 1000))
    assert view.own_qty("M", "yes", "0.5000") == 10.0
    router.on_public_own_delta(own_delta_msg("M", "yes", 0.50, -10.0, coid, 1001))
    assert view.own_qty("M", "yes", "0.5000") == 0.0


def test_inflight_lock_raises_on_second_place():
    books, view, be, router, _ = _setup()
    router.place(1.0, "M", "yes", 0.50, 10.0)          # PLACE_INFLIGHT
    with pytest.raises(RuntimeError):
        router.place(1.0, "M", "yes", 0.49, 10.0)      # locked


def test_cancel_inflight_then_ack_idle():
    books, view, be, router, _ = _setup()
    coid = router.place(1.0, "M", "yes", 0.50, 10.0)
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))
    router.cancel(2.0, "M", "yes")
    assert router.state("M", "yes") == CANCEL_INFLIGHT and not router.can_place("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid, "O1", "cancel", 2000))
    assert router.state("M", "yes") == IDLE and router.can_place("M", "yes")


def test_full_fill_reduces_and_idles():
    books, view, be, router, reduces = _setup()
    coid = router.place(1.0, "M", "yes", 0.50, 10.0)
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))
    be.orders.pop(1)                                    # backend dropped the fully-filled order
    router.on_private_fill(private_fill_msg("M", "yes", 0.50, 10.0, coid, "O1", "T1",
                                            "buy", 0.0, 2000))
    assert reduces == [("M", "yes", 10.0, 0.50, "buy", "trade")]
    assert router.state("M", "yes") == IDLE


def test_private_fill_deduped_by_trade_id():
    books, view, be, router, reduces = _setup()
    coid = router.place(1.0, "M", "yes", 0.50, 10.0)
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))
    msg = private_fill_msg("M", "yes", 0.50, 5.0, coid, "O1", "T1", "buy", 5.0, 2000)
    router.on_private_fill(msg)
    router.on_private_fill(msg)                          # same trade_id -> ignored
    assert len(reduces) == 1

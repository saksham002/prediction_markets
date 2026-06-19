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

    def resting_for(self, coid):
        for oid, c in self.coid_by_feoid.items():
            if c == coid:
                return self.orders.get(oid)
        return None

    def drain(self, lts):
        pass        # no auto-confirmation; test drives the handlers


class FakeLimiter:
    """Controllable write-budget stub: flip allow_place / allow_cancel to drive the
    rate-limited-defer paths; counts successful grants."""
    def __init__(self):
        self.allow_place = True
        self.allow_cancel = True
        self.places = 0
        self.cancels = 0

    def try_place(self, now):
        if self.allow_place:
            self.places += 1
            return True
        return False

    def try_cancel(self, now):
        if self.allow_cancel:
            self.cancels += 1
            return True
        return False


def _setup():
    books = defaultdict(MarketBook)
    books["M"].yes.load_snapshot([["0.5000", "100.00"]])
    view = MarketView(books)
    be = FakeBackend()
    reduces = []
    router = OrderRouter(be, view,
                         on_reduce = lambda t, s, q, px, a, r, post: reduces.append((t, s, q, px, a, r)))
    return books, view, be, router, reduces


CTX = {"event": "E", "alpha": 0.0, "expo": 0.0}


def _setup_target(limiter = None, max_queue = None):
    """Book with several supported levels + a desired-state router driven via
    set_target (FakeBackend.drain is a no-op, so place/cancel stay in-flight until the
    test feeds the ack — the delays>0 / prod regime where the lock binds)."""
    books = defaultdict(MarketBook)
    books["M"].yes.load_snapshot([["0.5000", "100.00"], ["0.4900", "100.00"],
                                  ["0.4800", "100.00"]])
    view = MarketView(books)
    be = FakeBackend()
    logs = []
    router = OrderRouter(be, view, rate_limiter = limiter, max_queue = max_queue,
                         log = lambda *a: logs.append(a))
    return books, view, be, router, logs


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


# ---- desired-state interface (set_target) ----
def test_set_target_places_then_keeps_same_price():
    _, view, be, router, logs = _setup_target()
    router.set_target(1.0, "M", "yes", (0.50, 10.0), CTX)
    assert router.state("M", "yes") == PLACE_INFLIGHT   # FakeBackend: no inline ack
    coid = router.coid_for("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))
    assert router.state("M", "yes") == RESTING
    assert router.resting_order("M", "yes").price == "0.5000"
    router.set_target(2.0, "M", "yes", (0.50, 10.0), CTX)   # same price -> keep, no churn
    assert router.state("M", "yes") == RESTING
    assert [a[4] for a in logs] == ["place"]            # only the initial place logged


def test_cancel_replace_on_price_change_defers_to_ack():
    _, view, be, router, logs = _setup_target()
    router.set_target(1.0, "M", "yes", (0.50, 10.0), CTX)
    coid = router.coid_for("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))   # RESTING @0.50
    router.set_target(2.0, "M", "yes", (0.49, 10.0), CTX)           # price moved -> cancel
    assert router.state("M", "yes") == CANCEL_INFLIGHT             # place deferred
    router.on_ack(ack_msg("M", "yes", coid, "O1", "cancel", 2000))  # -> IDLE (state only)
    assert router.state("M", "yes") == IDLE
    router.reconcile(2.0, "M", "yes")                              # next event re-drives
    assert router.state("M", "yes") == PLACE_INFLIGHT
    coid2 = router.coid_for("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid2, "O2", "place", 2100))
    assert router.resting_order("M", "yes").price == "0.4900"


def test_rate_limited_cancel_defers_new_until_budget_frees():
    lim = FakeLimiter()
    _, view, be, router, logs = _setup_target(limiter = lim)
    router.set_target(1.0, "M", "yes", (0.50, 10.0), CTX)
    coid = router.coid_for("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))   # RESTING
    lim.allow_place = False                                         # only a cancel fits
    router.set_target(2.0, "M", "yes", (0.49, 10.0), CTX)
    assert router.state("M", "yes") == CANCEL_INFLIGHT and lim.cancels == 1
    router.on_ack(ack_msg("M", "yes", coid, "O1", "cancel", 2000))  # IDLE
    router.reconcile(2.0, "M", "yes")                              # still rate-limited
    assert router.state("M", "yes") == IDLE and lim.places == 1    # NEW still deferred
    lim.allow_place = True                                          # budget frees
    router.reconcile(3.0, "M", "yes")
    assert router.state("M", "yes") == PLACE_INFLIGHT
    coid2 = router.coid_for("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid2, "O2", "place", 3000))
    assert router.resting_order("M", "yes").price == "0.4900"      # the new price went out


def test_price_change_while_cancel_inflight_uses_latest():
    _, view, be, router, logs = _setup_target()
    router.set_target(1.0, "M", "yes", (0.50, 10.0), CTX)
    coid = router.coid_for("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid, "O1", "place", 1000))   # RESTING @0.50
    router.set_target(2.0, "M", "yes", (0.49, 10.0), CTX)           # cancel -> CANCEL_INFLIGHT
    assert router.state("M", "yes") == CANCEL_INFLIGHT
    router.set_target(2.5, "M", "yes", (0.48, 10.0), CTX)           # changed mind mid-flight
    assert router.state("M", "yes") == CANCEL_INFLIGHT             # no 2nd action sent
    router.on_ack(ack_msg("M", "yes", coid, "O1", "cancel", 2600))
    router.reconcile(2.6, "M", "yes")
    coid2 = router.coid_for("M", "yes")
    router.on_ack(ack_msg("M", "yes", coid2, "O2", "place", 2700))
    assert router.resting_order("M", "yes").price == "0.4800"      # latest, not 0.49


def test_reject_frees_side_then_reconcile_all_replaces():
    _, view, be, router, logs = _setup_target()
    router.set_target(1.0, "M", "yes", (0.50, 10.0), CTX)
    coid = router.coid_for("M", "yes")
    assert router.state("M", "yes") == PLACE_INFLIGHT
    router.on_reject(coid, "place")                               # 4xx/429 -> freed, state only
    assert router.state("M", "yes") == IDLE
    assert router.resting_order("M", "yes") is None
    router.reconcile_all(2.0)                                     # safety-net re-places target
    assert router.state("M", "yes") == PLACE_INFLIGHT

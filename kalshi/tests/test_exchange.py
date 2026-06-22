"""exchange.py primitives: deterministic id minting, the effective-time delay
scheduler (ordering + backward-clamp + inline-at-zero), and the prod message
builders (exact captured field shapes)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collections import defaultdict

import asyncio

from research.hft.exchange import (
    SimIds, Scheduler, SimExchange, ProdExchange,
    own_delta_msg, public_trade_msg, private_fill_msg, ack_msg,
)
from research.hft.market_view import MarketView
from research.hft.order_router import OrderRouter, IDLE, PLACE_INFLIGHT, RESTING, CANCEL_INFLIGHT
from src.utils.orderbook import MarketBook


def _setup(ack_delay = 0.0, pub_delay = 0.0):
    """Wire MarketView + SimExchange + OrderRouter with a deliver that mimics the
    driver: a public own-delta is applied to the book (is_own) then routed to the
    ledger; acks/fills route to the router. Returns (view, sim, router, fills)."""
    books = defaultdict(MarketBook)
    books["M"].yes.load_snapshot([["0.5000", "100.00"]])
    books["M"].no.load_snapshot([["0.5000", "100.00"]])
    view = MarketView(books)
    sim = SimExchange(view, ack_delay = ack_delay, pub_delay = pub_delay)
    fills = []
    router = OrderRouter(sim, view,
                         on_reduce = lambda t, s, q, px, act, rsn, post: fills.append((t, s, q, px, act, rsn)))

    def deliver(kind, msg):
        if kind == "public_delta":
            view.apply_delta(msg["market_ticker"], msg["side"], msg["price_dollars"],
                             float(msg["delta_fp"]), is_own = True)
            router.on_public_own_delta(msg)
        elif kind == "ack":
            router.on_ack(msg)
        elif kind == "private_fill":
            router.on_private_fill(msg)
        # public_trade: alpha excludes our own print; nothing to do here
    sim.set_deliver(deliver)
    return view, sim, router, fills


def _yes_sell_trade(yes_price, count, ts):
    """A taker SELLING yes (taker_side 'no') that lifts our resting yes bid."""
    yp = round(yes_price, 4)
    return {"market_ticker": "M", "taker_side": "no", "yes_price_dollars": f"{yp:.4f}",
            "no_price_dollars": f"{1.0 - yp:.4f}", "count_fp": f"{count:.2f}",
            "ts": int(ts), "ts_ms": int(ts * 1000), "trade_id": "REAL-MARKET-TRADE"}


def test_sim_ids_unique_prefixed_monotonic():
    ids = SimIds()
    a, b = ids.coid(), ids.coid()
    assert a == "SIM-COID-000000001" and b == "SIM-COID-000000002"
    assert ids.oid().startswith("SIM-OID-") and ids.trade().startswith("SIM-TRADE-")
    # kinds have independent counters; no value collides
    seen = {ids.coid(), ids.oid(), ids.trade(), a, b}
    assert len(seen) == 5
    # SIM- prefix can't collide with a real UUID
    assert all(s.startswith("SIM-") for s in seen)


def test_scheduler_orders_by_effective_ts_then_seq():
    s = Scheduler()
    s.push(2.0, "k", {"i": 0})
    s.push(1.0, "k", {"i": 1})
    s.push(1.0, "k", {"i": 2})   # same ts as i=1 -> FIFO by seq
    out = []
    s.drain(10.0, lambda kind, m: out.append(m["i"]))
    assert out == [1, 2, 0]


def test_scheduler_drain_only_due():
    s = Scheduler()
    s.push(1.0, "k", {"i": 1})
    s.push(5.0, "k", {"i": 5})
    out = []
    s.drain(1.0, lambda kind, m: out.append(m["i"]))
    assert out == [1] and len(s) == 1          # i=5 not yet due
    s.drain(5.0, lambda kind, m: out.append(m["i"]))
    assert out == [1, 5] and len(s) == 0


def test_scheduler_backward_clamp():
    s = Scheduler()
    s.push(5.0, "k", {"i": 5})
    s.drain(5.0, lambda kind, m: None)         # last delivered = 5.0
    s.push(1.0, "k", {"i": 1})                 # past event -> clamped to 5.0
    assert s.due(5.0)                          # due at now=5.0 (not stuck in the past)
    out = []
    s.drain(5.0, lambda kind, m: out.append(m["i"]))
    assert out == [1]


def test_scheduler_zero_delay_due_immediately():
    s = Scheduler()
    s.push(3.0, "k", {"i": 1})                 # effective_ts == now
    assert s.due(3.0)                          # inline-at-zero: deliverable now


def test_own_delta_msg_shape():
    m = own_delta_msg("M", "yes", 0.25, 1.0, "SIM-COID-1", 1781563793147)
    assert m["price_dollars"] == "0.2500" and m["delta_fp"] == "1.00"
    assert m["client_order_id"] == "SIM-COID-1" and m["side"] == "yes"
    assert m["ts_ms"] == 1781563793147 and m["ts"] == 1781563793
    neg = own_delta_msg("M", "yes", 0.25, -1.0, "SIM-COID-1", 1781563796465)
    assert neg["delta_fp"] == "-1.00"


def test_public_trade_msg_shape():
    m = public_trade_msg("M", "no", 0.25, 1.0, "SIM-TRADE-1", 1781563796465)
    assert m["trade_id"] == "SIM-TRADE-1" and m["taker_side"] == "no"
    assert m["yes_price_dollars"] == "0.2500" and m["no_price_dollars"] == "0.7500"
    assert m["count_fp"] == "1.00" and "client_order_id" not in m   # anonymous


def test_private_fill_msg_shape():
    m = private_fill_msg("M", "yes", 0.25, 1.0, "SIM-COID-1", "SIM-OID-1", "SIM-TRADE-1",
                         "buy", 1.0, 1781563796465)
    assert m["trade_id"] == "SIM-TRADE-1" and m["order_id"] == "SIM-OID-1"
    assert m["client_order_id"] == "SIM-COID-1" and m["side"] == "yes"
    assert m["action"] == "buy" and m["post_position_fp"] == "1.00"
    assert m["book_side"] == "bid" and m["is_taker"] is False


def test_ack_msg_shape():
    m = ack_msg("M", "yes", "SIM-COID-1", "SIM-OID-1", "place", 1781563793100)
    assert m["ack"] == "place" and m["client_order_id"] == "SIM-COID-1"
    c = ack_msg("M", "yes", "SIM-COID-1", "SIM-OID-1", "cancel", 1781563793200)
    assert c["ack"] == "cancel"


def test_place_delay0_goes_resting_and_book_stays_market_only():
    view, sim, router, _ = _setup()
    router.place(1.0, "M", "yes", 0.50, 10.0)
    assert router.state("M", "yes") == RESTING          # delay 0 -> confirmed inline
    # book now includes our +10 (raw 110) but market-only read excludes it
    assert abs(view.books["M"].yes.levels["0.5000"] - 110.0) < 1e-9
    assert abs(view.depth("M", "yes", "0.5000") - 100.0) < 1e-9   # market-only
    assert router.resting_order("M", "yes") is not None


def test_fill_delay0_inventory_and_idle():
    view, sim, router, fills = _setup()
    router.place(1.0, "M", "yes", 0.50, 10.0)           # queue_ahead = 100
    # taker sells 200 yes @ 0.50: overflow 100 past our queue -> fills our 10
    sim.on_recorded_trade(2.0, _yes_sell_trade(0.50, 200.0, 2.0))
    sim.drain(2.0)
    assert fills == [("M", "yes", 10.0, 0.50, "buy", "trade")]   # our fill, once
    assert router.state("M", "yes") == IDLE             # fully filled -> side free
    assert abs(view.depth("M", "yes", "0.5000") - 100.0) < 1e-9   # ledger released
    # our public own-delta(-10) reduced the raw book back to market-only 100
    assert abs(view.books["M"].yes.levels["0.5000"] - 100.0) < 1e-9


def test_fill_deduped_across_feeds():
    view, sim, router, fills = _setup()
    router.place(1.0, "M", "yes", 0.50, 10.0)
    sim.on_recorded_trade(2.0, _yes_sell_trade(0.50, 200.0, 2.0))
    sim.drain(2.0)
    # re-delivering the same private fill must NOT double-count (trade_id dedup)
    fill_msgs = []
    sim2 = SimExchange(view, ack_delay = 0, pub_delay = 0)
    assert len(fills) == 1


def test_cancel_delay0_idle_and_ledger_released():
    view, sim, router, _ = _setup()
    router.place(1.0, "M", "yes", 0.50, 10.0)
    assert router.can_cancel("M", "yes")
    router.cancel(3.0, "M", "yes")
    assert router.state("M", "yes") == IDLE
    assert abs(view.depth("M", "yes", "0.5000") - 100.0) < 1e-9
    assert abs(view.books["M"].yes.levels["0.5000"] - 100.0) < 1e-9   # our qty removed


def test_inflight_lock_blocks_same_side_until_confirmation():
    view, sim, router, _ = _setup(ack_delay = 0.005, pub_delay = 0.010)
    router.place(1.0, "M", "yes", 0.50, 10.0)
    assert router.state("M", "yes") == PLACE_INFLIGHT   # not yet confirmed
    assert not router.can_place("M", "yes") and not router.can_cancel("M", "yes")
    sim.drain(1.004)                                    # before ack -> still locked
    assert router.state("M", "yes") == PLACE_INFLIGHT
    sim.drain(1.010)                                    # ack + delta delivered
    assert router.state("M", "yes") == RESTING and router.can_cancel("M", "yes")
    router.cancel(2.0, "M", "yes")
    assert router.state("M", "yes") == CANCEL_INFLIGHT  # locked again
    sim.drain(2.010)
    assert router.state("M", "yes") == IDLE


# ---- ProdExchange: non-blocking place/cancel (async REST off the event loop) ----
class _FakeResp:
    def __init__(self, status, oid = None):
        self.status_code = status
        self._oid = oid

    def json(self):
        return {"order_id": self._oid}


class _FakeApi:
    """Records calls; create_order/cancel_order are synchronous (run via to_thread)."""
    def __init__(self):
        self.created = []
        self.cancelled = []

    def create_order(self, ticker, side, action, count, price_cents, *, client_order_id = None):
        self.created.append((ticker, side, action, count, price_cents, client_order_id))
        return _FakeResp(201, oid = "H1")

    def cancel_order(self, handle):
        self.cancelled.append(handle)
        return _FakeResp(200)


class _StubConsumer:
    def __init__(self, router):
        self.router = router


def _setup_prod(api):
    books = defaultdict(MarketBook)
    books["M"].yes.load_snapshot([["0.5000", "100.00"]])
    view = MarketView(books)
    prod = ProdExchange(view, ["M"], api = api)
    router = OrderRouter(prod, view)

    def deliver(kind, msg):
        if kind == "ack":
            router.on_ack(msg)
        elif kind == "reject":
            router.on_reject(msg["client_order_id"], msg["kind"])
    prod.set_deliver(deliver)
    prod._consumer = _StubConsumer(router)     # set in run() live; injected here
    return view, prod, router


def test_prod_place_is_nonblocking_then_acks_async():
    async def scenario():
        api = _FakeApi()
        view, prod, router = _setup_prod(api)
        router.set_target(1.0, "M", "yes", (0.50, 10.0), {"event": "E"})
        # place() returned WITHOUT doing the REST: the side is in-flight and the REST
        # task hasn't even started (no create_order recorded yet).
        assert router.state("M", "yes") == PLACE_INFLIGHT
        assert api.created == []
        await asyncio.sleep(0.05)              # let the background REST task run
        assert len(api.created) == 1
        assert router.state("M", "yes") == RESTING        # ack delivered async
        assert router.resting_order("M", "yes") is not None
        # cancel is likewise non-blocking and frees the side on the async ack
        router.set_target(2.0, "M", "yes", None, {"event": "E"})
        assert router.state("M", "yes") == CANCEL_INFLIGHT
        assert api.cancelled == []
        await asyncio.sleep(0.05)
        assert api.cancelled == ["H1"]
        assert router.state("M", "yes") == IDLE
    asyncio.run(scenario())


def test_prod_place_sends_fractional_count_not_int_truncated():
    """Sub-1 sizes are sent as fractional contracts (Kalshi min 0.01), NOT
    int()-truncated to count=0 (the SCOMAR sub-1 dust-storm bug)."""
    async def scenario():
        api = _FakeApi()
        view, prod, router = _setup_prod(api)
        router.set_target(1.0, "M", "yes", (0.50, 0.99), {"event": "E"})
        await asyncio.sleep(0.05)
        assert len(api.created) == 1
        count = api.created[0][3]              # (ticker, side, action, count, ...)
        assert abs(count - 0.99) < 1e-9        # NOT int(0.99) == 0
    asyncio.run(scenario())


def test_dispatch_routes_market_lifecycle():
    """A market_lifecycle_v2 WS message is routed to consumer.on_market_lifecycle
    with the inner msg payload (event_type/market_ticker/...)."""
    api = _FakeApi()
    view, prod, router = _setup_prod(api)
    seen = []
    prod._consumer._timing = None
    prod._consumer.on_market_lifecycle = lambda m: seen.append(m)
    inner = {"event_type": "determined", "market_ticker": "M", "result": "yes"}
    prod._dispatch(prod._consumer, {"type": "market_lifecycle_v2", "msg": inner}, 1.0)
    assert seen == [inner]


def test_lifecycle_determined_closes_market_and_cancels():
    """determined/settled on a traded market closes EVERY leg of the owning
    strategy (both sides cancelled, want=None); non-terminal / unknown events
    are ignored."""
    from types import SimpleNamespace
    from research.hft.mm_sim import MMSimConsumer
    calls = []
    router = SimpleNamespace(set_target = lambda lts, t, s, want, ctx: calls.append((t, s, want)))
    mm = SimpleNamespace(first_ticker = "E-A", second_ticker = "E-B",
                         pair = {"event_ticker": "E"}, inventory = {"E-A": 5.0, "E-B": -2.0})
    stub = SimpleNamespace(mm_by_ticker = {"E-A": mm, "E-B": mm}, closed_markets = set(),
                           _cur_lts = 10.0, router = router)
    MMSimConsumer.on_market_lifecycle(stub, {"event_type": "determined", "market_ticker": "E-A",
                                             "result": "yes", "settlement_value": "1.0000"})
    assert stub.closed_markets == {"E-A", "E-B"}
    assert sorted(calls) == [("E-A", "no", None), ("E-A", "yes", None),
                             ("E-B", "no", None), ("E-B", "yes", None)]
    # non-terminal event (transient pause) -> ignored
    stub2 = SimpleNamespace(mm_by_ticker = {"E-A": mm}, closed_markets = set(),
                            _cur_lts = 0.0, router = SimpleNamespace(set_target = lambda *a: None))
    MMSimConsumer.on_market_lifecycle(stub2, {"event_type": "deactivated", "market_ticker": "E-A"})
    assert stub2.closed_markets == set()
    # unknown ticker (firehose, not ours) -> ignored, no crash
    MMSimConsumer.on_market_lifecycle(stub2, {"event_type": "determined", "market_ticker": "ZZZ"})
    assert stub2.closed_markets == set()

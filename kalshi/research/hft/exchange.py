"""
Exchange abstraction so the SIM emits the SAME public+private feed messages for
OUR OWN orders that PROD emits — the consumer then processes its own orders
identically in sim and prod (the book + the message stream already contain them),
which is what lets the self-feed exclusion (market-only reads, alpha skip) be
exercised and validated in sim.

Two implementations:
  - SimExchange: owns the recorded-book matching (PassiveFillEngine) and a delay
    scheduler. For each place/cancel/fill it mints synthetic ids and SCHEDULES the
    exact prod message shapes; the driver drains them in effective-time order into
    the consumer. View/ledger + resting state update ONLY on confirmation delivery
    (never optimistically at send).
  - ProdExchange: a thin pass-through — real websocket messages are forwarded
    unchanged; place/cancel hit the real REST API (executor is out of scope here).

Message shapes are taken from a live capture (scratch/fill_messages.json):
  PLACE  -> public orderbook_delta (delta_fp > 0, tagged client_order_id)
  FILL   -> public orderbook_delta (delta_fp < 0, tagged client_order_id)
          + public anonymous trade (trade_id; NO client_order_id)
          + private fill (trade_id + order_id + client_order_id + post_position_fp ...)
  CANCEL -> public orderbook_delta (delta_fp < 0, tagged client_order_id)
all stamped at the same exchange ts_ms. Match key public-trade<->private-fill = trade_id.

Delays (D4): constant, configurable. `ack_delay` / `pub_delay` are the TOTAL
send->confirmation times for the private (REST ack / fill) and public (orderbook
reflection) legs; at 0 confirmations deliver synchronously (in-flight lock
non-binding) so the path collapses to the current synchronous sim (equivalence
test). The FILL gate stays the fill engine's FORWARD_DELAY_S (unchanged) so fills
are identical at delays=0. PROD delays are real/variable and NOT modeled here.
"""

import heapq

from research.hft.passive_fill import price_key
from src.utils.feps import is_pos

# AWS us-east-1 measured feed/order timing (see project_status/experiments.md):
#   ack_delay  = place->REST-ack RTT ~21.6ms
#   pub_delay  = ack + PRIV->PUB lag (6.4ms) ~28ms
#   fill_delay = WS one-way (exchange match -> we learn of the fill) ~16ms
#   fill_pub_lag = priv-fill -> public(trade/delta), sub-ms (both WS, same ts_ms)
# Default delays are 0 (synchronous = the equivalence baseline; the in-flight lock
# is non-binding). Pass REALISTIC_DELAYS for the prod-faithful model (the lock binds,
# cancel-replace costs a round-trip) — this materially lowers captured PnL.
REALISTIC_DELAYS = {"ack_delay": 0.022, "pub_delay": 0.028,
                    "fill_delay": 0.016, "fill_pub_lag": 0.0}


# ---- synthetic ids (deterministic, reproducible; can't collide with real UUIDs
#      or recorded-trade trade_ids because of the SIM- prefix) ----
class SimIds:
    def __init__(self):
        self._n = {"coid": 0, "oid": 0, "trade": 0}

    def _next(self, kind: str) -> str:
        self._n[kind] += 1
        return f"SIM-{kind.upper()}-{self._n[kind]:09d}"

    def coid(self) -> str:
        return self._next("coid")

    def oid(self) -> str:
        return self._next("oid")

    def trade(self) -> str:
        return self._next("trade")


# ---- effective-time delivery scheduler ----
class Scheduler:
    """Min-heap of (effective_ts, seq, kind, msg). `drain(now, deliver)` pops
    everything due (effective_ts <= now) in time order, clamping effective_ts up
    to the last delivered time so the consumer clock never goes backward."""

    def __init__(self):
        self._h: list = []
        self._seq = 0
        self._last = float("-inf")

    def push(self, effective_ts: float, kind: str, msg: dict):
        # never schedule into the already-delivered past
        eff = max(effective_ts, self._last)
        heapq.heappush(self._h, (eff, self._seq, kind, msg))
        self._seq += 1

    def due(self, now: float) -> bool:
        return bool(self._h) and self._h[0][0] <= now

    def drain(self, now: float, deliver):
        """Deliver all events with effective_ts <= now, in (ts, seq) order."""
        while self._h and self._h[0][0] <= now:
            eff, _seq, kind, msg = heapq.heappop(self._h)
            self._last = eff
            deliver(kind, msg)

    def drain_all(self, deliver):
        """Flush every remaining event (end-of-replay)."""
        while self._h:
            eff, _seq, kind, msg = heapq.heappop(self._h)
            self._last = eff
            deliver(kind, msg)

    def __len__(self):
        return len(self._h)


# ---- prod message builders (exact captured shapes) ----
def _ts_pair(ts_ms: int) -> dict:
    return {"ts": int(ts_ms // 1000), "ts_ms": int(ts_ms)}


def own_delta_msg(ticker: str, side: str, price: float, delta_fp: float,
                  client_order_id: str, ts_ms: int) -> dict:
    """Public orderbook_delta tagged with our client_order_id (delta_fp>0 place,
    <0 fill/cancel)."""
    return {"market_ticker": ticker, "side": side, "price_dollars": price_key(price),
            "delta_fp": f"{delta_fp:.2f}", "client_order_id": client_order_id,
            **_ts_pair(ts_ms)}


def public_trade_msg(ticker: str, taker_side: str, yes_price: float, count: float,
                     trade_id: str, ts_ms: int) -> dict:
    """Public anonymous trade print (matchable to our private fill only by trade_id)."""
    yp = round(float(yes_price), 4)
    return {"trade_id": trade_id, "market_ticker": ticker,
            "yes_price_dollars": f"{yp:.4f}", "no_price_dollars": f"{1.0 - yp:.4f}",
            "count_fp": f"{count:.2f}", "taker_side": taker_side,
            "taker_outcome_side": taker_side,
            "taker_book_side": "bid" if taker_side == "yes" else "ask",
            **_ts_pair(ts_ms)}


def private_fill_msg(ticker: str, side: str, yes_price: float, count: float,
                     client_order_id: str, order_id: str, trade_id: str,
                     action: str, post_position_fp: float, ts_ms: int,
                     is_taker: bool = False, reason: str = "trade") -> dict:
    """Private fill-channel message (our side; authoritative for inventory/PnL).
    `reason` carries the originating sim Fill.reason ('trade'/'cross') for logging;
    it has no prod analog (real fills don't say why)."""
    yp = round(float(yes_price), 4)
    return {"trade_id": trade_id, "order_id": order_id, "client_order_id": client_order_id,
            "market_ticker": ticker, "is_taker": is_taker, "side": side,
            "yes_price_dollars": f"{yp:.4f}", "count_fp": f"{count:.2f}",
            "action": action, "post_position_fp": f"{post_position_fp:.2f}",
            "book_side": "bid" if side == "yes" else "ask", "reason": reason,
            **_ts_pair(ts_ms)}


def ack_msg(ticker: str, side: str, client_order_id: str, order_id: str,
            kind: str, ts_ms: int) -> dict:
    """Private REST ack for a place/cancel (releases the in-flight lock). Not a
    real WS channel — models the synchronous REST response (kind: 'place'/'cancel')."""
    return {"market_ticker": ticker, "side": side, "client_order_id": client_order_id,
            "order_id": order_id, "ack": kind, **_ts_pair(ts_ms)}


class SimExchange:
    """Drives the recorded-book matching (PassiveFillEngine) and emits OUR own
    order lifecycle as the exact prod message stream, delivered to the consumer in
    effective-time order via `on_deliver(kind, msg)`.

    Delays (all constant, configurable; 0 for the equivalence test):
      ack_delay   send -> private REST ack (place/cancel) — releases the in-flight lock
      pub_delay   send -> public orderbook_delta (place/cancel) — book + ledger update
      fill_pub_lag  private fill -> public(trade/delta) for a FILL (~0; both WS, same ts_ms)
    The FILL gate stays the fill engine's forward_delay (unchanged). At delays=0
    confirmations deliver inline (in place/cancel) so the lock is non-binding and
    the path collapses to the synchronous current sim.

    Backend interface for OrderRouter: place()/cancel()/orders. The router receives
    the scheduled confirmations back via its on_ack/on_public_own_delta/on_private_fill
    (wired through the consumer's deliver)."""

    simulates = True       # consumer re-injects our resting qty into recorded snapshots

    def __init__(self, view, *, fwd_delay=None, ack_delay=0.0, pub_delay=0.0,
                 fill_delay=0.0, fill_pub_lag=0.0, on_deliver=None):
        from research.hft.passive_fill import PassiveFillEngine, FORWARD_DELAY_S
        fwd = FORWARD_DELAY_S if fwd_delay is None else fwd_delay
        self.view = view
        # market-only matching: the fill engine reads via the view (own qty excluded)
        self.fill_engine = PassiveFillEngine(view.books, forward_delay = fwd, view = view)
        self.ack_delay = ack_delay         # send -> private ack (place/cancel)
        self.pub_delay = pub_delay         # send -> public delta (place/cancel)
        self.fill_delay = fill_delay       # exchange match -> private fill arrival (WS one-way)
        self.fill_pub_lag = fill_pub_lag   # private fill -> public(trade/delta) for a fill (~0)
        self.ids = SimIds()
        self.sched = Scheduler()
        self.on_deliver = on_deliver
        self._now = 0.0
        # fill-engine int oid <-> our synthetic coid/oid
        self._coid_by_feoid: dict[int, str] = {}
        self._feoid_by_coid: dict[str, int] = {}
        self._oid_str: dict[str, str] = {}

    @property
    def orders(self):
        return self.fill_engine.orders

    def set_deliver(self, cb):
        self.on_deliver = cb

    def _ms(self, lts: float) -> int:
        return int(round(lts * 1000))

    def _schedule(self, ts: float, kind: str, msg: dict):
        self.sched.push(ts, kind, msg)

    def drain(self, now: float):
        """Deliver all own messages with effective_ts <= now."""
        self._now = max(self._now, now)
        if self.on_deliver is not None:
            self.sched.drain(now, self.on_deliver)

    def drain_all(self):
        if self.on_deliver is not None:
            self.sched.drain_all(self.on_deliver)

    # ---- order entry (OrderRouter backend) ----
    def place(self, decide_lts: float, ticker: str, side: str, price: float, qty: float):
        self._now = max(self._now, decide_lts)
        fe_oid = self.fill_engine.place(decide_lts, ticker, side, price, qty)
        coid, oid = self.ids.coid(), self.ids.oid()
        self._coid_by_feoid[fe_oid] = coid
        self._feoid_by_coid[coid] = fe_oid
        self._oid_str[coid] = oid
        ts_ms = self._ms(decide_lts)
        self._schedule(decide_lts + self.ack_delay, "ack",
                       ack_msg(ticker, side, coid, oid, "place", ts_ms))
        self._schedule(decide_lts + self.pub_delay, "public_delta",
                       own_delta_msg(ticker, side, price, qty, coid, ts_ms))
        # NB: caller (OrderRouter) drains AFTER recording its state, so the inline
        # delay-0 confirmation finds the order registered.
        return fe_oid, coid

    def cancel(self, decide_lts: float, coid: str):
        self._now = max(self._now, decide_lts)
        fe_oid = self._feoid_by_coid.get(coid)
        order = self.fill_engine.orders.get(fe_oid) if fe_oid is not None else None
        if order is None:
            return                      # already gone (filled); router shouldn't call this
        ticker, side, price, rem = order.ticker, order.side, order.price_f, order.remaining
        oid = self._oid_str[coid]
        # the order stops matching once the cancel reaches the exchange; at delays=0
        # that's immediate (matches the current sim's synchronous cancel)
        self.fill_engine.cancel(fe_oid)
        ts_ms = self._ms(decide_lts)
        self._schedule(decide_lts + self.ack_delay, "ack",
                       ack_msg(ticker, side, coid, oid, "cancel", ts_ms))
        self._schedule(decide_lts + self.pub_delay, "public_delta",
                       own_delta_msg(ticker, side, price, -rem, coid, ts_ms))

    # ---- recorded market messages (driver feeds these) ----
    def _emit_fills(self, fills):
        for fill in fills:
            order = fill.order
            coid = self._coid_by_feoid.get(order.order_id)
            oid = self._oid_str.get(coid, "SIM-OID-UNKNOWN")
            trade_id = self.ids.trade()
            self.view.mark_own_fill(trade_id)            # exclude our public print from flow
            ticker, side = order.ticker, order.side
            yes_price = order.price_f if side == "yes" else round(1.0 - order.price_f, 6)
            taker_side = "no" if side == "yes" else "yes"   # taker is opposite our maker side
            action = "buy" if side == "yes" else "sell"
            ts_ms = self._ms(fill.lts)
            # we learn of the fill fill_delay after the exchange match (WS one-way);
            # private fill is authoritative (inventory/PnL + resting reduction), the
            # public legs (book reduction + anonymous own trade, size q, excluded)
            # arrive ~together (fill_pub_lag ~ 0).
            priv_ts = fill.lts + self.fill_delay
            pub_ts = priv_ts + self.fill_pub_lag
            self._schedule(pub_ts, "public_delta",
                           own_delta_msg(ticker, side, order.price_f, -fill.qty, coid, ts_ms))
            self._schedule(pub_ts, "public_trade",
                           public_trade_msg(ticker, taker_side, yes_price, fill.qty, trade_id, ts_ms))
            self._schedule(priv_ts, "private_fill",
                           private_fill_msg(ticker, side, yes_price, fill.qty, coid, oid,
                                            trade_id, action, 0.0, ts_ms, reason = fill.reason))

    def on_recorded_snapshot(self, lts, ticker):
        # the recorded (market-only) snapshot is already loaded into the book by the
        # driver, and our resting qty re-injected; re-cap the queue model.
        self._now = max(self._now, lts)
        fills = self.fill_engine.on_snapshot(lts, ticker)
        self._emit_fills(fills)

    def on_recorded_delta(self, lts, ticker, side, price, delta):
        self._now = max(self._now, lts)
        if delta < 0:
            self.fill_engine.record_delta(lts, ticker, side, price, delta)
        fills = self.fill_engine.on_book(lts, ticker)
        self._emit_fills(fills)

    def on_recorded_trade(self, lts, msg):
        self._now = max(self._now, lts)
        fills = self.fill_engine.on_trade(lts, msg)
        self._emit_fills(fills)

    def own_levels(self, ticker: str) -> dict:
        """Our current resting qty per (side, price_key) — for snapshot re-injection
        (the consumer's snapshot must include our orders, like a prod aggregated snapshot)."""
        out = {"yes": {}, "no": {}}
        for o in self.fill_engine.orders.values():
            if o.ticker == ticker and is_pos(o.remaining):
                out[o.side][o.price] = out[o.side].get(o.price, 0.0) + o.remaining
        return out


class _ProdRestingOrder:
    """Minimal resting-order view for the strategy's _reconcile_side reads
    (price_f / remaining / queue_ahead). queue_ahead is 0 in prod — we do NOT
    model the queue (per the design: when we fill, the private feed tells us)."""
    __slots__ = ("ticker", "side", "price_f", "remaining", "queue_ahead", "coid")

    def __init__(self, ticker, side, price_f, remaining, coid):
        self.ticker = ticker
        self.side = side
        self.price_f = price_f
        self.remaining = remaining
        self.queue_ahead = 0.0
        self.coid = coid

    @property
    def price(self):
        return price_key(self.price_f)


class ProdExchange:
    """LIVE backend — SAME API as SimExchange so the consumer/router/strategy are
    identical in sim and prod. The only differences are internal: the data source
    is the live websocket (public orderbook_delta+trade, private fill) instead of a
    replay, and order entry hits the real REST API instead of being simulated.

    Crucially prod does NOT simulate fills (no PassiveFillEngine / queue model):
    fills are learned from the authoritative private `fill` channel. The recorded-
    message hooks are therefore no-ops, and `simulates=False` tells the consumer to
    skip snapshot re-injection (live snapshots already include our resting orders).

    Confirmations: a place/cancel REST 2xx yields a synchronous ack (delivered via
    on_deliver); the public own-delta + private fill arrive later on the WS and are
    routed through the same consumer._deliver kinds as SimExchange. A hard reject
    (4xx) or rate-limit (429) is delivered as a 'reject' so the router frees the
    side instead of dead-locking in *_INFLIGHT."""

    simulates = False

    def __init__(self, view, tickers, *, on_deliver=None, api=None):
        from src.utils import api as _api
        self.view = view
        self.tickers = tickers
        self.on_deliver = on_deliver
        self.api = api or _api
        self.ids = SimIds()                 # client_order_id minting (ours; UUIDs from prod too)
        self._orders: dict[str, _ProdRestingOrder] = {}   # handle(order_id) -> resting order
        self._coid_to_handle: dict[str, str] = {}
        self._pending: list[tuple[str, dict]] = []        # (kind, msg) to deliver on next drain
        self._own_trades: set[str] = set()                # trade_ids known to be ours
        self._cur_lts = 0.0

    @property
    def orders(self):
        return self._orders

    def set_deliver(self, cb):
        self.on_deliver = cb

    # ---- order entry (OrderRouter backend; same signature as SimExchange) ----
    def place(self, decide_lts, ticker, side, price, qty):
        self._cur_lts = decide_lts
        coid = self.ids.coid()
        price_cents = int(round((price if side == "yes" else round(1.0 - price, 6)) * 100))
        action = "buy"
        try:
            resp = self.api.create_order(ticker, side, action, int(qty), price_cents,
                                         client_order_id = coid)
        except Exception as e:                       # network error -> treat as reject
            self._pending.append(("reject", {"client_order_id": coid, "kind": "place", "err": str(e)}))
            return None, coid
        if resp.status_code in (200, 201):
            order_id = resp.json()["order"]["order_id"]
            self._orders[order_id] = _ProdRestingOrder(ticker, side, price, float(qty), coid)
            self._coid_to_handle[coid] = order_id
            self._pending.append(("ack", ack_msg(ticker, side, coid, order_id, "place",
                                                 self._ms(decide_lts))))
            return order_id, coid
        # 429 rate-limit or hard 4xx -> the order did not rest; free the side
        self._pending.append(("reject", {"client_order_id": coid, "kind": "place",
                                         "status": resp.status_code}))
        return None, coid

    def cancel(self, decide_lts, coid):
        self._cur_lts = decide_lts
        handle = self._coid_to_handle.get(coid)
        order = self._orders.get(handle) if handle else None
        if order is None:
            return
        try:
            resp = self.api.cancel_order(handle)
        except Exception as e:
            self._pending.append(("reject", {"client_order_id": coid, "kind": "cancel", "err": str(e)}))
            return
        if resp.status_code in (200, 404):           # 404 == already gone (filled) -> treat as cancelled
            self._orders.pop(handle, None)
            self._coid_to_handle.pop(coid, None)
            self._pending.append(("ack", ack_msg(order.ticker, order.side, coid, handle, "cancel",
                                                 self._ms(decide_lts))))
        else:                                        # 429 / hard 4xx -> cancel failed, order still resting
            self._pending.append(("reject", {"client_order_id": coid, "kind": "cancel",
                                             "status": resp.status_code}))

    def drain(self, now: float):
        """Flush synchronous REST confirmations (place/cancel ack or reject). The
        public own-delta + private fill arrive asynchronously on the WS (run())."""
        self._cur_lts = max(self._cur_lts, now)
        if self.on_deliver is None:
            return
        pending, self._pending = self._pending, []
        for kind, msg in pending:
            self.on_deliver(kind, msg)

    # ---- recorded-message hooks: NO-OP in prod (no fill simulation) ----
    def on_recorded_snapshot(self, lts, ticker):
        return
    def on_recorded_delta(self, lts, ticker, side, price, delta):
        return
    def on_recorded_trade(self, lts, msg):
        return

    def own_levels(self, ticker):
        out = {"yes": {}, "no": {}}
        for o in self._orders.values():
            if o.ticker == ticker and is_pos(o.remaining):
                out[o.side][o.price] = out[o.side].get(o.price, 0.0) + o.remaining
        return out

    def _ms(self, lts):
        return int(round(lts * 1000))

    def reconcile(self):
        """Reconnect/startup safety: cancel ALL of our resting orders via REST so we
        start from a clean slate (no stale orders at any level), and reset local
        order/ledger state. The strategy re-places from the fresh book."""
        for o in self.api.get_orders(status = "resting"):
            try:
                self.api.cancel_order(o["order_id"])
            except Exception:
                pass
        self._orders.clear()
        self._coid_to_handle.clear()

    # ---- live driver: subscribe public + private feeds, raise the same events ----
    async def run(self, consumer):
        import asyncio
        import json
        import time
        import websockets
        from src.utils.api import WS_URL, ws_auth_headers
        asyncio.create_task(self._watchdog(consumer))   # independent reconciliation loop
        while True:
            try:
                async with websockets.connect(WS_URL, additional_headers = ws_auth_headers()) as ws:
                    # clean slate on (re)connect, then subscribe public + private fill
                    self.reconcile()
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                        "channels": ["orderbook_delta", "trade"], "market_tickers": self.tickers}}))
                    await ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {
                        "channels": ["fill", "market_positions"]}}))
                    print(f"ProdExchange subscribed: {len(self.tickers)} tickers + fill + market_positions")
                    async for raw in ws:
                        self._dispatch(consumer, json.loads(raw), time.time())
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                print(f"ProdExchange reconnect ({type(e).__name__}: {e}) in 5s...")
                await asyncio.sleep(5)

    # ---- watchdog: time-based reconciliation vs exchange truth (B6/B9) ----
    async def _watchdog(self, consumer, period=10.0, stuck_after=20.0):
        """The in-flight lock release is event-driven; if the confirming event is
        lost (dropped WS frame w/o disconnect, or a REST response lost after the
        exchange acted) only a TIMER can recover. Every `period`s this: (1) frees a
        side stuck in *_INFLIGHT > `stuck_after`s by checking REST order-status,
        (2) cancels orphan resting orders not in our book, (3) logs position drift
        vs the REST portfolio. Runs in ProdExchange (it owns the REST connection)."""
        import asyncio
        import time
        seen = {}
        while True:
            await asyncio.sleep(period)
            try:
                now = time.time()
                router = consumer.router
                inflight = set(router.inflight_sides())
                for k in [k for k in seen if k not in inflight]:
                    seen.pop(k, None)                       # resolved normally
                for k in inflight:
                    seen.setdefault(k, now)
                # (1) stuck in-flight -> reconcile against exchange order-status
                for key, t0 in list(seen.items()):
                    if now - t0 < stuck_after:
                        continue
                    ticker, side = key
                    coid = router.coid_for(ticker, side)
                    handle = self._coid_to_handle.get(coid)
                    resting = {o["order_id"] for o in
                               await asyncio.to_thread(self.api.get_orders, "resting", ticker)}
                    exists = bool(handle and handle in resting)
                    print(f"WATCHDOG: {key} stuck in-flight {now - t0:.0f}s -> "
                          f"exchange resting={exists}; reconciling")
                    router.reconcile_side(ticker, side, exists)
                    if not exists and handle:
                        self._orders.pop(handle, None)
                        self._coid_to_handle.pop(coid, None)
                    seen.pop(key, None)
                # (2) orphan resting orders (on exchange, not tracked) -> cancel
                live = await asyncio.to_thread(self.api.get_orders, "resting", None)
                for o in live:
                    if o["order_id"] not in self._orders:
                        print(f"WATCHDOG: orphan order {o['order_id'][:8]} on {o.get('ticker')} -> cancelling")
                        await asyncio.to_thread(self.api.cancel_order, o["order_id"])
                # (3) position drift vs REST portfolio (log loudly)
                rest = await asyncio.to_thread(self.api.get_positions)
                rq = {p["ticker"]: float(p.get("position", p.get("position_fp", 0))) for p in rest}
                for ticker, mm in consumer.mm_by_ticker.items():
                    ours = float(mm.inventory.get(ticker, 0.0))
                    theirs = rq.get(ticker, 0.0)
                    if abs(ours - theirs) > 1e-6:
                        print(f"WATCHDOG: POSITION DRIFT {ticker}: tracked={ours:+.2f} exchange={theirs:+.2f}")
            except Exception as e:
                print(f"watchdog error: {type(e).__name__}: {e}")

    def _dispatch(self, consumer, data, lts):
        """Route one WS message. MARKET messages -> consumer.on_book/on_trade (same
        as the replay driver); OUR messages (own delta tagged with client_order_id,
        private fill) -> consumer._deliver (same kinds as SimExchange).

        The strategy RE-DECIDES (requotes) ONLY on market book-changes + trades, exactly
        like sim: own deltas/fills only update state (ledger / inventory / PnL); the next
        market book-change or trade picks up their effect. No separate own-event requote."""
        self._cur_lts = lts
        mtype = data.get("type")
        msg = data.get("msg", {})
        # stamp the per-event timing context (#1 exchange ts, #2 local read ts) for
        # the consumer's order/decision logging (no-op when timing is off)
        if consumer._timing is not None:
            consumer._evt = {"exchange_ts": msg.get("ts_ms"), "read_ts": lts}
        if mtype == "orderbook_snapshot":
            ticker = msg["market_ticker"]
            self.view.apply_snapshot(ticker, msg.get("yes_dollars_fp", []), msg.get("no_dollars_fp", []))
            consumer.on_book(lts, ticker, None)
        elif mtype == "orderbook_delta":
            ticker = msg["market_ticker"]
            if msg.get("client_order_id"):                  # OUR order's book change
                consumer._deliver("public_delta", msg)      # ledger only; no requote
            else:                                           # market depth change
                self.view.apply_delta(ticker, msg["side"], msg["price_dollars"],
                                      float(msg["delta_fp"]), is_own = False)
                consumer.on_book(lts, ticker, msg)
        elif mtype == "trade":
            if msg.get("trade_id") in self._own_trades:     # our own print -> alpha skips it
                consumer._deliver("public_trade", msg)
            else:
                consumer.on_trade(lts, msg)
        elif mtype == "fill":                               # private, authoritative
            tid = msg.get("trade_id")
            if tid is not None:
                self._own_trades.add(tid)
                self.view.mark_own_fill(tid)
            handle = msg.get("order_id")
            o = self._orders.get(handle)
            if o is not None:
                o.remaining = max(0.0, o.remaining - float(msg["count_fp"]))
                if not is_pos(o.remaining):
                    self._orders.pop(handle, None)
                    self._coid_to_handle.pop(o.coid, None)
            consumer._deliver("private_fill", msg)          # inventory/PnL only; no requote
        elif mtype in ("market_positions", "market_position"):
            consumer.on_positions(msg)                      # authoritative -> overwrite positions; no requote
        elif mtype == "error":
            print(f"  WS error: {data}")

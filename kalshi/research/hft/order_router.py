"""
OrderRouter: the ONLY path passive orders flow through. The strategy declares a
DESIRED resting order per (ticker, side) via set_target(); the router asynchronously
drives the exchange toward it and owns ALL execution mechanics the strategy must NOT
know about — the in-flight lock (one outstanding action per side), the write
rate-limit budget, the passive-level invariants, and the per-order logging.

Per-side state machine (the in-flight lock):

  IDLE --place--> PLACE_INFLIGHT --ack--> RESTING --cancel--> CANCEL_INFLIGHT --ack--> IDLE
                                          RESTING --fill(partial)--> RESTING
                                          RESTING --fill(full)-----> IDLE

Reconciliation. set_target(t,s,want) stores the target and reconcile()s that side:
  - target differs from the resting order -> cancel; once IDLE, place the target;
  - rate-limited so only the cancel fits -> send the cancel, defer the place; the
    place fires on the next reconcile (next market event, an ack, or reconcile_all)
    once budget frees — never stacking a 2nd order on the side;
  - the target changes while a cancel is in flight -> reconcile() on the cancel-ack
    reads the LATEST target, so the NEW price is placed, never the stale one.
The strategy is unaware of all of this: it just re-declares the target each event.

Backends (SimExchange sim / ProdExchange prod) share place/cancel/drain/resting_for.
  - SimExchange delivers confirmations via the consumer's drain (inline at delays=0,
    scheduled at delays>0); at delays=0 a cancel+place completes inside one reconcile()
    (the inline drain frees the side), at delays>0 the place defers to a later
    reconcile — matching the synchronous sim exactly (this is the bit-identical path).
  - ProdExchange fires the REST in the background (non-blocking) and, when the ack
    lands, delivers it AND calls reconcile() so the deferred step fires off the latest
    target. reconcile_all() (a prod timer) is the safety net for rate-limit/reject
    deferrals when the market is quiet.

Confirmations arrive from the consumer's feed handlers and update STATE/ledger ONLY —
they never re-send orders (that would re-enter the reconcile drain): on_ack /
on_public_own_delta / on_private_fill / on_reject. In sim the deferred place is picked
up by the next event's pre-requote drain + reconcile; in prod by the ack-completion's
explicit reconcile + the periodic reconcile_all.
"""

import time

from research.hft.passive_fill import price_key
from src.utils.feps import is_pos, lte

IDLE = "idle"
PLACE_INFLIGHT = "place_inflight"
RESTING = "resting"
CANCEL_INFLIGHT = "cancel_inflight"


class OrderRouter:
    def __init__(self, backend, view, *, rate_limiter=None, max_queue=None,
                 on_reduce=None, on_order=None, log=None):
        self.backend = backend           # SimExchange (sim) / ProdExchange (prod)
        self.view = view                 # MarketView
        self.rate_limiter = rate_limiter # shared WriteRateLimiter (None -> unlimited)
        self.max_queue = max_queue       # passive queue-depth guard (None -> off)
        self._state: dict[tuple[str, str], str] = {}        # (ticker,side) -> state
        self._coid: dict[tuple[str, str], str] = {}         # (ticker,side) -> live coid
        # coid -> {ticker, side, price(key), qty, filled}
        self._orders: dict[str, dict] = {}
        # desired resting order per side: (price, qty) | None, + the decision context
        # ({event, alpha, expo}) used for logging the place/cancel it produces.
        self._target: dict[tuple[str, str], tuple | None] = {}
        self._ctx: dict[tuple[str, str], dict] = {}
        self._keys: list[tuple[str, str]] = []              # deterministic reconcile order
        self._applied_trades: set[str] = set()              # dedup private fills by trade_id
        # callback(ticker, side, qty, yes_price, action, reason, post) -> inventory/PnL
        self._on_reduce = on_reduce
        # callback(ticker, side, action, price, qty, t_sent, t_done, coid) AFTER a backend
        # send -> consumer timing log. None in sim (zero overhead).
        self._on_order = on_order
        # callback(lts, event, ticker, side, action, price, size, alpha, qa, expo) ->
        # consumer.log_order (the offline order_rows log; a no-op in live timing mode).
        self._log = log

    # ---- queries ----
    def state(self, ticker: str, side: str) -> str:
        return self._state.get((ticker, side), IDLE)

    def can_place(self, ticker: str, side: str) -> bool:
        return self.state(ticker, side) == IDLE

    def can_cancel(self, ticker: str, side: str) -> bool:
        return self.state(ticker, side) == RESTING

    def resting_order(self, ticker: str, side: str):
        """The backend resting order when RESTING (price/remaining/queue reads), else
        None. Looked up by coid via the backend so it works for the async prod backend
        (no synchronously-returned handle)."""
        if self.state(ticker, side) != RESTING:
            return None
        coid = self._coid.get((ticker, side))
        return self.backend.resting_for(coid) if coid else None

    # ---- desired-state interface (the ONLY ordering call the strategy makes) ----
    def set_target(self, lts: float, ticker: str, side: str, want, ctx=None):
        """Declare the desired resting order for (ticker, side): `want` is (price, qty)
        or None (no order). Store it + the decision context (for logging) and reconcile
        the side toward it immediately (synchronous — preserves per-side ordering)."""
        key = (ticker, side)
        if key not in self._ctx:
            self._keys.append(key)
        self._target[key] = want
        self._ctx[key] = ctx or {}
        self.reconcile(lts, ticker, side)

    def reconcile_all(self, lts: float):
        """Drive every tracked side toward its target (prod timer safety-net for
        rate-limit / reject deferrals that no market event would otherwise retry).
        Must stay await-free."""
        for ticker, side in list(self._keys):
            self.reconcile(lts, ticker, side)

    def reconcile(self, lts: float, ticker: str, side: str):
        """Take the next step toward the stored target for one side. Mirrors the old
        strategy `_reconcile_side` EXACTLY (same per-side order, same rate-limit and
        passive-invariant gates, same logging, same inline place-after-cancel via the
        backend drain) so the sim stays bit-identical; the only change is that `want`
        and the decision context come from the stored target instead of arguments."""
        # in-flight lock: one outstanding action per side; wait for the ack.
        if self.state(ticker, side) in (PLACE_INFLIGHT, CANCEL_INFLIGHT):
            return
        want = self._target.get((ticker, side))
        ctx = self._ctx.get((ticker, side), {})
        alpha, expo, event = ctx.get("alpha"), ctx.get("expo"), ctx.get("event")
        cur = self.resting_order(ticker, side)          # non-None iff RESTING
        if want is None:
            if cur is not None and self._try_cancel(lts):
                self.cancel(lts, ticker, side)
                self._log_order(lts, event, ticker, side, "cancel",
                                cur.price_f, cur.remaining, alpha, cur.queue_ahead, expo)
            return
        pk = price_key(want[0])
        if cur is not None:
            # Passive invariant: only rest where real liquidity backs the level;
            # a level emptied by a cancel is unsupported -> pull it
            supported = is_pos(self.view.depth(ticker, side, cur.price))
            if supported and cur.price == pk:
                return                                  # keep queue position
            if self._try_cancel(lts):
                self.cancel(lts, ticker, side)
                self._log_order(lts, event, ticker, side, "cancel",
                                cur.price_f, cur.remaining, alpha, cur.queue_ahead, expo)
            else:                                       # rate-limited: keep order, retry later
                self._log_order(lts, event, ticker, side, "rate_limited",
                                cur.price_f, cur.remaining, alpha, cur.queue_ahead, expo)
                return                                  # SKIP place -> never stack
        # place only if the side is free now (delays 0: cancel completed inline;
        # delays>0 / prod: a just-sent cancel is still in flight -> defer to a later
        # reconcile that re-reads the latest target)
        if not self.can_place(ticker, side):
            return
        # Passive invariant: never create a price level with no real backing
        if lte(self.view.depth(ticker, side, pk), 0):
            return
        # Queue-depth guard: a fresh join behind a huge displayed queue only fills
        # when the level breaks (worst selection)
        if self.max_queue is not None and self.view.depth(ticker, side, pk) > self.max_queue:
            return
        if not self._try_place(lts):
            return                                      # rate-limited: place retries later
        self.place(lts, ticker, side, want[0], want[1])
        o = self.resting_order(ticker, side)            # RESTING at delays 0; in-flight in prod
        qa = o.queue_ahead if o is not None else self.view.depth(ticker, side, pk)
        self._log_order(lts, event, ticker, side, "place", want[0], want[1], alpha, qa, expo)

    def _try_cancel(self, lts: float) -> bool:
        return self.rate_limiter is None or self.rate_limiter.try_cancel(lts)

    def _try_place(self, lts: float) -> bool:
        return self.rate_limiter is None or self.rate_limiter.try_place(lts)

    def _log_order(self, lts, event, ticker, side, action, price, size, alpha, qa, expo):
        if self._log is not None:
            self._log(lts, event, ticker, side, action, price, size, alpha, qa, expo)

    # ---- low-level senders (sim: confirm inline at delays 0 via the backend drain;
    #      prod: fire the REST in the background, ack arrives async) ----
    def place(self, lts: float, ticker: str, side: str, price: float, qty: float) -> str:
        if self.state(ticker, side) != IDLE:
            raise RuntimeError(f"OrderRouter.place on non-IDLE {(ticker, side)} "
                               f"= {self.state(ticker, side)} (in-flight lock)")
        t5 = time.time() if self._on_order else 0.0          # #5 sent to router->backend
        _fe_oid, coid = self.backend.place(lts, ticker, side, price, qty)
        t6 = time.time() if self._on_order else 0.0          # #6 backend send returned
        self._state[(ticker, side)] = PLACE_INFLIGHT
        self._coid[(ticker, side)] = coid
        self._orders[coid] = {"ticker": ticker, "side": side,
                              "price": price_key(price), "qty": qty}
        # deliver delay-0 confirmations now (state is recorded) -> RESTING this tick
        # (sim); a no-op in prod where the ack arrives asynchronously.
        self.backend.drain(lts)
        if self._on_order:
            self._on_order(ticker, side, "new", price, qty, t5, t6, coid)
        return coid

    def cancel(self, lts: float, ticker: str, side: str) -> bool:
        if self.state(ticker, side) != RESTING:
            raise RuntimeError(f"OrderRouter.cancel on non-RESTING {(ticker, side)} "
                               f"= {self.state(ticker, side)}")
        coid = self._coid[(ticker, side)]
        info = self._orders.get(coid, {})
        self._state[(ticker, side)] = CANCEL_INFLIGHT
        t5 = time.time() if self._on_order else 0.0
        self.backend.cancel(lts, coid)
        t6 = time.time() if self._on_order else 0.0
        self.backend.drain(lts)          # delay-0 cancel-ack -> IDLE this tick (sim)
        if self._on_order:
            self._on_order(ticker, side, "cancel", info.get("price"), info.get("qty"), t5, t6, coid)
        return True

    # ---- confirmations (from the consumer's feed handlers; STATE/ledger ONLY) ----
    def on_ack(self, msg: dict):
        coid = msg["client_order_id"]
        info = self._orders.get(coid)
        if info is None:
            return
        key = (info["ticker"], info["side"])
        if msg["ack"] == "place":
            if self._state.get(key) == PLACE_INFLIGHT:
                self._state[key] = RESTING
        else:                                    # cancel ack -> side free
            self._state[key] = IDLE
            self._coid.pop(key, None)
            self._orders.pop(coid, None)

    # ---- prod watchdog hooks (unused in sim) ----
    def inflight_sides(self):
        """(ticker, side) pairs currently stuck-able in a *_INFLIGHT state."""
        return [k for k, s in self._state.items() if s in (PLACE_INFLIGHT, CANCEL_INFLIGHT)]

    def coid_for(self, ticker: str, side: str):
        return self._coid.get((ticker, side))

    def force_state(self, ticker: str, side: str, resting: bool):
        """Watchdog reconciliation against exchange TRUTH (NOT the drain-loop
        reconcile): resting=True -> the order is live on the exchange (force RESTING);
        False -> it's gone (free -> IDLE). The watchdog calls this to break a stuck
        *_INFLIGHT; it must never re-send orders the way reconcile() does."""
        key = (ticker, side)
        if resting:
            self._state[key] = RESTING
        else:
            self._state[key] = IDLE
            coid = self._coid.pop(key, None)
            self._orders.pop(coid, None)

    def on_reject(self, coid: str, kind: str):
        """Exchange rejected a place/cancel (hard 4xx, or a retryable 429) — free the
        side so a later reconcile re-decides instead of dead-locking in *_INFLIGHT.
        place reject: the order never rested -> IDLE. cancel reject: the order is still
        resting -> back to RESTING (retry the cancel later). STATE only: the re-place /
        re-cancel is left to the next market event or the periodic reconcile_all, so a
        429 storm can't form a tight inline retry loop."""
        info = self._orders.get(coid)
        if info is None:
            return
        key = (info["ticker"], info["side"])
        if kind == "place":
            self._state[key] = IDLE
            self._coid.pop(key, None)
            self._orders.pop(coid, None)
        else:                                    # cancel failed -> order still live
            self._state[key] = RESTING

    def on_public_own_delta(self, msg: dict):
        """Keep the own-ledger in lockstep with the book (which the driver already
        updated by this same delta), so market-only reads stay correct. Message-based
        (reads the delta + the view's current own qty) so it's independent of the
        _orders lifecycle and robust to ack/delta delivery order. <=1 order per
        (ticker,side) means one order's qty lives at a level."""
        t, s, price = msg["market_ticker"], msg["side"], msg["price_dollars"]
        delta = float(msg["delta_fp"])
        new = max(0.0, self.view.own_qty(t, s, price) + delta)
        self.view.register(t, s, price, new)

    def on_private_fill(self, msg: dict):
        """Authoritative fill: inventory/PnL (via on_reduce) + resting-state transition.
        Deduped by trade_id (the public own-delta of the same fill only touches the
        ledger; PnL/state move here exactly once). STATE only — no reconcile (the next
        event/timer re-places toward the target)."""
        tid = msg.get("trade_id")
        if tid is not None:
            if tid in self._applied_trades:
                return
            self._applied_trades.add(tid)
        coid = msg["client_order_id"]
        info = self._orders.get(coid)
        ticker, side = msg["market_ticker"], msg["side"]
        qty = float(msg["count_fp"])
        yes_price = float(msg["yes_price_dollars"])
        if self._on_reduce is not None:
            self._on_reduce(ticker, side, qty, yes_price, msg.get("action"),
                            msg.get("reason", "trade"), msg.get("post_position_fp"))
        # state: free the side once the order is fully filled. Detect via cumulative
        # fills vs the order's qty (backend-agnostic — sim drops the fe order at the
        # same point, prod has no fill engine). A fill arriving while CANCEL_INFLIGHT
        # (cancel/fill race: it filled before our cancel landed) also frees the side.
        if info is not None:
            key = (info["ticker"], info["side"])
            info["filled"] = info.get("filled", 0.0) + qty
            if info["filled"] >= info["qty"] - 1e-9 and self._state.get(key) in (RESTING, CANCEL_INFLIGHT):
                self._state[key] = IDLE
                self._coid.pop(key, None)
                self._orders.pop(coid, None)

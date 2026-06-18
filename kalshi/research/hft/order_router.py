"""
OrderRouter: the ONLY path passive orders flow through. Enforces ONE outstanding
action per (ticker, side) — the in-flight lock: while a place/cancel is in flight
(sent, no confirmation yet) no other order may be sent on that side. Per-side
state machine:

  IDLE --place--> PLACE_INFLIGHT --ack--> RESTING --cancel--> CANCEL_INFLIGHT --ack--> IDLE
                                          RESTING --fill(partial)--> RESTING
                                          RESTING --fill(full)-----> IDLE

The router SENDS via `backend` (SimExchange in sim / a real order API in prod) and
RECEIVES confirmations from the consumer's feed handlers:
  on_ack(msg)               private REST ack for place/cancel -> release the lock
  on_public_own_delta(msg)  our orderbook_delta (book already applied by the driver)
                            -> register/reduce the MarketView own-ledger so reads
                            stay market-only (book - ledger). Book + ledger move
                            together on this message, so reads are correct in every
                            regime (incl. untagged snapshots).
  on_private_fill(msg)      authoritative fill -> inventory/PnL (via on_reduce) +
                            resting-state transition, deduped by trade_id.

At delays=0 the SimExchange delivers confirmations inline, so place()->RESTING and
cancel()->IDLE complete synchronously (lock non-binding) -> cancel-before-replace
in one tick, matching the current synchronous sim.
"""

import time

from research.hft.passive_fill import price_key
from src.utils.feps import is_pos

IDLE = "idle"
PLACE_INFLIGHT = "place_inflight"
RESTING = "resting"
CANCEL_INFLIGHT = "cancel_inflight"


class OrderRouter:
    def __init__(self, backend, view, *, on_reduce=None, on_order=None):
        self.backend = backend           # SimExchange (sim) / order API (prod)
        self.view = view                 # MarketView
        self._state: dict[tuple[str, str], str] = {}        # (ticker,side) -> state
        self._coid: dict[tuple[str, str], str] = {}         # (ticker,side) -> live coid
        # coid -> {ticker, side, fe_oid, price(key), qty, led}  (led = current ledger qty)
        self._orders: dict[str, dict] = {}
        self._applied_trades: set[str] = set()              # dedup private fills by trade_id
        # callback(ticker, side, qty, yes_price, action) -> consumer inventory/PnL/log
        self._on_reduce = on_reduce
        # callback(ticker, side, action, price, qty, t_sent, t_done) AFTER the backend
        # send returns -> consumer timing log. None in sim (zero overhead).
        self._on_order = on_order

    # ---- queries (strategy) ----
    def state(self, ticker: str, side: str) -> str:
        return self._state.get((ticker, side), IDLE)

    def can_place(self, ticker: str, side: str) -> bool:
        return self.state(ticker, side) == IDLE

    def can_cancel(self, ticker: str, side: str) -> bool:
        return self.state(ticker, side) == RESTING

    def resting_order(self, ticker: str, side: str):
        """The backend RestingOrder when RESTING (price/remaining/queue reads), else None."""
        if self.state(ticker, side) != RESTING:
            return None
        info = self._orders.get(self._coid.get((ticker, side)))
        return self.backend.orders.get(info["fe_oid"]) if info else None

    # ---- order entry (strategy must gate on can_place/can_cancel) ----
    def place(self, lts: float, ticker: str, side: str, price: float, qty: float) -> str:
        if self.state(ticker, side) != IDLE:
            raise RuntimeError(f"OrderRouter.place on non-IDLE {(ticker, side)} "
                               f"= {self.state(ticker, side)} (in-flight lock; gate on can_place)")
        t5 = time.time() if self._on_order else 0.0          # #5 sent to router->backend
        fe_oid, coid = self.backend.place(lts, ticker, side, price, qty)
        t6 = time.time() if self._on_order else 0.0          # #6 backend send finalized
        self._state[(ticker, side)] = PLACE_INFLIGHT
        self._coid[(ticker, side)] = coid
        self._orders[coid] = {"ticker": ticker, "side": side, "fe_oid": fe_oid,
                              "price": price_key(price), "qty": qty}
        # deliver delay-0 confirmations now (state is recorded) -> RESTING this tick
        self.backend.drain(lts)
        # emit timing AFTER the order is placed -> off the order's critical path
        if self._on_order:
            self._on_order(ticker, side, "new", price, qty, t5, t6)
        return coid

    def cancel(self, lts: float, ticker: str, side: str) -> bool:
        if self.state(ticker, side) != RESTING:
            raise RuntimeError(f"OrderRouter.cancel on non-RESTING {(ticker, side)} "
                               f"= {self.state(ticker, side)} (gate on can_cancel)")
        coid = self._coid[(ticker, side)]
        info = self._orders.get(coid, {})
        self._state[(ticker, side)] = CANCEL_INFLIGHT
        t5 = time.time() if self._on_order else 0.0
        self.backend.cancel(lts, coid)
        t6 = time.time() if self._on_order else 0.0
        self.backend.drain(lts)          # delay-0 cancel-ack -> IDLE this tick
        if self._on_order:
            self._on_order(ticker, side, "cancel", info.get("price"), info.get("qty"), t5, t6)
        return True

    # ---- confirmations (from the consumer's feed handlers) ----
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

    def reconcile_side(self, ticker: str, side: str, resting: bool):
        """Watchdog reconciliation against exchange truth: resting=True -> the order
        is live on the exchange (force RESTING); False -> it's gone (free -> IDLE)."""
        key = (ticker, side)
        if resting:
            self._state[key] = RESTING
        else:
            self._state[key] = IDLE
            coid = self._coid.pop(key, None)
            self._orders.pop(coid, None)

    def on_reject(self, coid: str, kind: str):
        """Exchange rejected a place/cancel (hard 4xx, NOT a retryable 429) — free
        the side so the strategy re-decides instead of dead-locking in *_INFLIGHT.
        place reject: the order never rested -> IDLE. cancel reject: the order is
        still resting -> back to RESTING (retry the cancel on a later tick)."""
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
        ledger; PnL/state move here exactly once)."""
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
            self._on_reduce(ticker, side, qty, yes_price, msg.get("action"), msg.get("reason", "trade"))
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

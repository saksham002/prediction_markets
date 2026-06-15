"""
Queue-position passive fill engine for paper trading on Kalshi books.

Simulated resting BUY orders (side "yes" or "no") join a price level behind
the displayed quantity. queue_ahead estimates real contracts ahead of us.

Mechanics, robust to Kalshi's channel ordering (the orderbook_delta for a
fill precedes its trade message ~98% of the time, June 2026 measurement):

  - Trade prints at our level decrement queue_ahead (front consumption is
    exact); overflow beyond queue_ahead fills us. Trades stamped before our
    order could have reached the exchange (placement + forward delay) are
    ignored — we were not on the book when they matched.
  - Negative book deltas at our level are buffered as "pending reductions"
    for PENDING_WINDOW_S. A pending reduction is explained (discarded) by its
    matching trade message; if unexplained when it expires it was a cancel.
  - queue_ahead is capped at displayed + unexpired pending reductions. So a
    trade reflected in both channels is counted once (the pending entry
    shields the cap until the trade message lands), while cancels tighten the
    cap only after the window — and cancels behind us never shrink the queue
    estimate beyond what the displayed total forces (pessimistic for fills).
  - The opposite book side resting at or through our price for longer than
    CROSS_GRACE_S fills us fully: a real order priced to match us rested only
    because our simulated order isn't on the exchange. The grace period
    filters transient crossed/locked states that appear mid-sweep while the
    delta stream updates one level at a time (those fills arrive as trade
    prints and are queue-accounted normally).

Prices are 4-decimal dollar strings to match book level keys exactly.
"""

from dataclasses import dataclass, field

PENDING_WINDOW_S = 1.0
# One-way order-entry latency to Kalshi. An order decided at local time P is
# live on the book at P + FORWARD_DELAY_S; only trades matched after that can
# fill us: fill iff placed_lts + forward_delay <= trade ts. Measured June 2026
# from babel (measure_latency.py): WS ping RTT median 35.6ms -> one-way ~18ms;
# feed receive offset median +19ms; exchange ts_ms verified Unix-epoch match
# time with negligible clock offset. Default rounds up to 20ms.
FORWARD_DELAY_S = 0.020
CROSS_GRACE_S = 0.5


def price_key(price: float) -> str:
    return f"{price:.4f}"


@dataclass
class RestingOrder:
    order_id: int
    ticker: str
    side: str            # "yes" or "no" — which side of the book we bid on
    price: str           # own-side price as 4-decimal dollar string
    qty: float
    placed_lts: float
    queue_ahead: float
    filled: float = 0.0
    crossed_since: float | None = None

    @property
    def remaining(self) -> float:
        return self.qty - self.filled

    @property
    def price_f(self) -> float:
        return float(self.price)


@dataclass
class Fill:
    lts: float
    order: RestingOrder
    qty: float
    reason: str          # "trade" or "cross"


class PassiveFillEngine:
    def __init__(self, books: dict, forward_delay: float = FORWARD_DELAY_S):
        self.books = books
        self.forward_delay = forward_delay
        self.orders: dict[int, RestingOrder] = {}
        self._by_ticker: dict[str, set[int]] = {}
        # Unexplained recent negative deltas per (ticker, side, price):
        # list of [lts, qty_remaining]
        self._pending: dict[tuple[str, str, str], list] = {}
        self._next_id = 1

    def _book_side(self, ticker: str, side: str):
        book = self.books[ticker]
        return book.yes if side == "yes" else book.no

    def displayed(self, ticker: str, side: str, price: str) -> float:
        return self._book_side(ticker, side).levels.get(price, 0.0)

    def place(self, lts: float, ticker: str, side: str, price: float, qty: float) -> int:
        key = price_key(price)
        order = RestingOrder(
            order_id = self._next_id,
            ticker = ticker,
            side = side,
            price = key,
            qty = qty,
            placed_lts = lts,
            queue_ahead = self.displayed(ticker, side, key),
        )
        self._next_id += 1
        self.orders[order.order_id] = order
        self._by_ticker.setdefault(ticker, set()).add(order.order_id)
        return order.order_id

    def cancel(self, order_id: int):
        order = self.orders.pop(order_id, None)
        if order is not None:
            self._by_ticker[order.ticker].discard(order_id)

    def orders_for(self, ticker: str) -> list[RestingOrder]:
        return [self.orders[oid] for oid in self._by_ticker.get(ticker, ())]

    def _remove_filled(self, order: RestingOrder):
        if order.remaining <= 0:
            self.cancel(order.order_id)

    def _crossing_qty(self, order: RestingOrder) -> float | None:
        """Displayed size of the opposite side resting at or through our price.

        None if not crossed. A real crossing order matches the real queue
        ahead of us first, so only its overflow can fill us.
        """
        opp_side = "no" if order.side == "yes" else "yes"
        book_side = self._book_side(order.ticker, opp_side)
        if not book_side.levels:
            return None
        total = 0.0
        for price_str, qty in book_side.levels.items():
            implied_ask = round(1.0 - float(price_str), 6)  # in our price space
            if implied_ask <= order.price_f + 1e-9:
                total += qty
        return total if total > 0 else None

    def _pending_total(self, lts: float, level_key: tuple) -> float:
        """Expire old entries, return remaining pending reduction at level."""
        entries = self._pending.get(level_key)
        if not entries:
            return 0.0
        live = [e for e in entries if lts - e[0] < PENDING_WINDOW_S]
        if live:
            self._pending[level_key] = live
            return sum(e[1] for e in live)
        del self._pending[level_key]
        return 0.0

    def _cap_order(self, lts: float, order: RestingOrder):
        level_key = (order.ticker, order.side, order.price)
        cap = self.displayed(order.ticker, order.side, order.price) + self._pending_total(lts, level_key)
        order.queue_ahead = min(order.queue_ahead, cap)

    def record_delta(self, lts: float, ticker: str, side: str, price: str, delta: float):
        """Call for each raw orderbook_delta BEFORE on_book (negative deltas only matter)."""
        if delta >= 0 or ticker not in self._by_ticker:
            return
        level_key = (ticker, side, price)
        for order in self.orders_for(ticker):
            if order.side == side and order.price == price:
                self._pending.setdefault(level_key, []).append([lts, -delta])
                return

    def on_snapshot(self, lts: float, ticker: str) -> list[Fill]:
        """Book replaced wholesale (reconnect): drop stale pending, re-cap."""
        for key in [k for k in self._pending if k[0] == ticker]:
            del self._pending[key]
        return self.on_book(lts, ticker)

    def on_book(self, lts: float, ticker: str) -> list[Fill]:
        """Call after the book for `ticker` has been updated."""
        fills = []
        for order in self.orders_for(ticker):
            self._cap_order(lts, order)
            crossing_qty = self._crossing_qty(order)
            if crossing_qty is not None:
                if order.crossed_since is None:
                    order.crossed_since = lts
                elif lts - order.crossed_since >= CROSS_GRACE_S:
                    # The crossing order fills the real queue ahead of us first;
                    # only its overflow reaches our simulated order.
                    fill_qty = min(order.remaining, max(0.0, crossing_qty - order.queue_ahead))
                    if fill_qty > 0:
                        order.filled += fill_qty
                        fills.append(Fill(lts = lts, order = order, qty = fill_qty, reason = "cross"))
                        self._remove_filled(order)
            else:
                order.crossed_since = None
        return fills

    def on_trade(self, lts: float, msg: dict) -> list[Fill]:
        ticker = msg["market_ticker"]
        if ticker not in self._by_ticker:
            return []
        if msg["taker_side"] == "yes":
            maker_side, maker_price = "no", msg["no_price_dollars"]
        else:
            maker_side, maker_price = "yes", msg["yes_price_dollars"]
        trade_qty = float(msg["count_fp"])
        trade_ts = msg["ts_ms"] / 1000.0 if "ts_ms" in msg else float(msg["ts"])
        level_key = (ticker, maker_side, maker_price)

        matching = [
            o for o in self.orders_for(ticker)
            if o.side == maker_side and o.price == maker_price
        ]
        # Cap BEFORE consuming pending: the pending entry for this very trade's
        # delta must shield the cap, or the trade would be counted twice.
        for order in matching:
            self._cap_order(lts, order)

        # Explain pending reductions at this level (consumed regardless of
        # whether any order predates the trade — the delta was this trade)
        remaining_explain = trade_qty
        entries = self._pending.get(level_key, [])
        while entries and remaining_explain > 0:
            consumed = min(entries[0][1], remaining_explain)
            entries[0][1] -= consumed
            remaining_explain -= consumed
            if entries[0][1] <= 0:
                entries.pop(0)
        if not entries and level_key in self._pending:
            del self._pending[level_key]

        fills = []
        for order in matching:
            # Fill iff placed_lts + forward_delay <= trade exchange ts: the
            # order must have reached the exchange before the trade matched
            if trade_ts < order.placed_lts + self.forward_delay:
                continue
            overflow = trade_qty - order.queue_ahead
            order.queue_ahead = max(0.0, order.queue_ahead - trade_qty)
            if overflow > 0:
                fill_qty = min(overflow, order.remaining)
                if fill_qty > 0:
                    order.filled += fill_qty
                    fills.append(Fill(lts = lts, order = order, qty = fill_qty, reason = "trade"))
                    self._remove_filled(order)
        return fills

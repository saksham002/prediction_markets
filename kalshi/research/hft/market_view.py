"""
MarketView: the single market-only view of the order book.

Both sim and prod process a book + message stream that ALREADY contain our own
orders (sim injects them via SimExchange exactly as prod's feed delivers them).
So there is ONE unified path — no `live` flag. To keep every market-state read
(obi/mom, the supported-level guards, best-bid/touch, spread, agg level-factors)
market-only, ALL such reads funnel through MarketView, which exposes the book
MINUS our own resting orders (the own-ledger). The book = market + ours at all
times; reads subtract the ledger -> market-only.

Mechanics:
  - `apply_delta`: OUR own deltas (is_own=True) change our qty -> applied directly;
    MARKET deltas (is_own=False) reduce only the MARKET portion (clamped at 0) so a
    recorded/aggregated market reduction never eats into our resting qty.
  - reads subtract the own-resting ledger — MANDATORY because orderbook_snapshot
    (every reconnect) is aggregated and UNTAGGED, so our resting qty re-enters the
    book with no client_order_id.
  - the OrderRouter drives `register`/`release`/`mark_own_fill`.

When nothing is registered (analysis callers that never place orders), the ledger
is empty so reads return the RAW book — behaviour-neutral.

This module also owns `TopOfBook` and the book-math helpers (`_depth`,
`market_obi`, `_best_bid_ex`); `alphas.py` re-exports them for back-compat.
"""

import heapq
from collections import defaultdict
from dataclasses import dataclass

from src.utils.orderbook import MarketBook
from src.utils.feps import is_pos, lte

OBI_LEVELS = 3
OBI_LEVEL_DECAY = 0.5


@dataclass
class TopOfBook:
    yes_bid: float | None = None
    yes_bid_qty: float | None = None
    yes_ask: float | None = None
    yes_ask_qty: float | None = None

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round(self.yes_ask - self.yes_bid, 6)


# ---- book-math helpers (own = price_key -> our resting qty to subtract) ----
def _depth(book_side, levels: int = OBI_LEVELS, decay: float = OBI_LEVEL_DECAY,
           own: dict | None = None) -> float | None:
    """Geometrically weighted depth over the top `levels` price levels; `own`
    subtracts our resting qty per level (market-only depth)."""
    lv = book_side.levels
    if not lv:
        return None
    if own:
        lv = {p: q - own.get(p, 0.0) for p, q in lv.items()}
        lv = {p: q for p, q in lv.items() if is_pos(q)}
        if not lv:
            return None
    if len(lv) <= levels:
        prices = sorted(lv.keys(), key = float, reverse = True)
    else:
        prices = heapq.nlargest(levels, lv.keys(), key = float)
    total = 0.0
    w = 1.0
    for p in prices:
        total += w * lv[p]
        w *= decay
    return total


def market_obi(book, own_yes: dict | None = None, own_no: dict | None = None) -> float | None:
    """(bid_depth - ask_depth) / (bid_depth + ask_depth) in YES space, market-only."""
    bid_depth = _depth(book.yes, own = own_yes)
    ask_depth = _depth(book.no, own = own_no)
    if bid_depth is None or ask_depth is None:
        return None
    total = bid_depth + ask_depth
    if lte(total, 0):
        return None
    return (bid_depth - ask_depth) / total


def _best_bid_ex(book_side, own: dict | None = None) -> tuple[float | None, float | None]:
    """Market-only best bid: highest price whose displayed qty exceeds our own
    resting qty there. Returns (price, market_qty). Falls back to raw best_bid
    when we hold nothing on this side."""
    if not own:
        return book_side.best_bid()
    best, best_q = None, None
    for p, q in book_side.levels.items():
        mq = q - own.get(p, 0.0)
        if is_pos(mq) and (best is None or float(p) > best):
            best, best_q = float(p), mq
    return best, best_q


class MarketView:
    def __init__(self, books: dict | None = None):
        self.books: dict[str, MarketBook] = books if books is not None else defaultdict(MarketBook)
        # our resting qty: (ticker, side) -> {price_key: qty}
        self._own: dict[tuple[str, str], dict[str, float]] = {}
        self._own_trade_ids: set[str] = set()

    # ---- own-resting ledger (driven by OrderRouter) ----
    def register(self, ticker: str, side: str, price_key_str: str, qty: float):
        level = self._own.setdefault((ticker, side), {})
        if qty <= 0:
            level.pop(price_key_str, None)
            if not level:
                self._own.pop((ticker, side), None)
        else:
            level[price_key_str] = qty

    def release(self, ticker: str, side: str, price_key_str: str):
        self.register(ticker, side, price_key_str, 0.0)

    def own_qty(self, ticker: str, side: str, price_key_str: str) -> float:
        """Our currently-registered resting qty at a level (0 if none)."""
        return self._own.get((ticker, side), {}).get(price_key_str, 0.0)

    def own_levels(self, ticker: str) -> dict:
        """{side: {price_key: qty}} of our registered resting qty (for snapshot
        re-injection: an aggregated/untagged snapshot must include our orders)."""
        return {side: dict(self._own.get((ticker, side), {})) for side in ("yes", "no")}

    def mark_own_fill(self, trade_id: str):
        self._own_trade_ids.add(trade_id)

    def _own_for(self, ticker: str, side: str) -> dict | None:
        """The own-ledger for (ticker, side) to subtract on reads (None if empty —
        analysis callers never register, so reads return the raw book)."""
        return self._own.get((ticker, side)) or None

    # ---- book maintenance (driven by Replayer / LiveFeed) ----
    def apply_snapshot(self, ticker: str, yes_levels, no_levels):
        book = self.books[ticker]
        book.yes.load_snapshot(yes_levels)
        book.no.load_snapshot(no_levels)

    def apply_delta(self, ticker: str, side: str, price: str, delta: float, *, is_own: bool = False):
        # The book = market + our orders at all times; reads subtract the own-ledger.
        # OUR OWN deltas (place/fill/cancel) change our qty -> apply directly.
        # A MARKET delta (is_own=False) must reduce only the MARKET portion: clamp it
        # at 0 so a recorded market-only reduction can never eat into our resting qty
        # (the commingled level is market+own; without this clamp a market delta whose
        # magnitude exceeds the market portion would drain our order). Mirrors prod,
        # where another participant's cancel only removes their own depth.
        book_side = self.books[ticker].yes if side == "yes" else self.books[ticker].no
        own = 0.0 if is_own else self.own_qty(ticker, side, price)
        if is_own or own <= 0:
            book_side.apply_delta(price, delta)
            return
        cur = book_side.levels.get(price, 0.0)
        market = cur - own
        if market + delta >= 0:
            book_side.apply_delta(price, delta)               # no clamp -> byte-exact
        else:
            book_side.apply_delta(price, -market)             # clamp: market portion -> 0

    # ---- market-only reads (replace direct book reads everywhere) ----
    def depth(self, ticker: str, side: str, price: str) -> float:
        book_side = self.books[ticker].yes if side == "yes" else self.books[ticker].no
        raw = book_side.levels.get(price, 0.0)
        own = self._own_for(ticker, side)
        return max(0.0, raw - own.get(price, 0.0)) if own else raw

    def best_bid(self, ticker: str, side: str) -> tuple[float | None, float | None]:
        book_side = self.books[ticker].yes if side == "yes" else self.books[ticker].no
        return _best_bid_ex(book_side, self._own_for(ticker, side))

    def top(self, ticker: str) -> TopOfBook:
        yb, ybq = self.best_bid(ticker, "yes")
        nb, nbq = self.best_bid(ticker, "no")
        ya = None if nb is None else round(1.0 - nb, 6)
        return TopOfBook(yes_bid = yb, yes_bid_qty = ybq, yes_ask = ya, yes_ask_qty = nbq)

    def obi(self, ticker: str) -> float | None:
        return market_obi(self.books[ticker],
                          own_yes = self._own_for(ticker, "yes"),
                          own_no = self._own_for(ticker, "no"))

    def market_levels(self, ticker: str, side: str) -> dict[str, float]:
        book_side = self.books[ticker].yes if side == "yes" else self.books[ticker].no
        own = self._own_for(ticker, side)
        if not own:
            return book_side.levels
        return {p: q - own.get(p, 0.0) for p, q in book_side.levels.items()
                if is_pos(q - own.get(p, 0.0))}

    # ---- own-message identification (public feed) ----
    def is_own_delta(self, msg: dict) -> bool:
        # On the authenticated WS, Kalshi tags ONLY our own orderbook_delta with
        # client_order_id, so its presence identifies the delta as ours.
        return bool(msg.get("client_order_id"))

    def is_own_trade(self, msg: dict) -> bool:
        return msg.get("trade_id") in self._own_trade_ids

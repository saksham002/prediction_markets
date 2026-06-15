"""
Orderbook state management for Kalshi markets.
"""

from dataclasses import dataclass, field


@dataclass
class BookSide:
    levels: dict[str, float] = field(default_factory = dict)
    # Cached best price (float, key) — rescan only when the best level empties
    _best_f: float | None = None
    _best_key: str | None = None

    def _rescan_best(self):
        if not self.levels:
            self._best_f = None
            self._best_key = None
            return
        best_key = max(self.levels.keys(), key = float)
        self._best_key = best_key
        self._best_f = float(best_key)

    def apply_delta(self, price: str, delta: float):
        current = self.levels.get(price, 0.0)
        new_qty = current + delta
        # Epsilon: fractional count_fp quantities leave float residue (e.g.
        # 0.1 + 0.2 - 0.3 > 0) that would linger as phantom zero-qty levels
        if new_qty <= 1e-9:
            self.levels.pop(price, None)
            if price == self._best_key:
                self._rescan_best()
        else:
            self.levels[price] = new_qty
            if self._best_f is None or float(price) > self._best_f:
                self._best_key = price
                self._best_f = float(price)

    def best_bid(self) -> tuple[float | None, float | None]:
        if self._best_key is None:
            return None, None
        return self._best_f, self.levels[self._best_key]

    def load_snapshot(self, levels: list[list[str]]):
        self.levels.clear()
        for price, qty in levels:
            q = float(qty)
            if q > 0:
                self.levels[price] = q
        self._rescan_best()


@dataclass
class MarketBook:
    yes: BookSide = field(default_factory = BookSide)
    no: BookSide = field(default_factory = BookSide)

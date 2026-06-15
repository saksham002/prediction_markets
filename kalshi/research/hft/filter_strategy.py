"""
FilterStrategy: the market-condition gate the MM strategy applies before it
will quote a leg, factored out so correlation studies can restrict to the same
data points the strategy would actually act on.

Mirrors PairMM._desired_sides (mm_sim.py): a leg is quotable only when both
sides of the touch are present, the spread is in [min_spread, max_spread], and
the touch sits inside [price_min, price_max]. Position / budget / inventory
gates are intentionally NOT here — those are strategy state, not market-data
filters, and don't define which price observations are tradable.

Defaults match the mm_sim argparse defaults (price_min 0.05, price_max 0.95,
max_spread 0.01) and the hard-coded min_spread 0.005 (reject crossed/locked).
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class FilterStrategy:
    price_min: float = 0.05
    price_max: float = 0.95
    max_spread: float = 0.01
    min_spread: float = 0.005

    def mask(self, yes_bid, yes_ask) -> np.ndarray:
        """Boolean array: True where a leg with this (yes_bid, yes_ask) is quotable."""
        yb = np.asarray(yes_bid, dtype = float)
        ya = np.asarray(yes_ask, dtype = float)
        spread = ya - yb
        return (
            ~np.isnan(yb) & ~np.isnan(ya)
            & (yb >= self.price_min - 1e-9)
            & (ya <= self.price_max + 1e-9)
            & (spread >= self.min_spread - 1e-9)
            & (spread <= self.max_spread + 1e-9)
        )

    def allows(self, yes_bid, yes_ask) -> bool:
        """Scalar form of mask() for one (yes_bid, yes_ask) touch."""
        if yes_bid is None or yes_ask is None:
            return False
        spread = yes_ask - yes_bid
        return (yes_bid >= self.price_min - 1e-9
                and yes_ask <= self.price_max + 1e-9
                and self.min_spread - 1e-9 <= spread <= self.max_spread + 1e-9)

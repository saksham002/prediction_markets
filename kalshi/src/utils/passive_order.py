"""
Resting passive buy order for Kalshi YES/NO markets.

A passive BUY joins the book at or below the best bid on its side and fills
when the opposite side of the book crosses the resting price. This module
provides the order-state container plus a fill check against current top-of-book.
"""

from dataclasses import dataclass


@dataclass
class PassiveOrder:
    """
    A resting passive BUY on one side of a Kalshi market.

    side = "yes" or "no" — which side of the book we're buying.
    price is the limit price (in the side's own price space).
    qty is the order size.
    """

    ticker: str
    side: str
    price: float
    qty: float

    def check_fill(self, yes_bid: float | None, yes_ask: float | None) -> bool:
        """
        Returns True if the opposite side of the book has crossed this order's price.

        For a BUY YES at P: fills when yes_ask <= P.
        For a BUY NO at P:  fills when no_ask = 1 - yes_bid <= P, i.e., yes_bid >= 1 - P.
        """
        if self.side == "yes":
            return yes_ask is not None and yes_ask <= self.price
        if self.side == "no":
            return yes_bid is not None and yes_bid >= 1.0 - self.price
        raise ValueError(f"Unknown side: {self.side}")

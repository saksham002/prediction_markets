"""
Generic position and PnL tracker for prediction market strategies.

Supports both arb strategies (multi-leg, locked-in profit) and directional
strategies (single-leg, mark-to-market or resolution-based PnL).
"""

from dataclasses import dataclass, field
import math

from src.utils.feps import is_pos


@dataclass
class Position:
    ticker: str
    side: str          # "long" or "short"
    qty: float         # total contracts held
    avg_price: float   # volume-weighted average entry price


class PnL:
    def __init__(
        self,
        *,
        charge_fees: bool = False,
        fee_model: str | None = None,
    ):
        self.positions: dict[str, Position] = {}
        self.realized_pnl: float = 0.0
        self.fees_paid: float = 0.0
        # Per-ticker attribution (train/test splits, per-game reports)
        self.realized_by_ticker: dict[str, float] = {}
        self.fees_by_ticker: dict[str, float] = {}
        self.charge_fees = charge_fees
        self.fee_model = fee_model
        # Lazy-populated fee caches: market ticker → series ticker, and series → (multiplier, fee_type)
        self.market_to_series: dict[str, str] = {}
        self.series_fees: dict[str, tuple[float, str]] = {}

    @staticmethod
    def kalshi_taker_fee(qty: float, price: float, fee_multiplier: float = 1.0) -> float:
        """
        Kalshi trade-fee approximation used in this repo.

        This matches the existing threshold-study assumption:
        fee = ceil(0.07 * multiplier * qty * price * (1 - price) * 100) / 100
        """
        raw = 0.07 * fee_multiplier * qty * price * (1.0 - price)
        return math.ceil(raw * 100) / 100

    @staticmethod
    def kalshi_maker_fee(qty: float, price: float, fee_multiplier: float = 1.0) -> float:
        """
        Kalshi maker fee (fee schedule, June 2026): only charged on series with
        fee_type == "quadratic_with_maker_fees", at 1/4 the taker rate:
        fee = ceil(0.0175 * multiplier * qty * price * (1 - price) * 100) / 100
        """
        raw = 0.0175 * fee_multiplier * qty * price * (1.0 - price)
        return math.ceil(raw * 100) / 100

    def _series_for(self, ticker: str) -> str:
        series = self.market_to_series.get(ticker)
        if series is None:
            series = ticker.split("-", 1)[0]
            self.market_to_series[ticker] = series
        return series

    def _series_fee(self, series: str) -> tuple[float, str]:
        if series not in self.series_fees:
            from src.utils.api import fetch_series_fee
            self.series_fees[series] = fetch_series_fee(series)
        return self.series_fees[series]

    def _trade_fee(self, ticker: str, qty: float, price: float, is_maker: bool = False) -> float:
        if not self.charge_fees:
            return 0.0
        if self.fee_model is None:
            return 0.0
        if self.fee_model == "kalshi":
            series = self._series_for(ticker)
            multiplier, fee_type = self._series_fee(series)
            if fee_type in ("quadratic", "quadratic_with_maker_fees"):
                if is_maker:
                    # Makers pay nothing on plain-quadratic series, 1/4 taker rate otherwise
                    if fee_type == "quadratic":
                        return 0.0
                    return self.kalshi_maker_fee(qty, price, fee_multiplier = multiplier)
                return self.kalshi_taker_fee(qty, price, fee_multiplier = multiplier)
            if fee_type == "none":
                return 0.0
            raise ValueError(f"Unsupported fee_type for series {series}: {fee_type}")
        raise ValueError(f"Unsupported fee model: {self.fee_model}")

    def _direction(self, side: str) -> int:
        return 1 if side == "long" else -1

    def trade(self, ticker: str, side: str, qty: float, price: float, is_maker: bool = False):
        """Record a trade. Same-side trades accumulate (VWAP). Opposite-side trades reduce and realize PnL."""
        fee = self._trade_fee(ticker, qty, price, is_maker = is_maker)
        self.fees_paid += fee
        self.fees_by_ticker[ticker] = self.fees_by_ticker.get(ticker, 0.0) + fee
        pos = self.positions.get(ticker)

        if pos is None:
            self.positions[ticker] = Position(ticker = ticker, side = side, qty = qty, avg_price = price)
            return

        if pos.side == side:
            # Same side — accumulate with VWAP
            total_qty = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + price * qty) / total_qty
            pos.qty = total_qty
        else:
            # Opposite side — reduce position, realize PnL
            close_qty = min(pos.qty, qty)
            direction = self._direction(pos.side)
            realized = close_qty * (price - pos.avg_price) * direction
            self.realized_pnl += realized
            self.realized_by_ticker[ticker] = self.realized_by_ticker.get(ticker, 0.0) + realized

            remaining = pos.qty - close_qty
            if is_pos(remaining):
                pos.qty = remaining
            else:
                del self.positions[ticker]

            # If incoming trade is larger, open new position on the other side
            leftover = qty - close_qty
            if is_pos(leftover):
                self.positions[ticker] = Position(ticker = ticker, side = side, qty = leftover, avg_price = price)

    def resolve(self, ticker: str, outcome: float):
        """Settle a market at outcome (0.0 or 1.0). Realizes PnL and removes position."""
        pos = self.positions.get(ticker)
        if pos is None:
            return
        direction = self._direction(pos.side)
        realized = pos.qty * (outcome - pos.avg_price) * direction
        self.realized_pnl += realized
        self.realized_by_ticker[ticker] = self.realized_by_ticker.get(ticker, 0.0) + realized
        del self.positions[ticker]

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """Unrealized PnL using provided prices (LTP, mid, etc.)."""
        unrealized = 0.0
        for ticker, pos in self.positions.items():
            if ticker in prices:
                direction = self._direction(pos.side)
                unrealized += pos.qty * (prices[ticker] - pos.avg_price) * direction
        return unrealized

    def total_pnl(self, prices: dict[str, float] | None = None) -> float:
        """Realized + unrealized PnL."""
        unrealized = self.mark_to_market(prices) if prices else 0.0
        return self.realized_pnl + unrealized

    def net_total_pnl(self, prices: dict[str, float] | None = None) -> float:
        """Realized + unrealized - fees."""
        return self.total_pnl(prices = prices) - self.fees_paid

    def summary(self) -> str:
        """Human-readable summary of current state."""
        lines = [f"Realized PnL: ${self.realized_pnl:.4f}"]
        if self.charge_fees:
            lines.append(f"Fees Paid: ${self.fees_paid:.4f}")
            lines.append(f"Net Realized PnL: ${self.realized_pnl - self.fees_paid:.4f}")
        if self.positions:
            lines.append(f"Open positions ({len(self.positions)}):")
            for ticker, pos in sorted(self.positions.items()):
                lines.append(f"  {ticker}: {pos.side} {pos.qty:.0f} @ ${pos.avg_price:.4f}")
        else:
            lines.append("No open positions")
        return "\n".join(lines)

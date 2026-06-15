"""
TradeFillMA: Exponential moving average of signed aggressor fill quantities.

Single-market mode:
  Signal = EMA(yes_aggressor_qty) - EMA(no_aggressor_qty)

Pair mode for "A vs B" winner markets:
  Positive → recent aggressive flow favoring the first-listed team A
  Negative → recent aggressive flow favoring the second team B

In pair mode, A YES and B NO are treated as the same directional flow, while
A NO and B YES are treated as the mirrored opposite.
"""

import math
import time
from typing import Optional

from .base import Alpha


HALF_LIVES_TIME = {
    "1s": 1,
    "10s": 10,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
}


class TradeFillMA(Alpha):
    """
    Directional signal from exponentially smoothed aggressor fill quantities.

    Tracks multiple half-lives in a single instance. Each half-life maintains
    one signed EMA. Pair mode uses the first ticker as the positive direction.
    """

    def __init__(
        self,
        ticker: str | None = None,
        *,
        pair_tickers: tuple[str, str] | None = None,
        half_life_fills: dict[str, int] | None = None,
        half_life_seconds: dict[str, float] | None = None,
        time_source: str = "exchange",
    ):
        """
        Provide exactly one of ticker or pair_tickers, plus one or both half-life configs.
        """
        if (ticker is None) == (pair_tickers is None):
            raise ValueError("Provide exactly one of ticker or pair_tickers")

        alpha_id = ticker if ticker is not None else f"{pair_tickers[0]}__{pair_tickers[1]}"
        super().__init__(alpha_id)
        self.time_source = time_source
        self.single_ticker = ticker
        self.pair_tickers = pair_tickers

        self._count_hls: dict[str, float] = {}
        self._time_hls: dict[str, float] = {}

        # Count-based EMA state: decay constant per fill
        if half_life_fills:
            for label, hl in half_life_fills.items():
                self._count_hls[label] = math.log(2) / hl

        # Time-based EMA state: decay constant per second
        if half_life_seconds:
            for label, hl in half_life_seconds.items():
                self._time_hls[label] = math.log(2) / hl

        if not self._count_hls and not self._time_hls:
            raise ValueError("Provide at least one of half_life_fills or half_life_seconds")

        # EMA state per label (raw signed qty + price-weighted signed qty)
        self._ema: dict[str, float] = {}
        self._ema_pw: dict[str, float] = {}
        all_labels = list(self._count_hls.keys()) + list(self._time_hls.keys())
        for label in all_labels:
            self._ema[label] = 0.0
            self._ema_pw[label] = 0.0

        self._last_time: float | None = None
        self._last_trade_wall_time: float | None = None
        self._n_fills = 0

    @property
    def labels(self) -> list[str]:
        return list(self._ema.keys())

    @property
    def name(self) -> str:
        return f"tfma_{self.time_source[0]}"

    @property
    def channels(self) -> list[str]:
        return ["trade"]

    def _get_time(self, msg: dict | None = None) -> float:
        """Get current time from the configured source."""
        if self.time_source == "exchange":
            if msg is not None:
                ts = msg.get("ts")
                if ts is not None:
                    return float(ts)
            # No msg available — use wall clock (same domain as unix epoch)
            return time.time()
        return time.monotonic()

    def _signed_qty(self, msg: dict) -> float | None:
        ticker = msg["market_ticker"]
        side = msg["taker_side"]
        qty = float(msg["count_fp"])

        if self.single_ticker is not None:
            if ticker != self.single_ticker:
                return None
            return qty if side == "yes" else -qty

        first_ticker, second_ticker = self.pair_tickers
        if ticker == first_ticker:
            return qty if side == "yes" else -qty
        if ticker == second_ticker:
            return qty if side == "no" else -qty
        return None

    def _trade_price(self, msg: dict) -> float:
        """Aggressor-side fill price in dollars. Derives from the complement if one side's key is absent."""
        side = msg["taker_side"]
        if side == "yes":
            if "yes_price_dollars" in msg:
                return float(msg["yes_price_dollars"])
            return 1.0 - float(msg["no_price_dollars"])
        if "no_price_dollars" in msg:
            return float(msg["no_price_dollars"])
        return 1.0 - float(msg["yes_price_dollars"])

    def on_message(self, channel: str, msg: dict):
        if channel != "trade":
            return

        signed_qty = self._signed_qty(msg)
        if signed_qty is None:
            return

        self._n_fills += 1
        weighted_qty = signed_qty * self._trade_price(msg)

        # Time-based decay
        now = self._get_time(msg)
        if self._last_time is not None:
            dt = max(now - self._last_time, 0.0)
            for label, decay in self._time_hls.items():
                factor = math.exp(-decay * dt)
                self._ema[label] *= factor
                self._ema_pw[label] *= factor
        self._last_time = now

        # Count-based decay (once per fill)
        for label, decay in self._count_hls.items():
            factor = math.exp(-decay)
            self._ema[label] *= factor
            self._ema_pw[label] *= factor

        self._last_trade_wall_time = time.time()

        for label in self._ema:
            self._ema[label] += signed_qty
            self._ema_pw[label] += weighted_qty

    def values(self, now: float | None = None) -> dict[str, float | None]:
        """Current signal value per half-life label. None if no fills yet. Read-only.

        now: optional timestamp in the same domain as the message times
        (exchange epoch when time_source == "exchange"). Used for replay,
        where wall-clock staleness would be wrong.
        """
        if self._n_fills == 0:
            return {label: None for label in self._ema}

        # Compute time-decay factor without mutating state
        # Always use wall-clock time for staleness decay (avoids domain mismatch)
        if now is not None and self._last_time is not None:
            dt = max(now - self._last_time, 0.0)
        elif self._time_hls and self._last_trade_wall_time is not None:
            dt = max(time.time() - self._last_trade_wall_time, 0.0)
        else:
            dt = 0.0

        result = {}
        for label in self._ema:
            value = self._ema[label]
            if label in self._time_hls and dt > 0:
                factor = math.exp(-self._time_hls[label] * dt)
                value *= factor
            result[label] = value
        return result

    def values_pw(self, now: float | None = None) -> dict[str, float | None]:
        """Price-weighted signed-qty EMA per half-life label. None if no fills yet."""
        if self._n_fills == 0:
            return {label: None for label in self._ema_pw}

        if now is not None and self._last_time is not None:
            dt = max(now - self._last_time, 0.0)
        elif self._time_hls and self._last_trade_wall_time is not None:
            dt = max(time.time() - self._last_trade_wall_time, 0.0)
        else:
            dt = 0.0

        result = {}
        for label in self._ema_pw:
            value = self._ema_pw[label]
            if label in self._time_hls and dt > 0:
                factor = math.exp(-self._time_hls[label] * dt)
                value *= factor
            result[label] = value
        return result

    @property
    def value(self) -> Optional[float]:
        """Returns first half-life's signal value (for Alpha interface compat)."""
        vals = self.values()
        if not vals:
            return None
        first = next(iter(vals.values()))
        return first

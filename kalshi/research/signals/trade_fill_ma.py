"""
TradeFillMA: Exponential moving average of signed aggressor fill quantities.

Signal = EMA(yes_aggressor_qty) - EMA(no_aggressor_qty)

Positive → recent buying pressure on yes side (bullish).
Negative → recent buying pressure on no side (bearish / yes-side selling).

Supports two decay modes:
  - Count-based: decay per fill with `half_life_fills` (e.g. half_life_fills=50
    means each new fill decays prior fills by factor 2^(-1/50)).
  - Time-based: decay per second with `half_life_s` seconds.
    Time source can be "exchange" (trade msg ts, unix epoch) or "local" (monotonic clock).

A single instance tracks all configured half-lives simultaneously and returns
a dict of signal values keyed by label.

Aggressor direction is inferred from the trade's `taker_side` field
in the Kalshi `trade` WebSocket channel. Trade ts is unix epoch (int seconds).
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
    its own yes/no EMA pair. Signal per half-life = ema_yes - ema_no.
    """

    def __init__(
        self,
        ticker: str,
        *,
        half_life_fills: dict[str, int] | None = None,
        half_life_seconds: dict[str, float] | None = None,
        time_source: str = "exchange",
    ):
        """
        Provide one or both of half_life_fills and half_life_seconds.

        half_life_fills: {"w50": 50, "w100": 100} — count-based EMA, decay per fill.
        half_life_seconds: {"1s": 1, "1m": 60, ...} — time-based EMA, decay per second.
        time_source: "exchange" uses trade msg unix epoch, "local" uses monotonic clock.
                     Only relevant for time-based half-lives.
        """
        super().__init__(ticker)
        self.time_source = time_source

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

        # EMA state per label
        self._yes_ema: dict[str, float] = {}
        self._no_ema: dict[str, float] = {}
        all_labels = list(self._count_hls.keys()) + list(self._time_hls.keys())
        for label in all_labels:
            self._yes_ema[label] = 0.0
            self._no_ema[label] = 0.0

        self._last_time: float | None = None
        self._n_fills = 0

    @property
    def labels(self) -> list[str]:
        return list(self._yes_ema.keys())

    @property
    def name(self) -> str:
        return f"tfma_{self.time_source[0]}"

    @property
    def channels(self) -> list[str]:
        return ["trade"]

    def _get_time(self, msg: dict | None = None) -> float:
        """Get current time from the configured source."""
        if self.time_source == "exchange" and msg is not None:
            ts = msg.get("ts")
            if ts is not None:
                return float(ts)
        return time.monotonic()

    def on_message(self, channel: str, msg: dict):
        if channel != "trade":
            return
        if msg["market_ticker"] != self.ticker:
            return

        side = msg["taker_side"]
        qty = float(msg["count_fp"])
        self._n_fills += 1

        # Time-based decay
        now = self._get_time(msg)
        if self._last_time is not None:
            dt = max(now - self._last_time, 0.0)
            for label, decay in self._time_hls.items():
                factor = math.exp(-decay * dt)
                self._yes_ema[label] *= factor
                self._no_ema[label] *= factor
        self._last_time = now

        # Count-based decay (once per fill)
        for label, decay in self._count_hls.items():
            factor = math.exp(-decay)
            self._yes_ema[label] *= factor
            self._no_ema[label] *= factor

        # Add new fill
        if side == "yes":
            for label in self._yes_ema:
                self._yes_ema[label] += qty
        else:
            for label in self._no_ema:
                self._no_ema[label] += qty

    def values(self) -> dict[str, float | None]:
        """Current signal value per half-life label. None if no fills yet."""
        if self._n_fills == 0:
            return {label: None for label in self._yes_ema}

        # Decay time-based EMAs to current time before reading
        now = self._get_time()
        if self._last_time is not None:
            dt = max(now - self._last_time, 0.0)
            for label, decay in self._time_hls.items():
                factor = math.exp(-decay * dt)
                self._yes_ema[label] *= factor
                self._no_ema[label] *= factor
        self._last_time = now

        result = {}
        for label in self._yes_ema:
            result[label] = self._yes_ema[label] - self._no_ema[label]
        return result

    @property
    def value(self) -> Optional[float]:
        """Returns first half-life's signal value (for Alpha interface compat)."""
        vals = self.values()
        if not vals:
            return None
        first = next(iter(vals.values()))
        return first

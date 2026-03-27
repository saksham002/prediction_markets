"""
Base class for directional trading signals (alphas).

Intentionally minimal — captures signal identity and output interface only.
Data subscription and connection management live outside alpha classes;
alphas are pure signal computers that receive updates and emit values.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SignalValue:
    """A single timestamped signal emission."""
    timestamp: datetime
    value: float
    ticker: str
    name: str


class Alpha(ABC):
    """
    Base class for a per-ticker directional signal.

    Subclasses must implement:
      - channels: which WS channels this alpha needs data from
      - on_message: process one message and update internal state
      - value: current signal value (None if not yet warmed up)
    """

    def __init__(self, ticker: str):
        self.ticker = ticker

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique signal name, e.g. 'trade_fill_ma'."""
        ...

    @property
    @abstractmethod
    def channels(self) -> list[str]:
        """WS channels this alpha consumes, e.g. ['trade']."""
        ...

    @abstractmethod
    def on_message(self, channel: str, msg: dict):
        """Ingest one WS message and update internal state."""
        ...

    @property
    @abstractmethod
    def value(self) -> Optional[float]:
        """Current signal value. None if insufficient data (warm-up)."""
        ...

    @property
    def warmed_up(self) -> bool:
        return self.value is not None

    def snapshot(self) -> Optional[SignalValue]:
        """Returns a SignalValue if warmed up, else None."""
        v = self.value
        if v is None:
            return None
        return SignalValue(
            timestamp = datetime.now(timezone.utc),
            value = v,
            ticker = self.ticker,
            name = self.name,
        )

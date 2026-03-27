"""
Timestamp conversion between unix epoch (seconds) and Eastern Time.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


class Timestamp:
    """
    Wraps a point in time. Constructed from either a unix epoch (seconds)
    or a datetime, and provides conversions to both representations.
    """

    def __init__(self, epoch: float):
        self._epoch = float(epoch)
        self._dt_utc = datetime.fromtimestamp(self._epoch, tz = UTC)
        self._dt_et = self._dt_utc.astimezone(ET)

    @classmethod
    def from_iso(cls, iso: str) -> "Timestamp":
        """Construct from an ISO-8601 timestamp string (e.g. Kalshi exchange ts)."""
        dt = datetime.fromisoformat(iso)
        return cls(dt.timestamp())

    @classmethod
    def from_et(cls, dt: datetime) -> "Timestamp":
        """Construct from a datetime in ET (naive datetimes are assumed ET)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo = ET)
        return cls(dt.timestamp())

    @classmethod
    def now(cls) -> "Timestamp":
        return cls(datetime.now(tz = UTC).timestamp())

    @property
    def epoch(self) -> float:
        return self._epoch

    @property
    def et(self) -> datetime:
        return self._dt_et

    @property
    def utc(self) -> datetime:
        return self._dt_utc

    def readable(self) -> str:
        """Human-readable ET string for logging: '2026-03-27 14:30:05.123 ET'."""
        return self._dt_et.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " ET"

    def __repr__(self) -> str:
        return f"Timestamp({self._dt_et.strftime('%Y-%m-%d %H:%M:%S %Z')}, epoch={self._epoch:.0f})"

    def __eq__(self, other) -> bool:
        if not isinstance(other, Timestamp):
            return NotImplemented
        return self._epoch == other._epoch

    def __lt__(self, other) -> bool:
        if not isinstance(other, Timestamp):
            return NotImplemented
        return self._epoch < other._epoch

    def __sub__(self, other) -> float:
        """Returns difference in seconds."""
        if not isinstance(other, Timestamp):
            return NotImplemented
        return self._epoch - other._epoch

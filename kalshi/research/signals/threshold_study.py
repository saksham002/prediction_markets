"""
Empirical study of threshold normalization strategies for TradeFillMA.

Single-pass design: reads the CSV ONCE and runs all heuristics in parallel.
Uses cheaper percentile approximation (sorted insertion via bisect).

Supports both single-market and pair-market signal modes:
  --mode single  reads tfma_* columns, groups by ticker
  --mode pair    reads pair_tfma_* columns, groups by event_ticker (first-position rows only)
"""

import argparse
import bisect
import csv
import math
import sys
from collections import defaultdict, deque

sys.path.insert(0, "/home/saksham3/projects/personal/prediction_markets/kalshi")
from src.pnl import PnL

ROLLING_WINDOW_S = 3600

def parse_readable_ts(s: str) -> float:
    """Cheap parser for '2026-03-30 12:40:45.739 ET'. Treats time as ET (UTC-4 in late March)."""
    # Format: YYYY-MM-DD HH:MM:SS.fff ET
    # We only need a monotonic seconds-since-epoch for windowing — exact tz doesn't matter.
    date_part, time_part = s[:10], s[11:23]
    h = int(time_part[0:2])
    m = int(time_part[3:5])
    sec = float(time_part[6:12])
    # Approximate epoch using day index (good enough for windowing)
    y, mo, d = int(date_part[0:4]), int(date_part[5:7]), int(date_part[8:10])
    # Use a fixed reference (2026-01-01) to avoid datetime overhead
    days = (mo - 1) * 31 + (d - 1)   # rough but monotonic within a day
    return days * 86400 + h * 3600 + m * 60 + sec


# --- Heuristic state per group key ---

class State:
    """Mutable rolling state for one group key, used by all heuristics."""
    __slots__ = ("times", "shorts", "abs_shorts_sorted", "sum_x", "sum_x2",
                 "sum_abs", "sum_abs_delta", "prev_short", "n")

    def __init__(self):
        self.times: deque = deque()
        self.shorts: deque = deque()                  # (ts, short) values
        self.abs_shorts_sorted: list[float] = []
        self.sum_x: float = 0.0
        self.sum_x2: float = 0.0
        self.sum_abs: float = 0.0                     # rolling sum |short| (volume proxy)
        self.sum_abs_delta: float = 0.0               # rolling sum |delta short| (imbalance denom)
        self.prev_short: float | None = None
        self.n: int = 0

    def update(self, ts: float, short: float):
        # Track delta
        if self.prev_short is not None:
            delta = abs(short - self.prev_short)
        else:
            delta = 0.0
        self.prev_short = short

        # Append new observation
        self.times.append((ts, short, delta))
        self.shorts.append(short)
        bisect.insort(self.abs_shorts_sorted, abs(short))
        self.sum_x += short
        self.sum_x2 += short * short
        self.sum_abs += abs(short)
        self.sum_abs_delta += delta
        self.n += 1

        # Evict expired
        cutoff = ts - ROLLING_WINDOW_S
        while self.times and self.times[0][0] < cutoff:
            _, old, old_delta = self.times.popleft()
            self.shorts.popleft()
            idx = bisect.bisect_left(self.abs_shorts_sorted, abs(old))
            self.abs_shorts_sorted.pop(idx)
            self.sum_x -= old
            self.sum_x2 -= old * old
            self.sum_abs -= abs(old)
            self.sum_abs_delta -= old_delta
            self.n -= 1

    def mean_abs(self) -> float | None:
        if self.n < 60:
            return None
        return self.sum_abs / self.n

    def sum_abs_delta_v(self) -> float | None:
        if self.n < 60:
            return None
        return self.sum_abs_delta

    def zscore(self, x: float) -> float | None:
        if self.n < 60:
            return None
        mean = self.sum_x / self.n
        var = max(self.sum_x2 / self.n - mean * mean, 0.0)
        std = math.sqrt(var)
        if std == 0:
            return None
        return (x - mean) / std

    def percentile(self, q: float) -> float | None:
        if self.n < 60:
            return None
        idx = int(q * (self.n - 1))
        return self.abs_shorts_sorted[idx]

    def max_abs(self) -> float | None:
        if self.n < 60:
            return None
        return self.abs_shorts_sorted[-1]


# --- Heuristic configurations ---

class Config:
    def __init__(self, name: str, kind: str, param: float):
        self.name = name
        self.kind = kind
        self.param = param
        self.pnl = PnL(charge_fees = True, fee_model = "kalshi")
        self.trades = 0
        self.wins = 0
        self.hold_secs: list[float] = []
        self.position: dict[str, dict] = {}

    def signal_action(self, state: State, short: float, medium: float) -> str | None:
        if self.kind == "fixed":
            T = self.param
            if short > T and medium > 0:
                return "long"
            if short < -T and medium < 0:
                return "short"
            return None
        if self.kind == "zscore":
            z = state.zscore(short)
            if z is None:
                return None
            k = self.param
            if z > k and medium > 0:
                return "long"
            if z < -k and medium < 0:
                return "short"
            return None
        if self.kind == "percentile":
            p = state.percentile(self.param)
            if p is None:
                return None
            if abs(short) < p:
                return None
            if short > 0 and medium > 0:
                return "long"
            if short < 0 and medium < 0:
                return "short"
            return None
        if self.kind == "frac_max":
            mx = state.max_abs()
            if mx is None or mx == 0:
                return None
            if abs(short) < self.param * mx:
                return None
            if short > 0 and medium > 0:
                return "long"
            if short < 0 and medium < 0:
                return "short"
            return None
        if self.kind == "vol_norm":
            mean_abs = state.mean_abs()
            if mean_abs is None or mean_abs == 0:
                return None
            ratio = abs(short) / mean_abs
            if ratio < self.param:
                return None
            if short > 0 and medium > 0:
                return "long"
            if short < 0 and medium < 0:
                return "short"
            return None
        if self.kind == "delta_norm":
            denom = state.sum_abs_delta_v()
            if denom is None or denom == 0:
                return None
            ratio = abs(short) / denom
            if ratio < self.param:
                return None
            if short > 0 and medium > 0:
                return "long"
            if short < 0 and medium < 0:
                return "short"
            return None
        return None


# --- Single-pass driver ---

def run_all(csv_path: str, mode: str, short_hl: str, medium_hl: str, qty: float):
    configs = [
        Config("fixed_T=50",     "fixed",      50),
        Config("fixed_T=200",    "fixed",     200),
        Config("fixed_T=1000",   "fixed",    1000),
        Config("zscore_k=1.5",   "zscore",    1.5),
        Config("zscore_k=2.0",   "zscore",    2.0),
        Config("zscore_k=3.0",   "zscore",    3.0),
        Config("pct_q=0.80",     "percentile", 0.80),
        Config("pct_q=0.90",     "percentile", 0.90),
        Config("pct_q=0.95",     "percentile", 0.95),
        Config("frac_max=0.50",  "frac_max",  0.50),
        Config("frac_max=0.75",  "frac_max",  0.75),
        Config("frac_max=0.90",  "frac_max",  0.90),
        Config("vol_norm=2",     "vol_norm",  2.0),
        Config("vol_norm=3",     "vol_norm",  3.0),
        Config("vol_norm=5",     "vol_norm",  5.0),
        Config("vol_norm=10",    "vol_norm", 10.0),
        Config("delta_norm=0.05","delta_norm", 0.05),
        Config("delta_norm=0.10","delta_norm", 0.10),
        Config("delta_norm=0.20","delta_norm", 0.20),
    ]

    if mode == "pair":
        short_col = f"pair_{short_hl}"
        medium_col = f"pair_{medium_hl}"
    else:
        short_col = short_hl
        medium_col = medium_hl

    states: dict[str, State] = defaultdict(State)
    last_prices: dict[str, tuple[float, float]] = {}
    last_ts: float = 0.0

    rows_processed = 0
    with open(csv_path) as f:
        r = csv.DictReader(f)

        if mode == "pair":
            if "pair_position" not in r.fieldnames or short_col not in r.fieldnames:
                print(f"Error: CSV lacks pair columns ({short_col}, pair_position). "
                      "Run signal_logger with pair mode first.", file = sys.stderr)
                sys.exit(1)

        for row in r:
            if mode == "pair" and row["pair_position"] != "first":
                continue

            rows_processed += 1
            if rows_processed % 200000 == 0:
                print(f"  ... {rows_processed} rows", file = sys.stderr)

            if mode == "pair":
                group_key = row["event_ticker"]
            else:
                group_key = row["ticker"]

            yb_s = row["yes_bid"]
            nb_s = row["no_bid"]
            if not yb_s or not nb_s:
                continue
            yes_bid = float(yb_s)
            no_bid = float(nb_s)
            last_prices[group_key] = (yes_bid, no_bid)

            short_s = row[short_col]
            medium_s = row[medium_col]
            if not short_s or not medium_s:
                continue
            short_sig = float(short_s)
            medium_sig = float(medium_s)
            ts = parse_readable_ts(row["local_ts"])
            last_ts = ts

            state = states[group_key]
            state.update(ts, short_sig)

            for cfg in configs:
                cur = cfg.position.get(group_key)
                if cur is None:
                    action = cfg.signal_action(state, short_sig, medium_sig)
                    if action == "long":
                        price = 1.0 - no_bid
                        cfg.pnl.trade(group_key, "long", qty, price)
                        cfg.position[group_key] = {"direction": "long", "entry_ts": ts, "entry_price": price}
                    elif action == "short":
                        price = yes_bid
                        cfg.pnl.trade(group_key, "short", qty, price)
                        cfg.position[group_key] = {"direction": "short", "entry_ts": ts, "entry_price": price}
                else:
                    exit_signal = (
                        (cur["direction"] == "long" and short_sig < 0) or
                        (cur["direction"] == "short" and short_sig > 0)
                    )
                    if exit_signal:
                        if cur["direction"] == "long":
                            price = yes_bid
                            cfg.pnl.trade(group_key, "short", qty, price)
                            delta = price - cur["entry_price"]
                        else:
                            price = 1.0 - no_bid
                            cfg.pnl.trade(group_key, "long", qty, price)
                            delta = cur["entry_price"] - price
                        if delta > 0:
                            cfg.wins += 1
                        cfg.trades += 1
                        held = ts - cur["entry_ts"]
                        cfg.hold_secs.append(held)
                        del cfg.position[group_key]

    # Force-flush remaining positions at last seen prices
    for cfg in configs:
        for group_key, cur in list(cfg.position.items()):
            if group_key not in last_prices:
                continue
            yes_bid, no_bid = last_prices[group_key]
            if cur["direction"] == "long":
                price = yes_bid
                cfg.pnl.trade(group_key, "short", qty, price)
                delta = price - cur["entry_price"]
            else:
                price = 1.0 - no_bid
                cfg.pnl.trade(group_key, "long", qty, price)
                delta = cur["entry_price"] - price
            if delta > 0:
                cfg.wins += 1
            cfg.trades += 1
            held = last_ts - cur["entry_ts"]
            cfg.hold_secs.append(held)

    # Report
    print(f"\nMode: {mode} | Short: {short_col} | Medium: {medium_col} | Qty: {qty}")
    print(f"{'heuristic':18s} {'realized':>12s} {'fees':>10s} {'net':>12s} {'trades':>8s} {'win%':>7s} {'avg_hold':>10s}")
    print("-" * 90)
    rows_out = []
    for cfg in configs:
        net = cfg.pnl.realized_pnl - cfg.pnl.fees_paid
        win_rate = cfg.wins / cfg.trades if cfg.trades > 0 else 0.0
        avg_hold = sum(cfg.hold_secs) / len(cfg.hold_secs) if cfg.hold_secs else 0.0
        rows_out.append((cfg.name, cfg.pnl.realized_pnl, cfg.pnl.fees_paid, net, cfg.trades, win_rate, avg_hold))

    rows_out.sort(key = lambda r: r[3], reverse = True)
    for name, gross, fees, net, trades, wr, hold in rows_out:
        print(f"{name:18s} {gross:12.2f} {fees:10.2f} {net:12.2f} {trades:8d} {wr * 100:6.1f}% {hold:9.0f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description = "Threshold normalization study for TradeFillMA")
    parser.add_argument("csv", help = "Path to signal log CSV")
    parser.add_argument("--mode", choices = ["single", "pair"], default = "single", help = "Signal mode")
    parser.add_argument("--short-hl", default = "tfma_1m_e", help = "Short half-life column name")
    parser.add_argument("--medium-hl", default = "tfma_5m_e", help = "Medium half-life column name")
    parser.add_argument("--qty", type = float, default = 100, help = "Contracts per trade")
    args = parser.parse_args()
    run_all(args.csv, args.mode, args.short_hl, args.medium_hl, args.qty)

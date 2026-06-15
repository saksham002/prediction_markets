"""
Correlation between alpha[t] and yes_bid[t+delta] - yes_bid[t]
for delta in {1m, 5m, 10m}, across all single-market and pair TFMA columns.

Single-market alphas: pooled across all rows (signal is per-ticker, signed to
own yes_bid direction).
Pair alphas: only first-leg rows used. Pair signal is signed by first ticker,
so corr(pair_alpha, delta first-leg yes_bid) is the sign-correct comparison.
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr

CSV_PATH = Path("/data/user_data/saksham3/kalshi/signal_logs/signal_log_20260428_013628.csv")
DELTAS_S = [60, 120, 300, 900]
DELTA_LABELS = {60: "1m", 120: "2m", 300: "5m", 900: "15m"}
MATCH_TOL_S = 30  # accept future row in [t+delta, t+delta+tol]

SINGLE_HL_LABELS = ["1s", "10s", "1m", "5m", "15m", "30m", "1h"]
SINGLE_COLS = (
    [f"tfma_{hl}_{src}" for hl in SINGLE_HL_LABELS for src in ("e", "l")]
    + [f"tfma_pw_{hl}_{src}" for hl in SINGLE_HL_LABELS for src in ("e", "l")]
)
PAIR_COLS = (
    [f"pair_tfma_{hl}_{src}" for hl in SINGLE_HL_LABELS for src in ("e", "l")]
    + [f"pair_tfma_pw_{hl}_{src}" for hl in SINGLE_HL_LABELS for src in ("e", "l")]
)


def parse_ts(s: str) -> float:
    return datetime.strptime(s.replace(" ET", ""), "%Y-%m-%d %H:%M:%S.%f").timestamp()


def parse_float(s: str):
    return float(s) if s else np.nan


def load_rows(path: Path):
    """Group rows by ticker. Each group: ts (s), yes_bid, alpha_cols dict."""
    by_ticker: dict[str, list] = defaultdict(list)
    with open(path, newline = "") as f:
        reader = csv.DictReader(f)
        for row in reader:
            by_ticker[row["ticker"]].append(row)
    return by_ticker


def build_arrays(rows: list[dict], cols: list[str]):
    """Return ts (sorted), yes_bid, dict[col -> array]."""
    rows = sorted(rows, key = lambda r: parse_ts(r["local_ts"]))
    ts = np.array([parse_ts(r["local_ts"]) for r in rows])
    yes_bid = np.array([parse_float(r["yes_bid"]) for r in rows])
    alphas = {c: np.array([parse_float(r[c]) for r in rows]) for c in cols}
    return ts, yes_bid, alphas


def future_match_indices(ts: np.ndarray, delta: float) -> np.ndarray:
    """For each i, return smallest j >= i with ts[j] in [ts[i]+delta, ts[i]+delta+tol].
    Returns -1 where no valid match exists."""
    n = len(ts)
    target_lo = ts + delta
    target_hi = ts + delta + MATCH_TOL_S
    j_lo = np.searchsorted(ts, target_lo, side = "left")
    out = np.full(n, -1, dtype = np.int64)
    valid = (j_lo < n) & (ts[np.clip(j_lo, 0, n - 1)] <= target_hi)
    out[valid] = j_lo[valid]
    return out


def collect_pairs(
    by_ticker: dict[str, list],
    cols: list[str],
    delta: int,
    pair_position_filter: str | None = None,
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """For each alpha col, return (alpha_t, dprice) pooled across tickers."""
    samples: dict[str, list[tuple[float, float]]] = {c: [] for c in cols}
    for ticker, rows in by_ticker.items():
        if pair_position_filter is not None:
            rows = [r for r in rows if r["pair_position"] == pair_position_filter]
        if len(rows) < 2:
            continue
        ts, yes_bid, alphas = build_arrays(rows, cols)
        j = future_match_indices(ts, delta)
        for c in cols:
            a = alphas[c]
            for i in range(len(ts)):
                if j[i] < 0:
                    continue
                dp = yes_bid[j[i]] - yes_bid[i]
                if np.isnan(dp) or np.isnan(a[i]):
                    continue
                samples[c].append((a[i], dp))
    out = {}
    for c, lst in samples.items():
        if len(lst) < 30:
            out[c] = (np.array([]), np.array([]))
        else:
            arr = np.array(lst)
            out[c] = (arr[:, 0], arr[:, 1])
    return out


def pearson(x: np.ndarray, y: np.ndarray) -> tuple[float, float, int]:
    if len(x) < 30:
        return float("nan"), float("nan"), len(x)
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0:
        return float("nan"), float("nan"), len(x)
    res = pearsonr(x, y)
    return float(res.statistic), float(res.pvalue), len(x)


def main():
    print(f"Loading {CSV_PATH}...")
    by_ticker = load_rows(CSV_PATH)
    print(f"  {len(by_ticker)} tickers, {sum(len(v) for v in by_ticker.values())} total rows\n")

    results = []  # (family, col, delta_label, hl, src, r, p, n)
    for delta in DELTAS_S:
        dlabel = DELTA_LABELS[delta]

        single = collect_pairs(by_ticker, SINGLE_COLS, delta, pair_position_filter = None)
        for c, (a, dp) in single.items():
            r, p, n = pearson(a, dp)
            parts = c.split("_")
            hl, src = parts[-2], parts[-1]
            weighting = "pw" if "pw" in parts else "raw"
            family = f"single-{weighting}"
            results.append((family, c, dlabel, hl, src, r, p, n))

        pair = collect_pairs(by_ticker, PAIR_COLS, delta, pair_position_filter = "first")
        for c, (a, dp) in pair.items():
            r, p, n = pearson(a, dp)
            parts = c.split("_")
            hl, src = parts[-2], parts[-1]
            weighting = "pw" if "pw" in parts else "raw"
            family = f"pair-{weighting}"
            results.append((family, c, dlabel, hl, src, r, p, n))

    print("All correlations (signed):")
    print(f"  {'family':<10}  {'col':<25}  {'delta':<5}  {'hl':<4}  {'src':<3}  {'r':>8}  {'p':>10}  {'n':>6}")
    for fam, col, dl, hl, src, r, p, n in sorted(results, key = lambda x: (x[0], x[2], x[1])):
        print(f"  {fam:<10}  {col:<25}  {dl:<5}  {hl:<4}  {src:<3}  {r:>8.4f}  {p:>10.4g}  {n:>6}")

    print("\nTop 25 by |r| (with p < 0.05 marked *):")
    print(f"  {'family':<10}  {'col':<25}  {'delta':<5}  {'r':>8}  {'p':>10}  {'n':>6}")
    valid = [x for x in results if not np.isnan(x[5])]
    for fam, col, dl, hl, src, r, p, n in sorted(valid, key = lambda x: -abs(x[5]))[:25]:
        sig = " *" if p < 0.05 else "  "
        print(f"  {fam:<10}  {col:<25}  {dl:<5}  {r:>8.4f}  {p:>10.4g}{sig}  {n:>6}")


if __name__ == "__main__":
    main()

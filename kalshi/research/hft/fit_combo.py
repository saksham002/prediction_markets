"""
Fit a linear combination of alphas to forward pair-mid returns via ridge
regression on tick_study samples.csv files.

Time-ordered split (first 70% train / last 30% test) guards against
look-ahead. Features are standardized with TRAIN means/stds; those constants
ship with the weights so live/replay engines reproduce the same combo.

Usage:
  fit_combo.py samples1.csv [samples2.csv ...] --horizon 60 --ridge 10 \
      --out /data/user_data/saksham3/kalshi_hft/studies/combo_60s.json
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import PairAlphaEngine


def load_samples(paths: list[str]):
    """Per event: ts, mid, alpha matrix (NaN for missing)."""
    alpha_names = PairAlphaEngine.alpha_names()
    by_event = defaultdict(lambda: {"ts": [], "mid": [], "alphas": []})
    for path in paths:
        with open(path, newline = "") as f:
            for row in csv.DictReader(f):
                g = by_event[row["event_ticker"]]
                g["ts"].append(float(row["lts"]))
                g["mid"].append(float(row["pair_mid"]))
                g["alphas"].append([
                    float(row[c]) if row[c] != "" else np.nan for c in alpha_names
                ])
    return alpha_names, by_event


def build_xy(by_event, horizon_s: float):
    xs, ys, ts_all = [], [], []
    for event, g in by_event.items():
        ts = np.array(g["ts"])
        mid = np.array(g["mid"])
        A = np.array(g["alphas"])
        order = np.argsort(ts)
        ts, mid, A = ts[order], mid[order], A[order]
        targets = ts + horizon_s
        idx = np.searchsorted(ts, targets, side = "right") - 1
        valid = (idx >= 0) & (targets <= ts[-1]) & ~np.isnan(A).any(axis = 1)
        fwd = np.full(len(ts), np.nan)
        fwd[valid] = (mid[idx[valid]] - mid[valid]) * 100.0
        keep = valid & ~np.isnan(fwd)
        xs.append(A[keep])
        ys.append(fwd[keep])
        ts_all.append(ts[keep])
    X = np.concatenate(xs)
    y = np.concatenate(ys)
    t = np.concatenate(ts_all)
    order = np.argsort(t)
    return X[order], y[order]


def main():
    parser = argparse.ArgumentParser(description = "Fit ridge combo of alphas to forward returns")
    parser.add_argument("samples", nargs = "+", help = "samples.csv paths from tick_study")
    parser.add_argument("--horizon", type = float, default = 60)
    parser.add_argument("--ridge", type = float, default = 10.0)
    parser.add_argument("--out", type = str, required = True)
    args = parser.parse_args()

    alpha_names, by_event = load_samples(args.samples)
    X, y = build_xy(by_event, args.horizon)
    n = len(y)
    print(f"{n} samples, {len(alpha_names)} features, horizon {args.horizon}s")
    if n < 500:
        print("WARNING: few samples; weights will be noisy")

    split = int(n * 0.7)
    X_tr, y_tr = X[:split], y[:split]
    X_te, y_te = X[split:], y[split:]

    means = X_tr.mean(axis = 0)
    stds = X_tr.std(axis = 0)
    stds[stds == 0] = 1.0
    Z_tr = (X_tr - means) / stds
    Z_te = (X_te - means) / stds

    lam = args.ridge
    w = np.linalg.solve(Z_tr.T @ Z_tr + lam * np.eye(Z_tr.shape[1]), Z_tr.T @ y_tr)

    def perf(Z, yy, label):
        pred = Z @ w
        if pred.std() == 0 or yy.std() == 0:
            print(f"  {label}: degenerate")
            return
        r = float(np.corrcoef(pred, yy)[0, 1])
        print(f"  {label}: corr(pred, fwd) = {r:.4f}  (n={len(yy)})")
        return r

    print("Fit quality:")
    perf(Z_tr, y_tr, "train")
    r_test = perf(Z_te, y_te, "test ")

    print("\nWeights (standardized):")
    for name, wi in sorted(zip(alpha_names, w), key = lambda kv: -abs(kv[1])):
        print(f"  {name:<16} {wi:+.4f}")

    out = {
        "horizon_s": args.horizon,
        "ridge": lam,
        "n_train": int(split),
        "n_test": int(n - split),
        "test_corr": r_test,
        "weights": {name: float(wi) for name, wi in zip(alpha_names, w)},
        "means": {name: float(m) for name, m in zip(alpha_names, means)},
        "stds": {name: float(s) for name, s in zip(alpha_names, stds)},
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents = True, exist_ok = True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent = 1)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()

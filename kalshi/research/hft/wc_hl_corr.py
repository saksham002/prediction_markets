"""Correlation of each tfma_pw half-life with the 120s forward return,
pooled over the WC-game markets in the dataset."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from lasso_pipeline import collect_samples
try:
    from research.hft.paths import DATASET
except ImportError:
    from paths import DATASET

HORIZON_S = 120
HLS = ["tfma_pw_1s", "tfma_pw_5s", "tfma_pw_10s", "tfma_pw_30s", "tfma_pw_60s", "tfma_pw_300s"]


def main():
    names, by_event = collect_samples(DATASET)
    col = {n: i for i, n in enumerate(names)}
    Xs, ys = [], []
    for event, g in by_event.items():
        if not event.split(":")[0].startswith("KXWCGAME"):
            continue
        ts = np.array(g["ts"])
        if len(ts) < 30:
            continue
        order = np.argsort(ts)
        ts = ts[order]
        mid = np.array(g["mid"])[order]
        A = np.array(g["a"])[order]
        idx = np.searchsorted(ts, ts + HORIZON_S, side = "right") - 1
        valid = (idx >= 0) & (ts + HORIZON_S <= ts[-1]) & ~np.isnan(A).any(axis = 1)
        Xs.append(A[valid])
        ys.append((mid[idx[valid]] - mid[valid]) * 100.0)
    X = np.concatenate(Xs)
    y = np.concatenate(ys)
    print(f"WC pool: n={len(y)}")
    best, best_r = None, 0.0
    for h in HLS:
        x = X[:, col[h]]
        r = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 else float("nan")
        mark = ""
        if abs(r) > abs(best_r):
            best, best_r = h, r
            mark = " <-"
        print(f"  {h:<16} r={r:+.4f}{mark}")
    print(f"\nbest: {best} (r={best_r:+.4f})")


if __name__ == "__main__":
    main()

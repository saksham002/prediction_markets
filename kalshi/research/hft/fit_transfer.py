"""Fit combo weights separately on the MLB pool and the WC pool, compare them,
and trade the WC games using the MLB-fitted weights (transfer test).

Usage: fit_transfer.py [--ticks-dir DIR] [--out-dir DIR]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from lasso_pipeline import collect_samples, lasso_cd
from tick_study import StudyConsumer  # noqa: F401 (imported via lasso_pipeline)

HORIZON_S = 180
FEATURES = ["tfma_pw_300s", "obi_ma_1s", "mom_30s", "obi"]
WC_FILES = [
    "ticks_20260611_074145_3157453.jsonl.gz",  # MEX-RSA
    "ticks_20260611_193554_1678643.jsonl.gz",  # KOR-CZE
]


def pool_xy(by_event, names, prefix):
    """X, y over all events whose game ticker starts with prefix."""
    col = {n: i for i, n in enumerate(names)}
    fidx = [col[f] for f in FEATURES]
    Xs, ys = [], []
    for event, g in by_event.items():
        if not event.split(":")[0].startswith(prefix):
            continue
        ts = np.array(g["ts"])
        if len(ts) < 30:
            continue
        order = np.argsort(ts)
        ts = ts[order]
        mid = np.array(g["mid"])[order]
        A = np.array(g["a"])[order][:, fidx]
        idx = np.searchsorted(ts, ts + HORIZON_S, side = "right") - 1
        valid = (idx >= 0) & (ts + HORIZON_S <= ts[-1]) & ~np.isnan(A).any(axis = 1)
        Xs.append(A[valid])
        ys.append((mid[idx[valid]] - mid[valid]) * 100.0)
    if not Xs:
        return np.empty((0, len(FEATURES))), np.empty(0)
    return np.concatenate(Xs), np.concatenate(ys)


def fit_pool(X, y, label):
    means = X.mean(axis = 0)
    stds = X.std(axis = 0)
    stds[stds == 0] = 1.0
    Z = (X - means) / stds
    w = lasso_cd(Z, y, 1e-4)  # ~OLS on standardized features
    r = float(np.corrcoef(Z @ w, y)[0, 1]) if (Z @ w).std() > 0 else float("nan")
    print(f"{label} pool: n={len(y)} in-sample corr={r:.3f}")
    for f, wi in zip(FEATURES, w):
        print(f"  {f:<16} {wi:+.4f}")
    return {
        "horizon_s": HORIZON_S,
        "weights": {f: float(wi) for f, wi in zip(FEATURES, w)},
        "means": {f: float(m) for f, m in zip(FEATURES, means)},
        "stds": {f: float(s) for f, s in zip(FEATURES, stds)},
    }, w


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks-dir", default = "/data/user_data/saksham3/kalshi_hft/dataset")
    parser.add_argument("--out-dir", default = "/data/user_data/saksham3/kalshi_hft/studies")
    args = parser.parse_args()

    names, by_event = collect_samples(Path(args.ticks_dir))

    combos, ws = {}, {}
    for label, prefix in (("MLB", "KXMLBGAME"), ("WC", "KXWCGAME")):
        X, y = pool_xy(by_event, names, prefix)
        combos[label], ws[label] = fit_pool(X, y, label)

    a, b = ws["MLB"], ws["WC"]
    cos = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    print(f"\ncosine similarity MLB vs WC weights: {cos:+.3f}")

    out_dir = Path(args.out_dir)
    for label, combo in combos.items():
        p = out_dir / f"transfer_{label.lower()}_combo.json"
        with open(p, "w") as f:
            json.dump(combo, f, indent = 1)
        print(f"wrote {p}")

    print("\n=== trade WC games with MLB-fitted weights ===")
    mm_sim = str(Path(__file__).parent / "mm_sim.py")
    mlb_combo = str(out_dir / "transfer_mlb_combo.json")
    for fname in WC_FILES:
        rec = str(Path(args.ticks_dir) / fname)
        for thr in (0.25, 0.5):
            print(f"\n--- {fname} thr={thr} ---")
            subprocess.run([
                sys.executable, mm_sim, rec, "-a", "combo",
                "--combo-file", mlb_combo, "-t", str(thr),
                "-s", "500", "-i", "1000", "--pair-risk", "--series", "KXWCGAME",
                "--tag", f"transfer_{fname.split('_')[1]}_t{thr}",
            ], check = False)


if __name__ == "__main__":
    main()

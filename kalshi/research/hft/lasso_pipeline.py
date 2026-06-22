"""
Lasso alpha-selection pipeline (June 2026 protocol):

  1. Sample all alphas (incl. every half-life variant) on the dataset
     recordings; label games fit/val/test:
       test = md5(event) % 5 == 0        (the standing 80-20 eval split)
       val  = md5("val" + event) % 5 == 0 among non-test games
       fit  = the rest
  2. Per alpha family with half-lives (tfma, tfma_pw, obi_ma, mom): pick the
     HL with max |pearson| vs 180s forward mid return on the FIT games.
  3. Lasso (coordinate descent) of 180s returns on [selected HLs + obi],
     standardized on fit; L1 penalty chosen to maximize correlation on VAL.
  4. Dump the fitted weights as a combo JSON for mm_sim/eval_buffer.

Threshold tuning / trading evaluation is run separately via eval_buffer.
"""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import PairAlphaEngine
from research.hft.replay import Replayer
from research.hft.tick_study import StudyConsumer
from research.hft.paths import DATASET, STUDIES

HORIZON_S = 180
# One family per signal CONCEPT: raw and price-weighted TFMA are
# near-collinear, so they compete within a single family and only one
# variant (best |corr| HL) enters the regression.
FAMILIES = {
    "tfma": ([f"tfma_{l}" for l in ("1s", "5s", "10s", "30s", "60s", "300s")]
             + [f"tfma_pw_{l}" for l in ("1s", "5s", "10s", "30s", "60s", "300s")]),
    "obi_ma": [f"obi_ma_{l}" for l in ("1s", "5s", "15s", "60s")],
    "mom": [f"mom_{l}" for l in ("5s", "30s", "120s")],
}
EXTRA_FEATURES = ["obi"]


def is_test(event: str) -> bool:
    # Sample keys for soccer are "EVENT:TICKER" — split assignment is BY GAME
    game = event.split(":")[0]
    return int(hashlib.md5(game.encode()).hexdigest(), 16) % 5 == 0


def is_val(event: str) -> bool:
    game = event.split(":")[0]
    return (not is_test(game)
            and int(hashlib.md5(("val" + game).encode()).hexdigest(), 16) % 5 == 0)


def collect_samples(ticks_dir: Path, throttle: float = 1.0):
    """Per event: ts, mid arrays + alpha matrix (alpha_names order)."""
    names = PairAlphaEngine.alpha_names()
    by_event = defaultdict(lambda: {"ts": [], "mid": [], "a": []})
    for rec in sorted(ticks_dir.glob("*.jsonl.gz")):
        replayer = Replayer(rec)
        consumer = StudyConsumer(replayer, throttle)
        replayer.run(consumer)
        for event, rows in consumer.samples.items():
            g = by_event[event]
            for lts, mid, _s1, _s2, alphas in rows:
                g["ts"].append(lts)
                g["mid"].append(mid)
                g["a"].append([np.nan if v is None else v for v in alphas])
        print(f"  sampled {rec.name}: {sum(len(r) for r in consumer.samples.values())} rows")
    return names, by_event


def build_xy(by_event, names, horizon):
    """Per split: X (n, n_features), y (cents)."""
    out = {"fit": ([], []), "val": ([], []), "test": ([], [])}
    for event, g in by_event.items():
        ts = np.array(g["ts"])
        if len(ts) < 30:
            continue
        order = np.argsort(ts)
        ts = ts[order]
        mid = np.array(g["mid"])[order]
        A = np.array(g["a"])[order]
        idx = np.searchsorted(ts, ts + horizon, side = "right") - 1
        valid = (idx >= 0) & (ts + horizon <= ts[-1]) & ~np.isnan(A).any(axis = 1)
        fwd = (mid[idx[valid]] - mid[valid]) * 100.0
        split = "test" if is_test(event) else ("val" if is_val(event) else "fit")
        out[split][0].append(A[valid])
        out[split][1].append(fwd)
    return {k: (np.concatenate(v[0]) if v[0] else np.empty((0, len(names))),
                np.concatenate(v[1]) if v[1] else np.empty(0)) for k, v in out.items()}


def lasso_cd(Z, y, lam, n_iter = 200):
    """Coordinate-descent lasso on standardized features (unit variance)."""
    n, p = Z.shape
    w = np.zeros(p)
    zy = Z.T @ y / n
    ZZ = Z.T @ Z / n
    for _ in range(n_iter):
        w_old = w.copy()
        for j in range(p):
            rho = zy[j] - ZZ[j] @ w + ZZ[j, j] * w[j]
            w[j] = np.sign(rho) * max(abs(rho) - lam, 0.0) / ZZ[j, j]
        if np.abs(w - w_old).max() < 1e-9:
            break
    return w


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks-dir", default = str(DATASET))
    parser.add_argument("--out", default = str(STUDIES / "lasso_combo.json"))
    args = parser.parse_args()

    names, by_event = collect_samples(Path(args.ticks_dir))
    n_fit = sum(1 for e in by_event if not is_test(e) and not is_val(e))
    n_val = sum(1 for e in by_event if is_val(e))
    n_test = sum(1 for e in by_event if is_test(e))
    print(f"games: fit={n_fit} val={n_val} test={n_test}")

    xy = build_xy(by_event, names, HORIZON_S)
    Xf, yf = xy["fit"]
    Xv, yv = xy["val"]
    print(f"samples: fit={len(yf)} val={len(yv)} test={len(xy['test'][1])}")

    # 1. Per-family best half-life by |corr| with 180s return on FIT
    col = {n: i for i, n in enumerate(names)}
    selected = []
    print(f"\nper-family HL selection (corr vs {HORIZON_S}s fwd return, fit set):")
    for family, members in FAMILIES.items():
        best, best_r = None, 0.0
        for m in members:
            x = Xf[:, col[m]]
            if x.std() == 0:
                continue
            r = float(np.corrcoef(x, yf)[0, 1])
            marker = ""
            if abs(r) > abs(best_r):
                best, best_r = m, r
                marker = " <-"
            print(f"  {m:<16} r={r:+.4f}{marker}")
        if best is not None:
            selected.append(best)
            print(f"  => {family}: {best} (r={best_r:+.4f})")
    features = selected + EXTRA_FEATURES
    print(f"\nlasso features: {features}")

    # 2. Lasso with L1 tuned on validation correlation
    fidx = [col[f] for f in features]
    means = Xf[:, fidx].mean(axis = 0)
    stds = Xf[:, fidx].std(axis = 0)
    stds[stds == 0] = 1.0
    Zf = (Xf[:, fidx] - means) / stds
    Zv = (Xv[:, fidx] - means) / stds

    lam_grid = np.geomspace(1e-4, 1.0, 13)
    best = None
    print(f"\n{'lambda':>9} {'val_corr':>9}  nonzero")
    for lam in lam_grid:
        w = lasso_cd(Zf, yf, lam)
        pred = Zv @ w
        r = float(np.corrcoef(pred, yv)[0, 1]) if pred.std() > 0 else float("nan")
        nz = [f for f, wi in zip(features, w) if abs(wi) > 1e-12]
        print(f"{lam:>9.4g} {r:>9.4f}  {len(nz)}: {nz}")
        if best is None or (r == r and r > best[1]):
            best = (lam, r, w)
    lam, val_r, w = best
    print(f"\nchosen lambda={lam:.4g} (val corr {val_r:.4f})")
    for f, wi in zip(features, w):
        print(f"  {f:<16} {wi:+.4f}")

    out = {
        "horizon_s": HORIZON_S,
        "lambda": float(lam),
        "val_corr": val_r,
        "weights": {f: float(wi) for f, wi in zip(features, w) if abs(wi) > 1e-12},
        "means": {f: float(m) for f, m in zip(features, means)},
        "stds": {f: float(s) for f, s in zip(features, stds)},
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent = 1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()

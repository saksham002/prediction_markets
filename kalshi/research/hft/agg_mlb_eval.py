"""Aggregation alpha on MLB: fit HL + thresholds on TRAIN games, trade, and
report train/test PnL vs tfma_pw and obi under the identical protocol.

1. Best agg_{hl} on train-MLB by corr with 120s+180s forward returns
   (also reports the price-weighted agg variant's correlations).
2. Threshold grid per alpha from train |alpha| percentiles {0, p50, p75, p90, p95}.
3. mm_sim replay per (alpha, threshold) over the dataset, s500/cap1000/budget1000;
   threshold chosen on TRAIN realized-net, decision metric is TEST.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from eval_buffer import run_one
from lasso_pipeline import collect_samples, is_test
try:
    from research.hft.paths import DATASET
except ImportError:
    from paths import DATASET
HORIZONS = (120.0, 180.0)
HLS = ["1s", "5s", "10s", "30s", "60s", "300s"]
PCTS = [50, 75, 90, 95]


def train_mlb_pool(by_event, names):
    """X (n, all alphas), y per horizon over TRAIN MLB games."""
    col = {n: i for i, n in enumerate(names)}
    Xs = []
    ys = {h: [] for h in HORIZONS}
    for event, g in by_event.items():
        if not event.startswith("KXMLBGAME") or is_test(event):
            continue
        ts = np.array(g["ts"])
        if len(ts) < 30:
            continue
        order = np.argsort(ts)
        ts = ts[order]
        mid = np.array(g["mid"])[order]
        A = np.array(g["a"], dtype = float)[order]
        keep = np.ones(len(ts), dtype = bool)
        for h in HORIZONS:
            idx = np.searchsorted(ts, ts + h, side = "right") - 1
            keep &= (idx >= 0) & (ts + h <= ts[-1])
        Xs.append(A[keep])
        for h in HORIZONS:
            idx = np.searchsorted(ts, ts + h, side = "right") - 1
            ys[h].append((mid[idx[keep]] - mid[keep]) * 100.0)
    return col, np.concatenate(Xs), {h: np.concatenate(v) for h, v in ys.items()}


def corr(x, y):
    m = ~np.isnan(x) & ~np.isnan(y)
    if m.sum() < 100 or x[m].std() == 0:
        return float("nan")
    return float(np.corrcoef(x[m], y[m])[0, 1])


def main():
    names, by_event = collect_samples(DATASET)
    col, X, ys = train_mlb_pool(by_event, names)
    print(f"\ntrain-MLB pool: n={len(X)}")

    print(f"\ncorr vs fwd return (TRAIN MLB):")
    print(f"{'alpha':>14} " + " ".join(f"{int(h)}s" for h in HORIZONS) + "   avg")
    table = {}
    for c in ([f"agg_{h}" for h in HLS] + [f"agg_pw_{h}" for h in HLS]
              + [f"tfma_pw_{h}" for h in HLS] + ["obi"]):
        rs = [corr(X[:, col[c]], ys[h]) for h in HORIZONS]
        avg = float(np.nanmean(rs))
        table[c] = avg
        print(f"{c:>14} " + " ".join(f"{r:+.4f}" for r in rs) + f" {avg:+.4f}")

    best_agg = max((c for c in table if c.startswith("agg_") and not c.startswith("agg_pw_")),
                   key = lambda c: abs(table[c]))
    best_tfma = max((c for c in table if c.startswith("tfma_pw_")), key = lambda c: abs(table[c]))
    best_agg_pw = max((c for c in table if c.startswith("agg_pw_")), key = lambda c: abs(table[c]))
    print(f"\nbest agg: {best_agg} ({table[best_agg]:+.4f}) | best agg_pw: {best_agg_pw} "
          f"({table[best_agg_pw]:+.4f}) | best tfma_pw: {best_tfma} ({table[best_tfma]:+.4f}) "
          f"| obi {table['obi']:+.4f}")

    recordings = [r for r in sorted(DATASET.glob("*.jsonl.gz")) if r.stat().st_size > 10000]
    print(f"\n=== trading eval over {len(recordings)} recordings, s500/cap1000/budget1000 ===")
    results = {}
    for alpha in (best_agg, best_tfma, "obi"):
        x = np.abs(X[:, col[alpha]])
        x = x[~np.isnan(x)]
        thresholds = [0.0] + [float(np.percentile(x, p)) for p in PCTS]
        results[alpha] = []
        for thr in thresholds:
            agg_row = {"train_realized_net": 0.0, "test_realized_net": 0.0,
                       "train_net": 0.0, "test_net": 0.0, "n_fills": 0, "fees": 0.0}
            cfg = {"alphas": [{"name": alpha, "threshold": thr}],
                   "per_order_size": 500, "inventory_cap": 1000, "budget": 1000}
            for rec in recordings:
                row = run_one(rec, "KXMLBGAME", cfg)
                if row is None:
                    continue
                for k in ("train_realized_net", "test_realized_net", "train_net", "test_net"):
                    agg_row[k] += row[k]
                agg_row["n_fills"] += row["n_fills"]
                agg_row["fees"] += row["fees_paid"]
            results[alpha].append((thr, agg_row))
            print(f"  {alpha} thr={thr:.6g}: train_rn={agg_row['train_realized_net']:+.2f} "
                  f"test_rn={agg_row['test_realized_net']:+.2f} train_net={agg_row['train_net']:+.2f} "
                  f"test_net={agg_row['test_net']:+.2f} fills={agg_row['n_fills']} fees={agg_row['fees']:.2f}")

    print(f"\n=== summary (threshold chosen on TRAIN realized-net) ===")
    print(f"{'alpha':>14} {'thr':>10} {'train_rn':>9} {'test_rn':>9} {'train_net':>10} {'test_net':>9}")
    for alpha, rows in results.items():
        thr, r = max(rows, key = lambda tr: tr[1]["train_realized_net"])
        print(f"{alpha:>14} {thr:>10.6g} {r['train_realized_net']:>+9.2f} {r['test_realized_net']:>+9.2f} "
              f"{r['train_net']:>+10.2f} {r['test_net']:>+9.2f}")


if __name__ == "__main__":
    main()

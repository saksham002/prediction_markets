"""
Per-game regression stability analysis.

Using the SAME selected features as the pooled lasso (from lasso_combo.json),
fit a small-ridge regression of 180s forward returns separately for each
game, standardizing with the GLOBAL fit-set stats so coefficients are
comparable. Report:
  - per-game weight vectors + per-game fit corr + game metadata
  - dispersion per coefficient vs a NOISE FLOOR (random split-half refits of
    the pooled fit data — dispersion below the floor is estimation noise)
  - k-means (k=2,3, seed 86) clustering of weight vectors and the league /
    volume / closeness composition of each cluster
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.lasso_pipeline import collect_samples, is_test, is_val, HORIZON_S
from research.hft.alphas import PairAlphaEngine
from research.hft.paths import DATASET, STUDIES

RIDGE = 1e-2
COMBO_PATH = str(STUDIES / "lasso_combo.json")


def ridge_fit(Z, y, lam = RIDGE):
    p = Z.shape[1]
    return np.linalg.solve(Z.T @ Z / len(y) + lam * np.eye(p), Z.T @ y / len(y))


def main():
    combo = json.load(open(COMBO_PATH))
    features = list(combo["means"].keys())   # selected feature set incl. zero-weight ones
    means = np.array([combo["means"][f] for f in features])
    stds = np.array([combo["stds"][f] for f in features])

    names, by_event = collect_samples(DATASET)
    col = {n: i for i, n in enumerate(names)}
    fidx = [col[f] for f in features]

    rows = []
    pooled_Z, pooled_y = [], []
    for event, g in sorted(by_event.items()):
        ts = np.array(g["ts"])
        if len(ts) < 240:
            continue
        order = np.argsort(ts)
        ts = ts[order]
        mid = np.array(g["mid"])[order]
        A = np.array(g["a"])[order][:, fidx]
        idx = np.searchsorted(ts, ts + HORIZON_S, side = "right") - 1
        valid = (idx >= 0) & (ts + HORIZON_S <= ts[-1]) & ~np.isnan(A).any(axis = 1)
        if valid.sum() < 240:
            continue
        y = (mid[idx[valid]] - mid[valid]) * 100.0
        Z = (A[valid] - means) / stds
        w = ridge_fit(Z, y)
        pred = Z @ w
        r = float(np.corrcoef(pred, y)[0, 1]) if pred.std() > 0 and y.std() > 0 else np.nan
        split = "test" if is_test(event) else ("val" if is_val(event) else "fit")
        rows.append({
            "event": event,
            "series": event.split("-", 1)[0],
            "split": split,
            "n": int(valid.sum()),
            "mid_mean": float(mid.mean()),
            "mid_vol": float(np.std(np.diff(mid)) * 100),
            "w": w,
            "fit_r": r,
        })
        if split == "fit":
            pooled_Z.append(Z)
            pooled_y.append(y)

    print(f"\nper-game fits: {len(rows)} games (>=240 valid rows each)")
    hdr = " ".join(f"{f[:10]:>11}" for f in features)
    print(f"{'event':<34} {'spl':<4} {'n':>6} {hdr} {'fit_r':>6}")
    for r in rows:
        wstr = " ".join(f"{wi:>11.3f}" for wi in r["w"])
        print(f"{r['event']:<34} {r['split']:<4} {r['n']:>6} {wstr} {r['fit_r']:>6.3f}")

    W = np.array([r["w"] for r in rows])
    print("\ncoefficient dispersion across games:")
    print(f"{'feature':<14} {'median':>8} {'IQR':>8} {'noise_IQR':>10}")
    # Noise floor: split-half refits of pooled fit data
    Zp = np.concatenate(pooled_Z)
    yp = np.concatenate(pooled_y)
    rng = np.random.default_rng(86)
    halves = []
    for _ in range(40):
        m = rng.random(len(yp)) < 0.5
        if m.sum() > 100:
            halves.append(ridge_fit(Zp[m], yp[m]))
    H = np.array(halves)
    for j, f in enumerate(features):
        med = np.median(W[:, j])
        iqr = np.percentile(W[:, j], 75) - np.percentile(W[:, j], 25)
        niqr = (np.percentile(H[:, j], 75) - np.percentile(H[:, j], 25)) * np.sqrt(len(rows) / 2)
        print(f"{f:<14} {med:>8.3f} {iqr:>8.3f} {niqr:>10.3f}")

    # k-means on normalized weight vectors
    Wn = W / (np.linalg.norm(W, axis = 1, keepdims = True) + 1e-12)
    labels = {}
    for k in (2, 3):
        centers = Wn[rng.choice(len(Wn), k, replace = False)]
        for _ in range(50):
            d = ((Wn[:, None, :] - centers[None]) ** 2).sum(-1)
            lab = d.argmin(1)
            for c in range(k):
                if (lab == c).any():
                    centers[c] = Wn[lab == c].mean(0)
        labels[k] = lab
        print(f"\nk-means k={k}:")
        for c in range(k):
            members = [rows[i] for i in range(len(rows)) if lab[i] == c]
            if not members:
                continue
            series = {}
            for m in members:
                series[m["series"]] = series.get(m["series"], 0) + 1
            mv = np.mean([m["mid_vol"] for m in members])
            mm = np.mean([abs(m["mid_mean"] - 0.5) for m in members])
            cw = np.mean([r2["w"] for r2 in members], axis = 0)
            print(f"  cluster {c}: n={len(members)} series={series} "
                  f"avg_midvol={mv:.3f}c avg_|mid-0.5|={mm:.3f}")
            print(f"    mean w: " + " ".join(f"{f[:8]}={wi:+.2f}" for f, wi in zip(features, cw)))

    # Persist results + figures
    import csv as csvmod
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out_dir = STUDIES / "per_game_weights"
    out_dir.mkdir(parents = True, exist_ok = True)
    with open(out_dir / "weights.csv", "w", newline = "") as f:
        wcsv = csvmod.writer(f)
        wcsv.writerow(["event", "series", "split", "n", "mid_vol_c", "abs_mid_dist", "fit_r"]
                      + [f"w_{x}" for x in features] + ["cluster_k2", "cluster_k3"])
        for i, r in enumerate(rows):
            wcsv.writerow([r["event"], r["series"], r["split"], r["n"],
                           round(r["mid_vol"], 4), round(abs(r["mid_mean"] - 0.5), 4),
                           round(r["fit_r"], 4)]
                          + [round(float(x), 4) for x in r["w"]]
                          + [int(labels[2][i]), int(labels[3][i])])

    ti = features.index("tfma_pw_300s") if "tfma_pw_300s" in features else 0
    oi = features.index("obi_ma_5s") if "obi_ma_5s" in features else 1
    fig, ax = plt.subplots(figsize = (9, 7))
    mv = np.array([r["mid_vol"] for r in rows])
    sc = ax.scatter(W[:, ti], W[:, oi], c = np.log10(mv + 1e-3), s = 90,
                    cmap = "coolwarm", edgecolor = "black")
    for i, r in enumerate(rows):
        ax.annotate(r["event"].split("-")[-1][:9], (W[i, ti], W[i, oi]), fontsize = 7,
                    xytext = (4, 4), textcoords = "offset points")
        if labels[2][i] == 1:
            ax.scatter([W[i, ti]], [W[i, oi]], marker = "s", s = 180,
                       facecolor = "none", edgecolor = "gray")
    ax.axhline(0, color = "gray", lw = 0.5)
    ax.axvline(0, color = "gray", lw = 0.5)
    ax.set_xlabel(f"weight: {features[ti]} (flow)")
    ax.set_ylabel(f"weight: {features[oi]} (book imbalance)")
    ax.set_title("Per-game regression weights — color = log10 mid-vol, squares = k2 cluster 1")
    fig.colorbar(sc, label = "log10 mid_vol (cents/step)")
    fig.tight_layout()
    fig.savefig(out_dir / "weight_regimes.png", dpi = 120)
    print(f"\nwrote {out_dir}/weights.csv + weight_regimes.png")


if __name__ == "__main__":
    main()

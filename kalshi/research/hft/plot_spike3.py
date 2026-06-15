"""Plots for the 3-leg spike sweep: PnL heatmap vs (t_pos, t_neg) and
PnL line vs t_neg (neg-only variant), summed over the 2 WC games."""
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

PLOTS = Path("/home/saksham3/projects/personal/prediction_markets/plots")


def main():
    both = defaultdict(float)
    negonly = defaultdict(float)
    with open(PLOTS / "spike3_results.csv") as f:
        for row in csv.DictReader(f):
            net = float(row["net_pnl"])
            if row["variant"] == "both":
                both[(float(row["t_pos"]), float(row["t_neg"]))] += net
            else:
                negonly[float(row["t_neg"])] += net

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize = (13, 5), constrained_layout = True)

    tpos_vals = sorted({k[0] for k in both})
    tneg_vals = sorted({k[1] for k in both})
    grid = np.full((len(tpos_vals), len(tneg_vals)), np.nan)
    for (tp, tn), v in both.items():
        grid[tpos_vals.index(tp), tneg_vals.index(tn)] = v
    vmax = np.nanmax(np.abs(grid))
    im = ax1.imshow(grid, cmap = "RdYlGn", vmin = -vmax, vmax = vmax, aspect = "auto")
    ax1.set_xticks(range(len(tneg_vals)), [f"{v:g}" for v in tneg_vals])
    ax1.set_yticks(range(len(tpos_vals)), [f"{v:g}" for v in tpos_vals])
    ax1.set_xlabel("t_neg")
    ax1.set_ylabel("t_pos")
    ax1.set_title("net PnL, MEX+KOR summed (t_pos, t_neg)")
    for i in range(len(tpos_vals)):
        for j in range(len(tneg_vals)):
            if not np.isnan(grid[i, j]):
                ax1.text(j, i, f"{grid[i, j]:+.0f}", ha = "center", va = "center", fontsize = 9)
    fig.colorbar(im, ax = ax1, shrink = 0.8)

    xs = sorted(negonly)
    ax2.plot(xs, [negonly[x] for x in xs], "o-", color = "tab:blue")
    ax2.axhline(0, color = "gray", lw = 0.6)
    ax2.set_xscale("log")
    ax2.set_xlabel("t_neg")
    ax2.set_ylabel("net PnL, MEX+KOR summed")
    ax2.set_title("neg-only variant (no t_pos)")
    ax2.grid(alpha = 0.3)

    out = PLOTS / "spike3_pnl.png"
    fig.savefig(out, dpi = 130)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

"""
Lead-lag cross-correlation between the three 60s-HL signals tfma_60s, agg_60s
and obi_ma_60s (obi's like-for-like 60s-smoothed form), per league and overall.

Reuses collect_samples for the 1 Hz per-game alpha series, resamples each to a
1 s grid (forward-fill, dropped across gaps longer than the 60s HL so dead
periods don't inflate correlation), demeans per game, and pools the lagged
cross-products.

  rho_XY(tau) = corr(X[t], Y[t+tau])

A peak at tau > 0 means X[t] lines up with Y in the future, i.e. X LEADS Y by
tau seconds; a peak at tau < 0 means Y leads X.

Outputs: plots/lead_lag_60s.csv (long: league, pair, lag_s, r, n) and
plots/lead_lag_60s.png (CCF curves per pair, per scope).
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.lasso_pipeline import collect_samples

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
OUT_CSV = Path("/home/saksham3/projects/personal/prediction_markets/plots/lead_lag_60s.csv")
OUT_PNG = Path("/home/saksham3/projects/personal/prediction_markets/plots/lead_lag_60s.png")

ALPHAS = ["tfma_60s", "agg_60s", "obi_ma_60s"]
PAIRS = [("tfma_60s", "agg_60s"), ("tfma_60s", "obi_ma_60s"), ("agg_60s", "obi_ma_60s")]
MAX_LAG = 300          # seconds, both directions
STALE_CAP = 60         # don't forward-fill across gaps longer than the 60s HL
MIN_GRID = 60          # skip games with too short a grid


def league_of(event: str) -> str:
    return event.split(":")[0].split("-", 1)[0]


def grid_series(ts: np.ndarray, vals: np.ndarray) -> np.ndarray:
    """1 s-grid forward-fill of vals(ts); NaN where the last sample is older
    than STALE_CAP or itself NaN."""
    t0, t1 = int(np.floor(ts[0])), int(np.floor(ts[-1]))
    grid = np.arange(t0, t1 + 1)
    idx = np.searchsorted(ts, grid, side = "right") - 1
    ok = idx >= 0
    safe = np.where(ok, idx, 0)
    age = grid - ts[safe]
    v = vals[safe]
    out = np.full(len(grid), np.nan)
    fill = ok & (age <= STALE_CAP) & ~np.isnan(v)
    out[fill] = v[fill]
    return out


def main():
    names, by_event = collect_samples(DATASET)
    col = {n: i for i, n in enumerate(names)}
    lags = np.arange(-MAX_LAG, MAX_LAG + 1)
    n_lag = len(lags)
    # (scope, pair) -> running cross-product sums over lags
    acc: dict = defaultdict(lambda: {k: np.zeros(n_lag) for k in ("sxy", "sxx", "syy", "n")})
    leagues = set()

    for event, g in by_event.items():
        ts = np.array(g["ts"])
        if len(ts) < MIN_GRID:
            continue
        order = np.argsort(ts)
        ts = ts[order]
        A = np.array(g["a"])[order]
        series = {}
        for a in ALPHAS:
            s = grid_series(ts, A[:, col[a]])
            finite = np.isfinite(s)
            series[a] = s - s[finite].mean() if finite.any() else s
        if len(next(iter(series.values()))) < MIN_GRID:
            continue
        league = league_of(event)
        leagues.add(league)

        for X, Y in PAIRS:
            x, y = series[X], series[Y]
            L = len(x)
            for li, lag in enumerate(lags):
                if lag >= 0:
                    xa, ya = x[: L - lag], y[lag:]
                else:
                    xa, ya = x[-lag:], y[: L + lag]
                m = ~np.isnan(xa) & ~np.isnan(ya)
                if not m.any():
                    continue
                xa, ya = xa[m], ya[m]
                for scope in (league, "ALL"):
                    d = acc[(scope, (X, Y))]
                    d["sxy"][li] += float(xa @ ya)
                    d["sxx"][li] += float(xa @ xa)
                    d["syy"][li] += float(ya @ ya)
                    d["n"][li] += len(xa)

    scopes = sorted(leagues) + ["ALL"]

    def rho(scope, pair):
        d = acc[(scope, pair)]
        denom = np.sqrt(d["sxx"] * d["syy"])
        r = np.full(n_lag, np.nan)
        good = (denom > 0) & (d["n"] >= 100)
        r[good] = d["sxy"][good] / denom[good]
        return r, d["n"]

    OUT_CSV.parent.mkdir(parents = True, exist_ok = True)
    with open(OUT_CSV, "w", newline = "") as fp:
        w = csv.writer(fp)
        w.writerow(["league", "pair", "lag_s", "r", "n"])
        for scope in scopes:
            for pair in PAIRS:
                r, n = rho(scope, pair)
                for li, lag in enumerate(lags):
                    w.writerow([scope, f"{pair[0]}->{pair[1]}", int(lag),
                                f"{r[li]:.4f}" if r[li] == r[li] else "", int(n[li])])
    print(f"wrote {OUT_CSV}")

    # Peak-lag summary (by |r|, so negative relationships locate correctly)
    print(f"\nLead-lag peaks  [rho_XY(tau)=corr(X[t], Y[t+tau]); tau>0 => X leads Y]")
    for scope in scopes:
        print(f"\n[{scope}]")
        for pair in PAIRS:
            r, _ = rho(scope, pair)
            if np.all(np.isnan(r)):
                continue
            li = int(np.nanargmax(np.abs(r)))
            lag = int(lags[li])
            if lag == 0:
                rel = "contemporaneous"
            elif lag > 0:
                rel = f"{pair[0]} leads {pair[1]} by {lag}s"
            else:
                rel = f"{pair[1]} leads {pair[0]} by {-lag}s"
            edge = "  (AT WINDOW EDGE)" if abs(lag) == MAX_LAG else ""
            print(f"  {pair[0]:<11} vs {pair[1]:<11} peak |r|: r={r[li]:+.3f} @ lag={lag:+d}s "
                  f"-> {rel}{edge}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, len(PAIRS), figsize = (15, 4.2), sharex = True)
        for ax, pair in zip(axes, PAIRS):
            for scope in scopes:
                r, _ = rho(scope, pair)
                ax.plot(lags, r, label = scope, lw = 1.3)
            ax.axvline(0, color = "k", lw = 0.6, ls = ":")
            ax.axhline(0, color = "k", lw = 0.6, ls = ":")
            ax.set_title(f"{pair[0]}  vs  {pair[1]}")
            ax.set_xlabel("lag tau (s)  [>0: first leads]")
            ax.grid(alpha = 0.25)
        axes[0].set_ylabel("corr(X[t], Y[t+tau])")
        axes[-1].legend(fontsize = 8)
        fig.suptitle("Lead-lag CCF of 60s-HL signals (per-game demeaned, pooled)")
        fig.tight_layout()
        fig.savefig(OUT_PNG, dpi = 130)
        print(f"\nwrote {OUT_PNG}")
    except Exception as e:
        print(f"plot skipped ({type(e).__name__}: {e})")


if __name__ == "__main__":
    main()

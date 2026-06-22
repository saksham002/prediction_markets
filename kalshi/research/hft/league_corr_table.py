"""
Correlation table: each alpha (incl. every half-life) vs forward pair-mid
return at multiple horizons, for each league separately and pooled overall.

Reuses collect_samples (lasso_pipeline) to sample all alphas across the whole
dataset, then groups events by league (the series prefix of the event ticker)
and reports Pearson r of alpha[t] against the cents forward return at each
horizon. "ALL" pools every league.

Output: plots/alpha_return_corr.csv — one row per (league, alpha), with an
r_<h>s column and an n_<h>s column for each horizon.
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.lasso_pipeline import collect_samples
from research.hft.tick_study import HORIZONS_S
from research.hft.paths import DATASET
OUT = Path("/home/saksham3/projects/personal/prediction_markets/plots/alpha_return_corr.csv")
MIN_N = 30


def league_of(event: str) -> str:
    """Series prefix, e.g. 'KXMLBGAME-26JUN..HOULAA' / 'KXWCGAME-..:TKR' -> 'KXMLBGAME'."""
    return event.split(":")[0].split("-", 1)[0]


def main():
    names, by_event = collect_samples(DATASET)
    n_alpha = len(names)

    # Per league: pooled alpha matrix A (rows, n_alpha) and forward-return
    # matrix F (rows, len(HORIZONS_S)). Forward returns are computed per event
    # (searchsorted must stay within a game), then concatenated.
    A_by_league: dict[str, list] = defaultdict(list)
    F_by_league: dict[str, list] = defaultdict(list)
    games_by_league: dict[str, int] = defaultdict(int)

    for event, g in by_event.items():
        ts = np.array(g["ts"])
        if len(ts) < MIN_N:
            continue
        order = np.argsort(ts)
        ts = ts[order]
        mid = np.array(g["mid"])[order]
        A = np.array(g["a"])[order]

        F = np.full((len(ts), len(HORIZONS_S)), np.nan)
        for hj, horizon in enumerate(HORIZONS_S):
            idx = np.searchsorted(ts, ts + horizon, side = "right") - 1
            valid = (idx >= 0) & (ts + horizon <= ts[-1])
            F[valid, hj] = (mid[idx[valid]] - mid[valid]) * 100.0

        league = league_of(event)
        A_by_league[league].append(A)
        F_by_league[league].append(F)
        games_by_league[league] += 1

    leagues = sorted(A_by_league.keys())
    scopes = leagues + ["ALL"]

    # Concatenate per league; ALL = stack of every league.
    A_cat = {lg: np.concatenate(A_by_league[lg]) for lg in leagues}
    F_cat = {lg: np.concatenate(F_by_league[lg]) for lg in leagues}
    A_cat["ALL"] = np.concatenate([A_cat[lg] for lg in leagues])
    F_cat["ALL"] = np.concatenate([F_cat[lg] for lg in leagues])
    games_by_league["ALL"] = sum(games_by_league[lg] for lg in leagues)

    def corr(a: np.ndarray, f: np.ndarray) -> tuple[float, int]:
        mask = ~np.isnan(a) & ~np.isnan(f)
        a, f = a[mask], f[mask]
        if len(a) < MIN_N or a.std() == 0 or f.std() == 0:
            return float("nan"), len(a)
        return float(pearsonr(a, f)[0]), len(a)

    OUT.parent.mkdir(parents = True, exist_ok = True)
    header = (["league", "alpha"]
              + [f"r_{h}s" for h in HORIZONS_S]
              + [f"n_{h}s" for h in HORIZONS_S])
    with open(OUT, "w", newline = "") as fp:
        w = csv.writer(fp)
        w.writerow(header)
        for scope in scopes:
            A = A_cat[scope]
            F = F_cat[scope]
            for ai, alpha in enumerate(names):
                rs, ns = [], []
                for hj in range(len(HORIZONS_S)):
                    r, n = corr(A[:, ai], F[:, hj])
                    rs.append(r)
                    ns.append(n)
                w.writerow([scope, alpha]
                           + [f"{r:.4f}" if r == r else "" for r in rs]
                           + ns)

    print(f"\nwrote {OUT} ({len(scopes)} scopes x {n_alpha} alphas)")
    print(f"games per league: " + ", ".join(f"{lg}={games_by_league[lg]}" for lg in scopes))

    # Headline preview: tfma_pw + obi + mom correlations, ALL scope
    preview = ([f"tfma_pw_{l}" for l in ("1s", "5s", "10s", "30s", "60s", "300s")]
               + ["obi"] + [f"mom_{l}" for l in ("5s", "30s", "120s")])
    col = {n: i for i, n in enumerate(names)}
    print(f"\nALL pool, Pearson r (cents forward return):")
    print(f"{'alpha':<14}" + "".join(f"{str(h)+'s':>9}" for h in HORIZONS_S))
    A = A_cat["ALL"]
    F = F_cat["ALL"]
    for name in preview:
        ai = col[name]
        cells = "".join(f"{corr(A[:, ai], F[:, hj])[0]:>+9.4f}" for hj in range(len(HORIZONS_S)))
        print(f"{name:<14}{cells}")


if __name__ == "__main__":
    main()

"""
Strategy-faithful alpha/return correlation table, per league and overall.

Differs from league_corr_table.py (which sampled the pooled pair-mid at 1 Hz)
on every point the user flagged:

  * Sampling: one row per STRATEGY TRIGGER per market — a trade or a
    top-of-book change (mm_sim requotes only on those; deep book deltas are
    skipped). No 1 Hz throttle.
  * Forward price: forward_fields() looks up the EXACT touch at t+horizon over
    the market's full trigger series, BEFORE filtering (forward_price.py).
  * Filtering: only rows where the leg is quotable under the strategy's
    market-condition gate are correlated (filter_strategy.py).
  * Return: relative, (p[t+h] - p[t]) / p[t], with p the touch mid.

Per market we leg-sign the (pair-space) alphas so both legs pool in their own
yes-space: first leg +alpha, second leg -alpha; soccer single markets +alpha.

Output: plots/alpha_return_corr_strat.csv — one row per (league, alpha), with
r_<h>s / n_<h>s columns over the extended horizon grid.
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import PairAlphaEngine
from research.hft.filter_strategy import FilterStrategy
from research.hft.forward_price import forward_fields
from research.hft.replay import Replayer
from research.hft.tick_study import StudyConsumer

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
OUT = Path("/home/saksham3/projects/personal/prediction_markets/plots/alpha_return_corr_strat.csv")

# Extended beyond the original [1,5,10,30,60,300]s (alpha HLs are extended in alphas.py).
HORIZONS_S = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800]
MIN_N = 30
FILTER = FilterStrategy()


class TriggerConsumer(StudyConsumer):
    """Records one row per strategy trigger (trade / top-of-book change) per
    market: (ts, yes_bid, yes_ask, leg-signed alpha vector). Reuses
    StudyConsumer.on_meta for engine creation and the alpha_names order."""

    def __init__(self, replayer: Replayer):
        super().__init__(replayer, throttle_s = 0.0)
        self.table: dict[str, list] = defaultdict(list)
        self._last_top: dict[str, tuple] = {}
        self._nan_row = [np.nan] * len(self.alpha_names)

    def _engine_sign(self, ticker: str):
        event = self.pair_by_ticker.get(ticker)
        if event is None:
            return None, 1.0
        engine = self.engines[event]
        is_second = engine.second_ticker == ticker and engine.first_ticker != engine.second_ticker
        return engine, (-1.0 if is_second else 1.0)

    def _record(self, ticker: str, lts: float, engine, sign: float):
        top = self.replayer.top(ticker)
        if top.yes_bid is None or top.yes_ask is None:
            return
        # Full touch series is retained for the forward lookup; alphas are only
        # computed where the row is quotable (the rows we'll actually correlate).
        if FILTER.allows(top.yes_bid, top.yes_ask):
            vals = engine.values(now = lts)
            a = [np.nan if vals[n] is None else sign * vals[n] for n in self.alpha_names]
        else:
            a = self._nan_row
        self.table[ticker].append((lts, top.yes_bid, top.yes_ask, a))

    def on_book(self, lts: float, ticker: str, delta_msg):
        engine, sign = self._engine_sign(ticker)
        if engine is None:
            return
        if delta_msg is not None:
            engine.on_delta(lts, ticker, delta_msg)
        engine.on_book(lts, ticker)
        top = self.replayer.top(ticker)
        key = (top.yes_bid, top.yes_ask)
        if key != self._last_top.get(ticker):           # top-of-book change = requote trigger
            self._last_top[ticker] = key
            self._record(ticker, lts, engine, sign)

    def on_trade(self, lts: float, msg: dict):
        ticker = msg["market_ticker"]
        engine, sign = self._engine_sign(ticker)
        if engine is None:
            return
        engine.on_trade(lts, msg)
        self._record(ticker, lts, engine, sign)          # trade = requote trigger


def league_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def corr(a: np.ndarray, f: np.ndarray) -> tuple[float, int]:
    mask = ~np.isnan(a) & ~np.isnan(f)
    a, f = a[mask], f[mask]
    if len(a) < MIN_N or a.std() == 0 or f.std() == 0:
        return float("nan"), len(a)
    return float(pearsonr(a, f)[0]), len(a)


def main():
    names = PairAlphaEngine.alpha_names()
    n_alpha = len(names)
    A_by_league: dict[str, list] = defaultdict(list)   # quotable-row alpha matrices
    RET_by_league: dict[str, list] = defaultdict(list)  # quotable-row return matrices (per horizon)

    for rec in sorted(DATASET.glob("*.jsonl.gz")):
        replayer = Replayer(rec)
        consumer = TriggerConsumer(replayer)
        replayer.run(consumer)
        kept = 0
        for ticker, rows in consumer.table.items():
            if len(rows) < MIN_N:
                continue
            ts = np.array([r[0] for r in rows])
            yb = np.array([r[1] for r in rows])
            ya = np.array([r[2] for r in rows])
            A = np.array([r[3] for r in rows], dtype = float)
            mid = (yb + ya) / 2.0

            fwd = forward_fields(ts, yb, ya, HORIZONS_S)              # before filtering
            RET = np.full((len(ts), len(HORIZONS_S)), np.nan)
            for hj, h in enumerate(HORIZONS_S):
                p_h = (fwd[f"bid_{h}"] + fwd[f"ask_{h}"]) / 2.0
                RET[:, hj] = (p_h - mid) / mid                        # relative return

            quotable = FILTER.mask(yb, ya)
            if not quotable.any():
                continue
            league = league_of(ticker)
            A_by_league[league].append(A[quotable])
            RET_by_league[league].append(RET[quotable])
            kept += int(quotable.sum())
        print(f"  {rec.name}: {kept} quotable trigger rows")

    leagues = sorted(A_by_league.keys())
    A_cat = {lg: np.concatenate(A_by_league[lg]) for lg in leagues}
    RET_cat = {lg: np.concatenate(RET_by_league[lg]) for lg in leagues}
    A_cat["ALL"] = np.concatenate([A_cat[lg] for lg in leagues])
    RET_cat["ALL"] = np.concatenate([RET_cat[lg] for lg in leagues])
    scopes = leagues + ["ALL"]

    OUT.parent.mkdir(parents = True, exist_ok = True)
    header = (["league", "alpha"]
              + [f"r_{h}s" for h in HORIZONS_S]
              + [f"n_{h}s" for h in HORIZONS_S])
    with open(OUT, "w", newline = "") as fp:
        w = csv.writer(fp)
        w.writerow(header)
        for scope in scopes:
            A = A_cat[scope]
            RET = RET_cat[scope]
            for ai, alpha in enumerate(names):
                rs, ns = [], []
                for hj in range(len(HORIZONS_S)):
                    r, n = corr(A[:, ai], RET[:, hj])
                    rs.append(r)
                    ns.append(n)
                w.writerow([scope, alpha]
                           + [f"{r:.4f}" if r == r else "" for r in rs]
                           + ns)

    print(f"\nwrote {OUT} ({len(scopes)} scopes x {n_alpha} alphas, horizons {HORIZONS_S})")
    print("quotable rows per scope: "
          + ", ".join(f"{s}={len(A_cat[s])}" for s in scopes))

    # Headline preview: obi + a couple of long-HL families on ALL
    preview = ["obi", "obi_ma_60s", "tfma_pw_300s", "tfma_pw_1800s",
               "agg_pw_300s", "mom_120s", "mom_600s"]
    col = {n: i for i, n in enumerate(names)}
    A = A_cat["ALL"]
    RET = RET_cat["ALL"]
    print(f"\nALL pool, Pearson r (relative return):")
    print(f"{'alpha':<16}" + "".join(f"{str(h)+'s':>9}" for h in HORIZONS_S))
    for name in preview:
        ai = col[name]
        cells = "".join(f"{corr(A[:, ai], RET[:, hj])[0]:>+9.4f}" for hj in range(len(HORIZONS_S)))
        print(f"{name:<16}{cells}")


if __name__ == "__main__":
    main()

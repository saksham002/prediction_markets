"""Per WC game: 6 stacked panels (tfma_pw_5s + YES odds per leg), plus an A/B
test of the tfma_pw price weighting: taker-side price (current), symmetric
YES price, and raw — each correlated with the 120s forward return."""
import datetime
import math
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from alphas import PairAlphaEngine
from plot_alpha import DATASET, GAMES, game_clock_ticks
from replay import Replayer
from tick_study import StudyConsumer

OUT_DIR = Path("/home/saksham3/projects/personal/prediction_markets/plots")
ALPHA = "tfma_pw_5s"
AB_HL = 30.0
AB_HORIZON = 120.0


def plots(alpha = None):
    alpha = alpha or ALPHA
    names = PairAlphaEngine.alpha_names()
    idx = names.index(alpha)
    for game, g in GAMES.items():
        replayer = Replayer(DATASET / g["file"])
        consumer = StudyConsumer(replayer, 1.0)
        replayer.run(consumer)
        wc = {k: v for k, v in consumer.samples.items()
              if k.split(":")[0].startswith("KXWCGAME") and v}
        legs = sorted(wc.items())
        fig, axes = plt.subplots(2 * len(legs), 1, figsize = (14, 1.9 * 2 * len(legs)),
                                 sharex = True, constrained_layout = True)
        xmin = min(r[0] for _, rows in legs for r in rows)
        xmax = max(r[0] for _, rows in legs for r in rows)
        ticks, labels = game_clock_ticks(g, xmin, xmax)
        for i, (key, rows) in enumerate(legs):
            leg = key.split(":")[1].replace("KXWCGAME-", "")
            ts = [r[0] for r in rows]
            a = np.array([np.nan if r[4][idx] is None else r[4][idx] for r in rows])
            mid = np.array([r[1] for r in rows])
            ax_a, ax_m = axes[2 * i], axes[2 * i + 1]
            ax_a.plot(ts, a, lw = 0.6, color = "tab:blue")
            ax_a.axhline(0, color = "gray", lw = 0.5)
            ax_a.set_ylabel(f"{leg}\n{alpha}", fontsize = 7)
            ax_m.plot(ts, mid, lw = 0.8, color = "tab:red")
            ax_m.set_ylabel(f"{leg}\nYES odds", fontsize = 7)
            ax_m.set_ylim(0, 1)
            for ax in (ax_a, ax_m):
                ax.grid(alpha = 0.25)
                ax.axvspan(g["ht"], g["sh"], color = "gray", alpha = 0.12)
                if xmin < g["ko"]:
                    ax.axvspan(xmin, g["ko"], color = "gray", alpha = 0.12)
                if xmax > g["ft"]:
                    ax.axvspan(g["ft"], xmax, color = "gray", alpha = 0.12)
                ax.set_xticks(ticks, labels)
        axes[0].set_title(f"{game} — {alpha} vs YES odds per leg (exact game clock)")
        axes[-1].set_xlabel("game clock")
        axes[-1].set_xlim(xmin, xmax)
        out = OUT_DIR / f"alpha_vs_odds_{alpha}_{game}.png" if alpha != ALPHA else OUT_DIR / f"alpha_vs_odds_{game}.png"
        fig.savefig(out, dpi = 130)
        plt.close(fig)
        print(f"wrote {out}")


class ABConsumer:
    """Three tfma variants side by side: taker-price weight (current code),
    symmetric YES-price weight, raw signed qty."""

    def __init__(self, replayer):
        self.replayer = replayer
        self.decay = math.log(2) / AB_HL
        self.tickers: set = set()
        self.state: dict = {}
        self.samples = defaultdict(list)
        self._last_sample: dict = {}

    def on_meta(self, lts, meta):
        for ev in meta.get("events", []):
            if ev["series"] == "KXWCGAME":
                self.tickers.update(ev["tickers"])

    def on_trade(self, lts, msg):
        tkr = msg["market_ticker"]
        if tkr not in self.tickers:
            return
        qty = float(msg["count_fp"])
        side = msg["taker_side"]
        sq = qty if side == "yes" else -qty
        if "yes_price_dollars" in msg:
            yes_p = float(msg["yes_price_dollars"])
        else:
            yes_p = 1.0 - float(msg["no_price_dollars"])
        taker_p = yes_p if side == "yes" else 1.0 - yes_p
        st = self.state.setdefault(tkr, [None, 0.0, 0.0, 0.0])
        ts = float(msg.get("ts", lts))
        if st[0] is not None:
            f = math.exp(-self.decay * max(ts - st[0], 0.0))
            st[1] *= f
            st[2] *= f
            st[3] *= f
        st[0] = ts
        st[1] += sq * taker_p
        st[2] += sq * yes_p
        st[3] += sq
        self._sample(lts, tkr)

    def on_book(self, lts, ticker, delta_msg):
        if ticker in self.tickers and ticker in self.state:
            self._sample(lts, ticker)

    def _sample(self, lts, tkr):
        if lts - self._last_sample.get(tkr, 0.0) < 1.0:
            return
        mid = self.replayer.top(tkr).mid
        if mid is None:
            return
        st = self.state[tkr]
        f = math.exp(-self.decay * max(lts - st[0], 0.0))
        self.samples[tkr].append((lts, mid, st[1] * f, st[2] * f, st[3] * f))
        self._last_sample[tkr] = lts


def ab_test():
    cols = {"taker_price (current)": 2, "yes_price (symmetric)": 3, "raw": 4}
    pooled = {k: ([], []) for k in cols}
    for game, g in GAMES.items():
        replayer = Replayer(DATASET / g["file"])
        consumer = ABConsumer(replayer)
        replayer.run(consumer)
        for tkr, rows in consumer.samples.items():
            arr = np.array(rows)
            ts = arr[:, 0]
            idx = np.searchsorted(ts, ts + AB_HORIZON, side = "right") - 1
            valid = (idx >= 0) & (ts + AB_HORIZON <= ts[-1])
            fwd = (arr[idx[valid], 1] - arr[valid, 1]) * 100.0
            for name, c in cols.items():
                pooled[name][0].append(arr[valid, c])
                pooled[name][1].append(fwd)
    print(f"\nA/B weighting test, {AB_HL:g}s HL, {AB_HORIZON:g}s horizon, WC pool:")
    for name, (xs, ys) in pooled.items():
        x = np.concatenate(xs)
        y = np.concatenate(ys)
        r = float(np.corrcoef(x, y)[0, 1])
        print(f"  {name:<24} r={r:+.4f}  (n={len(y)})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--alpha", default = None)
    parser.add_argument("--skip-ab", action = "store_true")
    args = parser.parse_args()
    plots(args.alpha)
    if not args.skip_ab:
        ab_test()

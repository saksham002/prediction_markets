"""Plot a single alpha vs time for each market of the recorded WC games.

Usage: plot_alpha.py [--alpha tfma_pw_5s] [--out-dir DIR]
"""
import argparse
import datetime
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from alphas import PairAlphaEngine
from replay import Replayer
from tick_study import StudyConsumer

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")


def _ts(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# Exact period boundaries from ESPN keyEvents wallclocks (events 760415/760414)
GAMES = {
    "MEX-RSA": {
        "file": "ticks_20260611_074145_3157453.jsonl.gz",
        "ko": _ts("2026-06-11T19:05:57Z"), "ht": _ts("2026-06-11T19:54:57Z"),
        "sh": _ts("2026-06-11T20:11:31Z"), "ft": _ts("2026-06-11T21:03:44Z"),
    },
    "KOR-CZE": {
        "file": "ticks_20260611_193554_1678643.jsonl.gz",
        "ko": _ts("2026-06-12T02:00:46Z"), "ht": _ts("2026-06-12T02:48:52Z"),
        "sh": _ts("2026-06-12T03:03:57Z"), "ft": _ts("2026-06-12T03:57:03Z"),
    },
}


def game_clock_ticks(g, xmin, xmax):
    """Tick positions (wall time) and exact game-clock labels."""
    ticks, labels = [], []
    for m in range(0, 46, 15):
        t = g["ko"] + m * 60
        if xmin <= t <= min(xmax, g["ht"]):
            ticks.append(t)
            labels.append("KO" if m == 0 else f"{m}'")
    for m in range(45, 91, 15):
        t = g["sh"] + (m - 45) * 60
        if max(xmin, g["sh"]) <= t <= min(xmax, g["ft"]):
            ticks.append(t)
            labels.append(f"{m}'")
    if xmin <= g["ft"] <= xmax:
        ticks.append(g["ft"])
        labels.append("FT")
    return ticks, labels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alpha", default = "tfma_pw_5s")
    parser.add_argument("--out-dir", default = "/home/saksham3/projects/personal/prediction_markets/plots")
    args = parser.parse_args()

    names = PairAlphaEngine.alpha_names()
    idx = names.index(args.alpha)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents = True, exist_ok = True)

    for game, g in GAMES.items():
        replayer = Replayer(DATASET / g["file"])
        consumer = StudyConsumer(replayer, 1.0)
        replayer.run(consumer)
        wc = {k: v for k, v in consumer.samples.items()
              if k.split(":")[0].startswith("KXWCGAME") and v}
        if not wc:
            print(f"{game}: no WC samples found in {g['file']}")
            continue
        fig, axes = plt.subplots(len(wc), 1, figsize = (14, 2.8 * len(wc)),
                                 sharex = True, constrained_layout = True)
        axes = np.atleast_1d(axes)
        xmin = min(r[0] for rows in wc.values() for r in rows)
        xmax = max(r[0] for rows in wc.values() for r in rows)
        ticks, labels = game_clock_ticks(g, xmin, xmax)
        for ax, (key, rows) in zip(axes, sorted(wc.items())):
            ts = [r[0] for r in rows]
            a = np.array([np.nan if r[4][idx] is None else r[4][idx] for r in rows])
            ax.plot(ts, a, lw = 0.6, color = "tab:blue")
            ax.axhline(0, color = "gray", lw = 0.5)
            ax.set_ylabel(key.split(":")[1].replace("KXWCGAME-", ""), fontsize = 8)
            ax.grid(alpha = 0.25)
            # Shade the non-play phases on the exact whistle boundaries
            if xmin < g["ko"]:
                ax.axvspan(xmin, g["ko"], color = "gray", alpha = 0.12)
            ax.axvspan(g["ht"], g["sh"], color = "gray", alpha = 0.12)
            if xmax > g["ft"]:
                ax.axvspan(g["ft"], xmax, color = "gray", alpha = 0.12)
            ax.set_xticks(ticks, labels)
        if xmin < g["ko"]:
            axes[0].text((xmin + g["ko"]) / 2, axes[0].get_ylim()[1] * 0.9,
                         "pre-game", ha = "center", fontsize = 8, color = "dimgray")
        axes[0].text((g["ht"] + g["sh"]) / 2, axes[0].get_ylim()[1] * 0.9,
                     "HT", ha = "center", fontsize = 8, color = "dimgray")
        axes[0].set_title(f"{game} — {args.alpha} (1s samples, exact game clock)")
        axes[-1].set_xlabel("game clock")
        axes[-1].set_xlim(xmin, xmax)
        out = out_dir / f"{args.alpha}_{game}.png"
        fig.savefig(out, dpi = 130)
        plt.close(fig)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()

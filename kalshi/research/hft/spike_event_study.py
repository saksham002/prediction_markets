"""Event study: mean 120s forward return conditioned on tfma_pw_30s magnitude
bucket, WC games pooled. Adjudicates whether sharp spikes follow through even
though pooled correlation is negative."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from plot_alpha import DATASET, GAMES
from plot_alpha_odds import ABConsumer
from replay import Replayer

BUCKETS = [0, 500, 1000, 2000, 5000, 10000, 25000, 50000, np.inf]


def main():
    xs, ys = [], []
    for game, g in GAMES.items():
        replayer = Replayer(DATASET / g["file"])
        consumer = ABConsumer(replayer)
        replayer.run(consumer)
        for tkr, rows in consumer.samples.items():
            arr = np.array(rows)
            ts = arr[:, 0]
            idx = np.searchsorted(ts, ts + 120.0, side = "right") - 1
            valid = (idx >= 0) & (ts + 120.0 <= ts[-1])
            xs.append(arr[valid, 2])  # taker-price-weighted 30s EMA (current alpha)
            ys.append((arr[idx[valid], 1] - arr[valid, 1]) * 100.0)
    x = np.concatenate(xs)
    y = np.concatenate(ys)

    print("signed-bucket event study, tfma_pw_30s vs 120s fwd return (cents), WC pool:")
    print(f"{'bucket':>22} {'n':>7} {'mean_fwd':>9} {'t-stat':>7}")
    for lo, hi in zip(BUCKETS[:-1], BUCKETS[1:]):
        for sign, m in (("+", (x >= lo) & (x < hi)), ("-", (x <= -lo) & (x > -hi))):
            n = int(m.sum())
            if n < 10:
                continue
            mu = y[m].mean()
            t = mu / (y[m].std() / np.sqrt(n)) if n > 1 and y[m].std() > 0 else 0.0
            lab = f"{sign}[{lo:g},{hi:g})"
            print(f"{lab:>22} {n:>7} {mu:>+9.3f} {t:>7.1f}")


if __name__ == "__main__":
    main()

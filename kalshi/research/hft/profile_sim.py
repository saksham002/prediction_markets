"""
Profile one mm_sim replay on a single-event recording and print a component
time breakdown (absolute seconds + % of total).

Usage: profile_sim.py <recording> [--alpha agree_om]
"""

import argparse
import cProfile
import pstats
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.mm_sim import MMSimConsumer
from research.hft.replay import Replayer
from research.hft.passive_fill import FORWARD_DELAY_S

# Component buckets: substrings matched against "file:function" keys
BUCKETS = [
    ("json parse", ["json/", "decoder.py", "loads"]),
    ("gzip read", ["gzip.py", "_compression"]),
    ("book updates", ["orderbook.py"]),
    ("alpha: TFMA", ["trade_fill_ma.py"]),
    ("alpha: OBI/depth", ["alphas.py:_depth", "alphas.py:market_obi", "_pair_obi"]),
    ("alpha: engine other", ["alphas.py"]),
    ("fill engine", ["passive_fill.py"]),
    ("strategy requote/fills", ["mm_sim.py:requote", "_desired_sides", "_leg_alpha", "on_fill"]),
    ("state/mid logging", ["_maybe_log_state", "_record_mid", "_deployed_dollars", "log_fill"]),
    ("replay dispatch", ["replay.py"]),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recording")
    parser.add_argument("--alpha", default = "agree_om")
    args = parser.parse_args()

    params = SimpleNamespace(
        per_order_size = 500, inventory_cap = 1000, skew_threshold = 0.0,
        alpha_name = args.alpha, max_spread = 0.02, price_min = 0.05, price_max = 0.95,
        improve = False, pair_risk = True, combo = None, series = None,
        forward_delay = FORWARD_DELAY_S, size_ref = None, max_queue_ahead = None,
        depth_quote = False, ladder = False, per_leg_alpha = False,
        budget = 1000, write_rate = 10,
    )
    replayer = Replayer(args.recording)
    consumer = MMSimConsumer(replayer, params)

    prof = cProfile.Profile()
    prof.enable()
    n = replayer.run(consumer)
    prof.disable()

    stats = pstats.Stats(prof)
    total = stats.total_tt
    rows = []
    seen = set()
    bucket_time = {name: 0.0 for name, _ in BUCKETS}
    other = 0.0
    for (fname, lineno, func), (cc, nc, tt, ct, callers) in stats.stats.items():
        key = f"{Path(fname).name}:{func}"
        placed = False
        for bname, pats in BUCKETS:
            if any(p.rstrip(":") in key or p in fname for p in pats):
                # match either file or file:function patterns
                if any((":" in p and p.split(":")[1] in func and p.split(":")[0] in key) or
                       (":" not in p and (p in key or p in fname)) for p in pats):
                    bucket_time[bname] += tt
                    placed = True
                    break
        if not placed:
            other += tt

    print(f"\nmessages: {n}   total profiled time: {total:.1f}s   fills: {len(consumer.fill_rows)}")
    print(f"{'component':<26} {'seconds':>9} {'%':>6}")
    for bname, _ in BUCKETS:
        t = bucket_time[bname]
        print(f"{bname:<26} {t:>9.1f} {t / total * 100:>5.1f}%")
    print(f"{'everything else':<26} {other:>9.1f} {other / total * 100:>5.1f}%")

    print("\ntop 12 functions by own-time:")
    stats.sort_stats("tottime").print_stats(12)


if __name__ == "__main__":
    main()

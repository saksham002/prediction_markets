"""
Distribution of aggressive (taker) trade sizes per series from a tick recording.
"""

import gzip
import json
import sys
import zlib
from collections import defaultdict

import numpy as np


def main():
    sizes = defaultdict(list)
    notionals = defaultdict(list)
    for path in sys.argv[1:]:
        f = gzip.open(path, "rt")
        try:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                d = rec.get("d", {})
                if d.get("type") != "trade":
                    continue
                m = d["msg"]
                qty = float(m["count_fp"])
                yp = float(m["yes_price_dollars"])
                price = yp if m["taker_side"] == "yes" else 1.0 - yp
                series = m["market_ticker"].split("-", 1)[0]
                sizes[series].append(qty)
                notionals[series].append(qty * price)
        except (EOFError, zlib.error):
            pass

    all_sizes = np.concatenate([np.array(v) for v in sizes.values()])
    all_notional = np.concatenate([np.array(v) for v in notionals.values()])
    header = f"{'series':<20} {'trades':>7} {'mean':>8} {'median':>7} {'p75':>7} {'p90':>8} {'p99':>9} {'mean_usd':>9}"
    print(header)
    for s in sorted(sizes, key = lambda k: -len(sizes[k])):
        a = np.array(sizes[s])
        n = np.array(notionals[s])
        print(f"{s:<20} {len(a):>7} {a.mean():>8.1f} {np.median(a):>7.1f} "
              f"{np.percentile(a, 75):>7.1f} {np.percentile(a, 90):>8.1f} "
              f"{np.percentile(a, 99):>9.1f} {n.mean():>9.2f}")
    print(f"{'ALL':<20} {len(all_sizes):>7} {all_sizes.mean():>8.1f} {np.median(all_sizes):>7.1f} "
          f"{np.percentile(all_sizes, 75):>7.1f} {np.percentile(all_sizes, 90):>8.1f} "
          f"{np.percentile(all_sizes, 99):>9.1f} {all_notional.mean():>9.2f}")


if __name__ == "__main__":
    main()

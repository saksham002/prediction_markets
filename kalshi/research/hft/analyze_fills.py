"""
Slice a sim's fills.csv: markouts by fill reason, by series, by side, and
round-trip / inventory-aging stats. Used to diagnose where passive MM PnL
comes from (queue fills vs cross fills, which venues, adverse selection).

Usage: analyze_fills.py <sim_dir> [sim_dir ...]
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path


def fnum(x):
    return float(x) if x not in ("", None) else None


def analyze(sim_dir: Path):
    fills_path = sim_dir / "fills.csv"
    if not fills_path.exists():
        print(f"{sim_dir}: no fills.csv")
        return
    with open(fills_path, newline = "") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"{sim_dir.name}: 0 fills")
        return

    print(f"\n=== {sim_dir.name} ({len(rows)} fills) ===")

    def bucket_stats(keyfn, label):
        groups = defaultdict(list)
        for r in rows:
            groups[keyfn(r)].append(r)
        print(f"  by {label}:")
        print(f"    {'group':<28} {'fills':>6} {'qty':>8} {'mo5s':>8} {'mo30s':>8} {'mo60s':>8} {'mo300s':>8}")
        for g, rs in sorted(groups.items()):
            qty = sum(float(r["qty"]) for r in rs)
            mos = []
            for h in (5, 30, 60, 300):
                vals = [(fnum(r[f"markout_{h}s"]), float(r["qty"])) for r in rs if fnum(r.get(f"markout_{h}s")) is not None]
                if vals:
                    mos.append(f"{sum(m * q for m, q in vals) / sum(q for _, q in vals):8.3f}")
                else:
                    mos.append(f"{'-':>8}")
            print(f"    {str(g):<28} {len(rs):>6} {qty:>8.0f} {' '.join(mos)}")

    bucket_stats(lambda r: r["reason"], "reason")
    bucket_stats(lambda r: r["ticker"].split("-", 1)[0], "series")
    bucket_stats(lambda r: r["side"], "side")

    # Round trips per ticker: qty matched between buy-side (yes) and sell-side (no)
    bought = defaultdict(float)
    sold = defaultdict(float)
    for r in rows:
        if r["side"] in ("yes", "buy"):
            bought[r["ticker"]] += float(r["qty"])
        else:
            sold[r["ticker"]] += float(r["qty"])
    rt = sum(min(bought[t], sold[t]) for t in set(bought) | set(sold))
    open_qty = sum(abs(bought[t] - sold[t]) for t in set(bought) | set(sold))
    print(f"  round-trip contracts: {rt:.0f}   open at end: {open_qty:.0f}")
    last = rows[-1]
    print(f"  final realized={last['realized_pnl']}  fees={last['fees_paid']}")


def main():
    for arg in sys.argv[1:]:
        analyze(Path(arg))


if __name__ == "__main__":
    main()

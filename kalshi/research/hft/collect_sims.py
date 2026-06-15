"""
Collect all sim summary.csv files under /data/user_data/saksham3/kalshi_hft/sims/
into one table, print sorted by net_pnl, and write combined CSV.
"""

import csv
from pathlib import Path

SIMS_DIR = Path("/data/user_data/saksham3/kalshi_hft/sims")


def main():
    rows = []
    for summary_path in sorted(SIMS_DIR.glob("*/summary.csv")):
        with open(summary_path, newline = "") as f:
            for row in csv.DictReader(f):
                row["tag"] = summary_path.parent.name
                rows.append(row)
    if not rows:
        print("No summaries found.")
        return

    fields = ["tag"] + [k for k in rows[0] if k != "tag"]
    out_path = SIMS_DIR / "all_summaries.csv"
    with open(out_path, "w", newline = "") as f:
        w = csv.DictWriter(f, fieldnames = fields, extrasaction = "ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {out_path} ({len(rows)} sims)\n")

    rows.sort(key = lambda r: -float(r["net_pnl"]))
    print(f"{'tag':<58} {'fills':>6} {'gross':>9} {'fees':>8} {'net':>9} {'mo5s':>7} {'mo60s':>7}")
    print("-" * 110)
    for r in rows:
        print(f"{r['tag']:<58} {r['n_fills']:>6} {float(r['realized_pnl']):>9.2f} "
              f"{float(r['fees_paid']):>8.2f} {float(r['net_pnl']):>9.2f} "
              f"{r['markout_5s_cents']:>7} {r['markout_60s_cents']:>7}")


if __name__ == "__main__":
    main()

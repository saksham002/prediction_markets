"""Restricted WC FootballStrategy sweep: OBI ONLY (no gating) under a real
$1000 deployed-capital budget. Unlike wc_sweep.py (budget off -> position limit
binds), here the global deployed-dollars cap is the deploy-realistic $1000, so
capital is the binding constraint. In-sample = first 4 WC games, out = last 4.

Usage: wc_sweep_obi.py --shard I --num-shards N   |   wc_sweep_obi.py --collect
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_buffer import run_one

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
RESULTS = Path("/data/user_data/saksham3/kalshi_hft/sims/wc_sweep_obi")
BEST_CFG = Path("/data/user_data/saksham3/kalshi_hft/studies/wc_obi_budget_best_config.json")
BUDGET = 1000.0   # real $1000 deployed-capital cap -> capital is the binding constraint

# obi |alpha| percentiles {50,75,90,95,99} (same wc_thresholds.json as the full
# sweep; obi saturates at 1.0 so p90+ collapse) -> deduped.
_PCT = json.load(open(Path("/data/user_data/saksham3/kalshi_hft/studies/wc_thresholds.json")))
THRS = sorted(set(_PCT["obi"].values()))
SIZES = [10, 50, 200]
CAPS = [10, 50, 200, 1000, 5000]

COMBOS = [("obi", "obi", thr, s, cap)
          for thr in THRS for s in SIZES for cap in CAPS if s <= cap]


def wc_games():
    return sorted(DATASET.glob("KXWCGAME*.jsonl.gz"))   # chronological by ticker date


def _agg(games, cfg):
    r = {"net": 0.0, "realized_net": 0.0, "fills": 0, "fees": 0.0, "n": 0}
    for g in games:
        row = run_one(g, "KXWCGAME", cfg)
        if row is None:
            continue
        r["net"] += row["net_pnl"]
        r["realized_net"] += row["realized_pnl"] - row["fees_paid"]
        r["fills"] += row["n_fills"]
        r["fees"] += row["fees_paid"]
        r["n"] += 1
    return r


def run_shard(shard, n):
    RESULTS.mkdir(parents = True, exist_ok = True)
    games = wc_games()
    inn, out = games[:4], games[4:8]
    for idx, (label, alpha, thr, s, cap) in enumerate(COMBOS):
        if idx % n != shard:
            continue
        f = RESULTS / f"wc_obi_t{thr:g}_s{s}_c{cap}.json"
        if f.exists():
            continue
        cfg = {"alpha_name": alpha, "skew_threshold": thr, "per_order_size": s,
               "inventory_cap": cap, "budget": BUDGET, "football": True}
        res = {"label": label, "alpha": alpha, "thr": thr, "size": s, "cap": cap,
               "in": _agg(inn, cfg), "out": _agg(out, cfg)}
        with open(f, "w") as fh:
            json.dump(res, fh)
        print(f"obi t{thr:<7g} s{s:<4} c{cap:<5} in_net={res['in']['net']:+8.1f} "
              f"out_net={res['out']['net']:+8.1f}", flush = True)


def collect():
    rows = [json.load(open(p)) for p in sorted(RESULTS.glob("wc_obi_*.json"))]
    print(f"{len(rows)}/{len(COMBOS)} combos done  (BUDGET=${BUDGET:g})\n")
    print(f"{'thr':>8} {'size':>5} {'cap':>5} {'IN_net':>9} {'OUT_net':>9} "
          f"{'IN_rn':>9} {'OUT_rn':>9} {'in_fills':>8}")
    for r in sorted(rows, key = lambda r: -r["in"]["net"]):
        print(f"{r['thr']:>8g} {r['size']:>5g} {r['cap']:>5g} "
              f"{r['in']['net']:>+9.1f} {r['out']['net']:>+9.1f} "
              f"{r['in']['realized_net']:>+9.1f} {r['out']['realized_net']:>+9.1f} {r['in']['fills']:>8}")
    if rows:
        bo = max(rows, key = lambda r: r["in"]["net"])
        BEST_CFG.write_text(json.dumps(
            {"alpha": bo["alpha"], "thr": bo["thr"], "size": bo["size"],
             "cap": bo["cap"], "budget": BUDGET}))
        print(f"\nOVERALL best-in: obi thr={bo['thr']:g} s={bo['size']} cap={bo['cap']} "
              f"-> IN net {bo['in']['net']:+.1f} / OUT net {bo['out']['net']:+.1f} "
              f"(realized-net IN {bo['in']['realized_net']:+.1f} / OUT {bo['out']['realized_net']:+.1f})")
        print(f"wrote {BEST_CFG}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type = int, default = 0)
    ap.add_argument("--num-shards", type = int, default = 1)
    ap.add_argument("--collect", action = "store_true")
    a = ap.parse_args()
    collect() if a.collect else run_shard(a.shard, a.num_shards)


if __name__ == "__main__":
    main()

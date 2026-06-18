"""WC FootballStrategy sweep: train (first 12 WC games, chronological) vs test
(next 8) for blind, raw obi, NORMALIZED flow (agg_ratio / tfma_pw_ratio =
net/gross flow imbalance, scale-free so thresholds transfer across games), and
obi-deviation (obi - obi_ma at 5s/60s/300s), over order size, position limit,
and alpha threshold. Per-run deployed-capital `--budget` (run $1000 and $250);
results -> sims/wc_sweep_r<budget>/, best config -> studies/wc_best_config_r<budget>.json.
Reports in (train) vs out (test) net PnL.

PROD-FAITHFUL execution (REALISTIC, 2026-06-17): SimExchange AWS feed delays
(REALISTIC_DELAYS: ack 22ms / pub 28ms / fill 16ms) + the in-flight lock (no new
order/cancel on a side while one is in flight) + 20ms forward fill latency. This
materially lowers captured PnL vs the optimistic (delays=0) wc_sweep_88.

Usage: wc_sweep.py --shard I --num-shards N   |   wc_sweep.py --collect
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_buffer import run_one
from exchange import REALISTIC_DELAYS

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
SIMS = Path("/data/user_data/saksham3/kalshi_hft/sims")
STUDIES = Path("/data/user_data/saksham3/kalshi_hft/studies")
N_TRAIN = 20      # 20 in-sample / 4 out-sample (24-game dataset, chronological)
FORWARD_DELAY = 0.020   # 20ms forward latency to gate fills
RESULTS_SUFFIX = "_r204"   # restricted obi_dev sweep, 20-4 split (fresh dir)
PCTS = [75, 90, 95]        # skew-threshold percentiles (of |alpha|, on the in-sample)
SWEEP_ALPHAS = ["obi_dev_60s", "obi_dev_300s"]
SIZES = [10, 50, 200]
CAPS = [50, 200, 1000]
# budget is per-run (--budget); results -> sims/wc_sweep_r<budget>_r204/


def _build_combos():
    """(label, alpha, thr, size, cap) grid. Thresholds = {PCTS} percentiles of
    |alpha| computed on the SAME in-sample as the sweep (wc_games()[:N_TRAIN]) via
    the game-set-keyed cache (threshold_cache.get_thresholds) — never a hardcoded
    game list, never recomputed when the in-sample is unchanged."""
    from threshold_cache import get_thresholds
    pct = get_thresholds(wc_games()[:N_TRAIN], SWEEP_ALPHAS, PCTS)
    out = []
    for a in SWEEP_ALPHAS:
        for thr in sorted({pct[a][str(p)] for p in PCTS}):
            for s in SIZES:
                for cap in CAPS:
                    if s <= cap:
                        out.append((a, a, thr, s, cap))
    return out


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


def run_shard(shard, n, budget, results):
    results.mkdir(parents = True, exist_ok = True)
    games = wc_games()
    inn, out = games[:N_TRAIN], games[N_TRAIN:]    # 20 train / 4 test (chronological)
    for idx, (label, alpha, thr, s, cap) in enumerate(_build_combos()):
        if idx % n != shard:
            continue
        f = results / f"wc_{label}_t{thr:g}_s{s}_c{cap}.json"
        if f.exists():
            continue
        cfg = {"alpha_name": alpha, "skew_threshold": thr, "per_order_size": s,
               "inventory_cap": cap, "budget": budget, "football": True,
               "forward_delay": FORWARD_DELAY, **REALISTIC_DELAYS}
        res = {"label": label, "alpha": alpha, "thr": thr, "size": s, "cap": cap,
               "in": _agg(inn, cfg), "out": _agg(out, cfg)}
        with open(f, "w") as fh:
            json.dump(res, fh)
        print(f"{label:>6} t{thr:<7g} s{s:<4} c{cap:<5} in_net={res['in']['net']:+8.1f} "
              f"out_net={res['out']['net']:+8.1f}", flush = True)


def collect(results, budget):
    rows = [json.load(open(p)) for p in sorted(results.glob("wc_*.json"))]
    print(f"{len(rows)}/{len(_build_combos())} combos done\n")
    print(f"{'label':>12} {'thr':>8} {'size':>5} {'cap':>5} {'IN_net':>9} {'OUT_net':>9} "
          f"{'IN_rn':>9} {'OUT_rn':>9} {'in_fills':>8}")
    for r in sorted(rows, key = lambda r: -r["out"]["net"]):       # rank by OOS net
        print(f"{r['label']:>12} {r['thr']:>8g} {r['size']:>5g} {r['cap']:>5g} "
              f"{r['in']['net']:>+9.1f} {r['out']['net']:>+9.1f} "
              f"{r['in']['realized_net']:>+9.1f} {r['out']['realized_net']:>+9.1f} {r['in']['fills']:>8}")
    print("\n=== per-alpha: best OUT-sample config ===")
    for a in SWEEP_ALPHAS:
        sub = [r for r in rows if r["label"] == a]
        if sub:
            b = max(sub, key = lambda r: r["out"]["net"])
            print(f"{a:>12}: best-out thr={b['thr']:g} s={b['size']} cap={b['cap']} -> "
                  f"OUT net {b['out']['net']:+.1f} (OUT rn {b['out']['realized_net']:+.1f}, IN net {b['in']['net']:+.1f})")
    if rows:
        bo = max(rows, key = lambda r: r["out"]["net"])            # DEPLOY = best OOS net
        brn = max(rows, key = lambda r: r["out"]["realized_net"])
        (STUDIES / f"wc_best_config_r{int(budget)}{RESULTS_SUFFIX}.json").write_text(
            json.dumps({"alpha": bo["alpha"], "thr": bo["thr"], "size": bo["size"], "cap": bo["cap"]}))
        print(f"\nBEST OOS net (-> deploy file): {bo['label']} thr={bo['thr']:g} s={bo['size']} "
              f"cap={bo['cap']} -> OUT net {bo['out']['net']:+.1f} / OUT rn {bo['out']['realized_net']:+.1f} "
              f"(IN net {bo['in']['net']:+.1f})")
        print(f"BEST OOS realized-net: {brn['label']} thr={brn['thr']:g} s={brn['size']} "
              f"cap={brn['cap']} -> OUT rn {brn['out']['realized_net']:+.1f} / OUT net {brn['out']['net']:+.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type = int, default = 0)
    ap.add_argument("--num-shards", type = int, default = 1)
    ap.add_argument("--budget", type = float, default = 1000.0)
    ap.add_argument("--collect", action = "store_true")
    ap.add_argument("--prep", action = "store_true",
                    help = "compute + cache the in-sample thresholds (warm before sharding)")
    a = ap.parse_args()
    results = SIMS / f"wc_sweep_r{int(a.budget)}{RESULTS_SUFFIX}"
    if a.prep:
        _build_combos()                          # triggers get_thresholds (compute + cache)
    elif a.collect:
        collect(results, a.budget)
    else:
        run_shard(a.shard, a.num_shards, a.budget, results)


if __name__ == "__main__":
    main()

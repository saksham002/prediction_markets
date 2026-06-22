"""Generic WC strategy sweep.

Runs a SET of StrategyConfigs over an in-sample and an out-sample game set with
prod-faithful SimExchange execution (REALISTIC_DELAYS + in-flight lock + 20ms
forward fill latency), PERSISTS the per-(config, game) PnLs, and picks the best
in-sample and best out-sample config FROM that store via a pluggable scoring
function (default = mean per-game net). There is NO separate --collect re-run:
`--finalize` reads the stored per-game PnLs (no sims re-run) and writes the
best-in / best-out deploy specs.

The runner (`run_shard` / `best_configs` / `finalize`) is generic over
(in_games, out_games, configs, out_dir, score_fn). The DEFAULT study wired below
is: obi_dev alone vs obi_dev AND agg_dev, over half-lives + percentile thresholds,
$250 budget, free_budget on, size 200 / cap 1000, 21-6 chronological split. Edit
`build_default_configs()` / the constants to sweep something else.

Usage: wc_sweep.py --prep | --shard I --num-shards N | --finalize
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_buffer import run_one
from exchange import REALISTIC_DELAYS

from paths import DATASET, SIMS, STUDIES

# ---- default study: obi_dev vs obi_dev AND agg_dev ----
N_TRAIN = 21                                  # 21 in-sample / 6 out-sample (27-game set)
RESULTS_DIR = SIMS / "wc_sweep_aggdev216"     # fresh dir (per-config store)
FORWARD_DELAY = 0.020
BUDGET = 250
FREE_BUDGET = True
SIZE, CAP = 200, 1000
PCTS = [50, 75, 90]                           # per-alpha |alpha| percentiles on the in-sample
OBI_HLS = [10, 60, 300]                       # obi_dev half-lives (s)
AGG_HLS = [1, 10, 60]                         # agg_dev half-lives (s)


def wc_games():
    return sorted(DATASET.glob("KXWCGAME*.jsonl.gz"))   # chronological by ticker date


def default_splits():
    games = wc_games()
    return games[:N_TRAIN], games[N_TRAIN:]             # 21 train / 6 test (chronological)


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _bps(games):
    """PnL efficiency: total net / total dollar volume traded, in bps (edge per $
    transacted). `games` is a {stem: {net, volume, ...}} per-game dict."""
    vol = sum(g.get("volume", 0.0) for g in games.values())
    net = sum(g["net"] for g in games.values())
    return net / vol * 1e4 if vol else 0.0


def build_default_configs():
    """36 configs as a list of (label, cfg_dict). Arm A: obi_dev alone (3 HL x 3 pct).
    Arm B: obi_dev AND agg_dev (3 obi_hl x 3 agg_hl x 3 pct). All gates symmetric;
    thresholds = each alpha's pct-percentile of |alpha| on the in-sample (same pct
    selects both alphas' percentiles in a combo)."""
    from threshold_cache import get_thresholds
    inn, _ = default_splits()
    obi_names = [f"obi_dev_{h}s" for h in OBI_HLS]
    agg_names = [f"agg_dev_{h}s" for h in AGG_HLS]
    thr = get_thresholds(inn, obi_names + agg_names, PCTS)
    base = {"per_order_size": SIZE, "inventory_cap": CAP, "budget": BUDGET,
            "football": True, "free_budget": FREE_BUDGET,
            "forward_delay": FORWARD_DELAY, **REALISTIC_DELAYS}
    configs = []
    for oh in OBI_HLS:                                   # arm A: obi_dev only
        for p in PCTS:
            gates = [{"family": "obi_dev", "hl": oh, "threshold": thr[f"obi_dev_{oh}s"][str(p)]}]
            configs.append((f"A_obi{oh}s_p{p}", {**base, "alphas": gates}))
    for oh in OBI_HLS:                                   # arm B: obi_dev AND agg_dev
        for ah in AGG_HLS:
            for p in PCTS:
                gates = [{"family": "obi_dev", "hl": oh, "threshold": thr[f"obi_dev_{oh}s"][str(p)]},
                         {"family": "agg_dev", "hl": ah, "threshold": thr[f"agg_dev_{ah}s"][str(p)]}]
                configs.append((f"B_obi{oh}s_agg{ah}s_p{p}", {**base, "alphas": gates}))
    return configs


def _per_game(games, cfg):
    """{stem: {net, realized_net, fills}} for one config over `games`."""
    out = {}
    for g in games:
        row = run_one(g, "KXWCGAME", cfg)
        if row is None:
            continue
        out[g.stem.replace(".jsonl", "")] = {
            "net": round(row["net_pnl"], 4),
            "realized_net": round(row["realized_pnl"] - row["fees_paid"], 4),
            "fills": row["n_fills"],
            "volume": row["volume"]}
    return out


def run_shard(shard, n, configs, inn, out, results):
    """Run this shard's slice of `configs`, writing one JSON per config holding the
    per-(config, game) PnLs for the in and out sets. Resume/preempt-safe (skips
    already-written configs)."""
    results.mkdir(parents = True, exist_ok = True)
    for idx, (label, cfg) in enumerate(configs):
        if idx % n != shard:
            continue
        f = results / f"wc_{label}.json"
        if f.exists():
            continue
        res = {"label": label, "alphas": cfg["alphas"],
               "in": {"games": _per_game(inn, cfg)},
               "out": {"games": _per_game(out, cfg)}}
        f.write_text(json.dumps(res))
        in_net = _mean([g["net"] for g in res["in"]["games"].values()])
        out_net = _mean([g["net"] for g in res["out"]["games"].values()])
        print(f"{label:>24} in_net/g={in_net:+8.2f} out_net/g={out_net:+8.2f} "
              f"in_bps={_bps(res['in']['games']):+6.1f} out_bps={_bps(res['out']['games']):+6.1f}",
              flush = True)


def best_configs(results, score_fn=None):
    """Read the per-(config, game) store and return (best_in, best_out), each a
    (row, score) tuple. `score_fn(list_of_per_game_net) -> float` decides "best"
    (default = mean). Applied to the in-sample and out-sample arrays independently."""
    score_fn = score_fn or _mean
    rows = [json.loads(p.read_text()) for p in sorted(results.glob("wc_*.json"))]
    if not rows:
        return None, None

    def score(row, key):
        vals = [g["net"] for g in row[key]["games"].values()]
        return score_fn(vals) if vals else float("-inf")

    best_in = max(rows, key = lambda r: score(r, "in"))
    best_out = max(rows, key = lambda r: score(r, "out"))
    return (best_in, score(best_in, "in")), (best_out, score(best_out, "out"))


def finalize(results, score_fn=None):
    """Rank configs by per-game OOS net, and write the best-in / best-out deploy
    specs from the stored PnLs (no sims re-run)."""
    bi, bo = best_configs(results, score_fn)
    if bi is None:
        print(f"no results in {results}")
        return
    rows = [json.loads(p.read_text()) for p in sorted(results.glob("wc_*.json"))]
    pg = lambda r, k: _mean([g["net"] for g in r[k]["games"].values()])
    print(f"{len(rows)} configs in {results.name}\n")
    print(f"{'label':>24} {'IN_net/g':>9} {'OUT_net/g':>9} {'IN_bps':>7} {'OUT_bps':>7} {'in_fills':>9}")
    for r in sorted(rows, key = lambda r: -pg(r, "out")):
        in_fills = sum(g["fills"] for g in r["in"]["games"].values())
        print(f"{r['label']:>24} {pg(r, 'in'):>+9.2f} {pg(r, 'out'):>+9.2f} "
              f"{_bps(r['in']['games']):>+7.1f} {_bps(r['out']['games']):>+7.1f} {in_fills:>9}")
    (bi_row, bi_s), (bo_row, bo_s) = bi, bo
    STUDIES.mkdir(parents = True, exist_ok = True)
    for tag, row in (("in", bi_row), ("out", bo_row)):
        spec = {"alphas": row["alphas"], "per_order_size": SIZE, "inventory_cap": CAP,
                "budget": BUDGET, "free_budget": FREE_BUDGET, "football": True}
        (STUDIES / f"wc_best_{tag}_{results.name}.json").write_text(json.dumps(spec))
    print(f"\nBEST IN  ({bi_s:+.2f}/g): {bi_row['label']}  {bi_row['alphas']}")
    print(f"BEST OUT ({bo_s:+.2f}/g): {bo_row['label']}  {bo_row['alphas']}")
    print(f"deploy specs -> {STUDIES}/wc_best_in_{results.name}.json , wc_best_out_{results.name}.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type = int, default = 0)
    ap.add_argument("--num-shards", type = int, default = 1)
    ap.add_argument("--prep", action = "store_true",
                    help = "compute + cache the in-sample thresholds (warm before sharding)")
    ap.add_argument("--finalize", action = "store_true",
                    help = "pick best-in / best-out from the stored per-game PnLs (no sims)")
    a = ap.parse_args()
    if a.prep:
        build_default_configs()                          # triggers get_thresholds (compute + cache)
    elif a.finalize:
        finalize(RESULTS_DIR)
    else:
        inn, out = default_splits()
        run_shard(a.shard, a.num_shards, build_default_configs(), inn, out, RESULTS_DIR)


if __name__ == "__main__":
    main()

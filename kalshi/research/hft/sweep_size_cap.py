"""Size x cap x threshold sweep for {agg_300s, tfma_pw_300s, obi} on MLB.

Thresholds are the train |alpha| percentiles computed by agg_mlb_eval.py
(train-MLB pool, 2026-06-12) — no re-sampling needed. Each (alpha, thr, size,
cap) combo is one JSON result file -> resumable, shardable across workers.

Usage: sweep_size_cap.py --shard I --num-shards N
Collect: sweep_size_cap.py --collect
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_buffer import run_one
try:
    from research.hft.paths import DATASET, SIMS, STUDIES
except ImportError:
    from paths import DATASET, SIMS, STUDIES

RESULTS = SIMS / "size_cap_sweep"
THR_JSON = STUDIES / "mlb_thresholds.json"

# Train-MLB |alpha| percentiles, recomputed per dataset by compute_thresholds.py.
# Fall back to the Jun-12 hardcoded values only if the json is absent.
if THR_JSON.exists():
    with open(THR_JSON) as f:
        THRESHOLDS = json.load(f)
else:
    THRESHOLDS = {
        "agg_300s": [0.0, 9298.46, 23376.3, 49391.0, 70152.9],
        "tfma_pw_300s": [0.0, 14619.0, 31070.4, 62063.5, 91714.4],
        "obi": [0.0, 0.775275, 1.0],
    }
SIZES = [100, 500, 1000]
CAPS = [300, 1000, 3000]
SQUARE_OFF = [False, True]   # MM stance: ride signal-aligned inventory vs flatten

COMBOS = [(alpha, thr, s, cap, sq)
          for alpha, thrs in THRESHOLDS.items()
          for thr in thrs
          for s in SIZES
          for cap in CAPS
          for sq in SQUARE_OFF]


def _combo_key(alpha, thr, s, cap, sq) -> str:
    # content-based result name: resumable across grid changes
    return f"r_{alpha}_t{thr:g}_s{s:g}_c{cap:g}_sq{int(sq)}.json"


def run_shard(shard: int, num_shards: int):
    RESULTS.mkdir(parents = True, exist_ok = True)
    recordings = [r for r in sorted(DATASET.glob("*.jsonl.gz")) if r.stat().st_size > 10000]
    for idx, (alpha, thr, s, cap, sq) in enumerate(COMBOS):
        if idx % num_shards != shard:
            continue
        out = RESULTS / _combo_key(alpha, thr, s, cap, sq)
        if out.exists():
            continue
        cfg = {"alphas": [{"name": alpha, "threshold": thr}], "per_order_size": s,
               "inventory_cap": cap, "budget": 1000, "square_off": sq}
        agg = {"alpha": alpha, "thr": thr, "size": s, "cap": cap, "square_off": sq,
               "train_realized_net": 0.0, "test_realized_net": 0.0,
               "train_net": 0.0, "test_net": 0.0, "n_fills": 0, "fees": 0.0}
        for rec in recordings:
            row = run_one(rec, "KXMLBGAME", cfg)
            if row is None:
                continue
            for k in ("train_realized_net", "test_realized_net", "train_net", "test_net"):
                agg[k] += row[k]
            agg["n_fills"] += row["n_fills"]
            agg["fees"] += row["fees_paid"]
        with open(out, "w") as f:
            json.dump(agg, f)
        print(f"[{idx:3d}] {alpha} thr={thr:g} s={s} cap={cap} sq={int(sq)}: "
              f"train_rn={agg['train_realized_net']:+.2f} test_rn={agg['test_realized_net']:+.2f}",
              flush = True)


def collect():
    rows = []
    for p in sorted(RESULTS.glob("r_*.json")):
        with open(p) as f:
            rows.append(json.load(f))
    print(f"{len(rows)}/{len(COMBOS)} combos done")
    rows.sort(key = lambda r: -r["train_realized_net"])
    print(f"\n{'alpha':>14} {'thr':>9} {'size':>5} {'cap':>5} {'sq':>3} {'train_rn':>9} {'test_rn':>9} "
          f"{'train_net':>10} {'test_net':>9} {'fills':>6}")
    for r in rows:
        print(f"{r['alpha']:>14} {r['thr']:>9.6g} {r['size']:>5g} {r['cap']:>5g} {int(r.get('square_off', 0)):>3} "
              f"{r['train_realized_net']:>+9.2f} {r['test_realized_net']:>+9.2f} "
              f"{r['train_net']:>+10.2f} {r['test_net']:>+9.2f} {r['n_fills']:>6}")
    print("\n=== per-alpha best by TRAIN realized-net (decision metric = TEST) ===")
    for alpha in THRESHOLDS:
        sub = [r for r in rows if r["alpha"] == alpha]
        if sub:
            b = sub[0]
            print(f"{alpha}: thr={b['thr']:g} s={b['size']:g} cap={b['cap']:g} sq={int(b.get('square_off',0))} -> "
                  f"train_rn={b['train_realized_net']:+.2f} TEST_rn={b['test_realized_net']:+.2f} "
                  f"test_net={b['test_net']:+.2f}")
    print("\n=== square_off comparison (best TRAIN config per stance) ===")
    for sq in (0, 1):
        sub = [r for r in rows if int(r.get("square_off", 0)) == sq]
        if sub:
            b = sub[0]
            print(f"  square_off={sq}: {b['alpha']} thr={b['thr']:g} s={b['size']:g} cap={b['cap']:g} -> "
                  f"train_rn={b['train_realized_net']:+.2f} TEST_rn={b['test_realized_net']:+.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type = int, default = 0)
    parser.add_argument("--num-shards", type = int, default = 1)
    parser.add_argument("--collect", action = "store_true")
    args = parser.parse_args()
    if args.collect:
        collect()
    else:
        run_shard(args.shard, args.num_shards)


if __name__ == "__main__":
    main()

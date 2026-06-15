"""
Generate eval_buffer config files for a PnL-driven search over weighted alpha
combinations and skew thresholds.

Combination alpha = sum_i w_i * (alpha_i - mean_i) / std_i over the verified
core set {obi, tfma_pw_10s}. Momentum alphas are excluded from all config
sweeps (user rule 2026-06-12: mom lags price by construction).
Standardization stats come from a
fit_combo.py JSON (its means/stds were computed on recorded samples), so a
threshold of 1.0 means "one standard deviation of combined signal".

Outputs:
  <out_dir>/cfg_<idx>.json      eval_buffer per-league config
  <out_dir>/manifest.csv        idx -> weights/threshold mapping
"""

import argparse
import csv
import itertools
import json
from pathlib import Path

CORE_ALPHAS = ["obi", "tfma_pw_10s"]
WEIGHT_GRID = [0.0, 0.5, 1.0]
THRESHOLDS = [0.0, 0.25, 0.5, 1.0]


def main():
    parser = argparse.ArgumentParser(description = "Generate combo-grid configs for eval_buffer")
    parser.add_argument("--stats-json", required = True,
                        help = "fit_combo.py output JSON (means/stds source)")
    parser.add_argument("--out-dir", required = True)
    parser.add_argument("--series", default = "KXMLBGAME,KXNBAGAME,KXNHLGAME,KXNFLGAME")
    parser.add_argument("--size", type = float, default = 1000)
    parser.add_argument("--cap", type = float, default = 3000)
    args = parser.parse_args()

    with open(args.stats_json) as f:
        stats = json.load(f)
    means = {a: stats["means"][a] for a in CORE_ALPHAS}
    stds = {a: stats["stds"][a] for a in CORE_ALPHAS}

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents = True, exist_ok = True)
    series_list = [s.strip() for s in args.series.split(",") if s.strip()]

    manifest = []
    idx = 0
    for weights in itertools.product(WEIGHT_GRID, repeat = len(CORE_ALPHAS)):
        if all(w == 0 for w in weights):
            continue
        w_map = dict(zip(CORE_ALPHAS, weights))
        for threshold in THRESHOLDS:
            combo = {"weights": w_map, "means": means, "stds": stds}
            league_cfg = {
                "alpha_name": "combo",
                "combo": combo,
                "skew_threshold": threshold,
                "per_order_size": args.size,
                "inventory_cap": args.cap,
                "pair_risk": True,
            }
            cfg = {series: league_cfg for series in series_list}
            cfg_path = out_dir / f"cfg_{idx:03d}.json"
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent = 1)
            manifest.append({
                "idx": idx,
                "config": cfg_path.name,
                "threshold": threshold,
                **{f"w_{a}": w for a, w in w_map.items()},
            })
            idx += 1

    with open(out_dir / "manifest.csv", "w", newline = "") as f:
        w = csv.DictWriter(f, fieldnames = list(manifest[0].keys()))
        w.writeheader()
        w.writerows(manifest)
    print(f"Wrote {idx} configs + manifest.csv to {out_dir}")


if __name__ == "__main__":
    main()

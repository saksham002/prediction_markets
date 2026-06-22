"""
Evaluate passive MM configs over the whole buffer of tick recordings, with
per-league (per-series) parameters — same parameters for every game within a
league, as required.

Config JSON maps series ticker -> mm_sim-style params:

  {
    "KXMLBGAME":  {"alpha_name": "agree_om", "per_order_size": 1000,
                   "inventory_cap": 3000, "skew_threshold": 0, "pair_risk": true},
    "KXNBAGAME":  {"alpha_name": "agree_om", "per_order_size": 500, ...}
  }

Every recording in --ticks-dir is replayed once per configured league (the
series filter isolates that league's pairs). Outputs one row per
(recording, league) plus totals, to --out CSV.
"""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.mm_sim import MMSimConsumer, compute_markouts, MARKOUT_HORIZONS_S
from research.hft.replay import Replayer
from research.hft.passive_fill import FORWARD_DELAY_S

TICKS_DIR = Path("/data/user_data/saksham3/kalshi_hft/ticks")
PARAM_DEFAULTS = {
    "per_order_size": 1000,
    "inventory_cap": 3000,
    "skew_threshold": 0.0,
    "alpha_name": "agree_om",
    # new symmetric multi-alpha gate spec (list of {name|family, hl, threshold}); when
    # present it supersedes alpha_name/skew_threshold (StrategyConfig.from_params).
    "alphas": None,
    "max_spread": 0.01,
    "price_min": 0.05,
    "price_max": 0.95,
    "improve": False,
    "pair_risk": True,
    "square_off": False,
    "combo": None,
    "forward_delay": FORWARD_DELAY_S,
    "size_ref": None,
    "max_queue_ahead": None,
    "per_leg_alpha": False,
    # SimExchange feed delays (s): 0 = equivalence test (synchronous, lock non-binding);
    # AWS-realistic values for prod-faithful runs (ack=place RTT, pub=ack+PRIV->PUB
    # lag, fill_delay=WS one-way). See REALISTIC_DELAYS in exchange usage.
    "ack_delay": 0.0,
    "pub_delay": 0.0,
    "fill_delay": 0.0,
    "fill_pub_lag": 0.0,
    "budget": 1000,
    "free_budget": False,
    "losing_leg_bias": 0.0,
    "aggro_entry": None,
    "aggro_limit": 300,
    "aggro_profit": 0.02,
    "aggro_stop": 0.05,
    "aggro_cross": False,
    "aggro_neg": None,
    "football": False,
}


def is_test_game(event_ticker: str) -> bool:
    """Deterministic 80-20 split BY GAME, identical for every config; new
    games auto-assign consistently. Decisions are made on the TEST set."""
    return int(hashlib.md5(event_ticker.encode()).hexdigest(), 16) % 5 == 0


def run_one(recording: Path, series: str, cfg: dict) -> dict | None:
    params = SimpleNamespace(**{**PARAM_DEFAULTS, **cfg, "series": series})
    replayer = Replayer(recording)
    consumer = MMSimConsumer(replayer, params)
    replayer.run(consumer)
    if not consumer.strategies:
        return None
    compute_markouts(consumer)

    pnl = consumer.pnl
    last_mids = {t: h[-1][1] for t, h in consumer.mid_history.items() if h}

    # Per-game PnL attribution -> train/test aggregation (split by event)
    split = {"train": {"real": 0.0, "fees": 0.0, "mark": 0.0, "n": 0},
             "test": {"real": 0.0, "fees": 0.0, "mark": 0.0, "n": 0}}
    for key, mm in consumer.strategies.items():
        event = mm.pair["event_ticker"] if mm.second_ticker is not None else mm.event_ticker
        bucket = split["test" if is_test_game(event) else "train"]
        bucket["n"] += 1
        tickers = ([mm.first_ticker, mm.second_ticker]
                   if mm.second_ticker is not None else [mm.ticker])
        for t in tickers:
            bucket["real"] += pnl.realized_by_ticker.get(t, 0.0)
            bucket["fees"] += pnl.fees_by_ticker.get(t, 0.0)
            pos = pnl.positions.get(t)
            if pos is not None and t in last_mids:
                direction = 1 if pos.side == "long" else -1
                bucket["mark"] += pos.qty * (last_mids[t] - pos.avg_price) * direction
    vals = [(r["markout_30s"], r["qty"]) for r in consumer.fill_rows if r.get("markout_30s", "") != ""]
    mo30 = sum(m * q for m, q in vals) / sum(q for _, q in vals) if vals else float("nan")
    duration_h = 0.0
    if consumer.mid_history:
        all_ts = [h[0] for hist in consumer.mid_history.values() for h in (hist[0], hist[-1])]
        duration_h = (max(all_ts) - min(all_ts)) / 3600
    return {
        "recording": recording.name,
        "series": series,
        "n_pairs": len(consumer.strategies),
        "n_fills": len(consumer.fill_rows),
        "contracts": round(sum(r["qty"] for r in consumer.fill_rows), 1),
        # gross dollar volume traded = sum(execution price in the side's own space * qty)
        # over every fill; PnL/volume (in bps) = edge captured per dollar transacted.
        "volume": round(sum(r["price"] * r["qty"] for r in consumer.fill_rows), 2),
        "realized_pnl": round(pnl.realized_pnl, 4),
        "fees_paid": round(pnl.fees_paid, 4),
        "net_pnl": round(pnl.net_total_pnl(prices = last_mids), 4),
        "peak_deployed": round(consumer.peak_deployed, 2),
        "duration_h": round(duration_h, 2),
        "markout_30s_cents": round(mo30, 4) if mo30 == mo30 else "",
        "n_train": split["train"]["n"],
        "n_test": split["test"]["n"],
        "train_realized_net": round(split["train"]["real"] - split["train"]["fees"], 4),
        "test_realized_net": round(split["test"]["real"] - split["test"]["fees"], 4),
        "train_net": round(split["train"]["real"] - split["train"]["fees"] + split["train"]["mark"], 4),
        "test_net": round(split["test"]["real"] - split["test"]["fees"] + split["test"]["mark"], 4),
    }


def main():
    parser = argparse.ArgumentParser(description = "Evaluate per-league configs over the recording buffer")
    parser.add_argument("--config", required = True, help = "JSON: series -> params")
    parser.add_argument("--ticks-dir", default = str(TICKS_DIR))
    parser.add_argument("--out", default = None, help = "Output CSV path")
    args = parser.parse_args()

    with open(args.config) as f:
        league_cfgs = json.load(f)

    recordings = sorted(Path(args.ticks_dir).glob("*.jsonl.gz"))
    recordings = [r for r in recordings if r.stat().st_size > 10000]
    print(f"{len(recordings)} recordings x {len(league_cfgs)} leagues")

    rows = []
    for rec in recordings:
        for series, cfg in league_cfgs.items():
            row = run_one(rec, series, cfg)
            if row is None:
                continue
            rows.append(row)
            print(f"  {rec.name} {series}: fills={row['n_fills']} net={row['net_pnl']} "
                  f"realized={row['realized_pnl']} peak=${row['peak_deployed']}")

    if not rows:
        print("No results.")
        return

    out_path = Path(args.out) if args.out else Path(args.ticks_dir).parent / "buffer_eval.csv"
    with open(out_path, "w", newline = "") as f:
        w = csv.DictWriter(f, fieldnames = list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    total_net = sum(r["net_pnl"] for r in rows)
    total_realized = sum(r["realized_pnl"] for r in rows)
    total_fees = sum(r["fees_paid"] for r in rows)
    total_hours = sum(r["duration_h"] for r in rows)
    peak = max(r["peak_deployed"] for r in rows)
    print(f"\nTOTAL: net={total_net:.2f} realized={total_realized:.2f} fees={total_fees:.2f} "
          f"over {total_hours:.1f} replay-hours, max peak_deployed=${peak:.0f}")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()

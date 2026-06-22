"""
Common launch path for live trading: ONE launcher discovers the universe once,
then spawns TWO processes sharing a multiprocessing.Queue —

  - trading process (live_mm.build_and_run): trades; emits strategy-stamped
    order/decision records to the queue; does NO file I/O; computes only the
    decision alpha.
  - logger process (live_logger.logger_main): logs the private feed (own WS) +
    drains the queue -> orders.jsonl / decisions.jsonl.

Usage:
  run_live.py --paper  --series KXWCGAME --football --budget 250 --config <best.json> -d 2
  run_live.py --live   --series KXWCGAME --football --budget 50  --config <best.json> -d 0.25
Default is PAPER (no real orders). --live places REAL orders via the trade key.
"""
import argparse
import asyncio
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.live_mm import resolve_universe, build_and_run, OUTPUT_BASE
from research.hft.live_ipc import TimingEmitter
from research.hft.live_logger import logger_main
from research.hft.passive_fill import FORWARD_DELAY_S


def build_args():
    p = argparse.ArgumentParser(description = "Live trading launcher (main + logger processes)")
    p.add_argument("--live", action = "store_true", help = "REAL orders (default: paper)")
    p.add_argument("--paper", action = "store_true", help = "explicit paper (no real orders)")
    p.add_argument("--config", type = str, default = None,
                   help = "strategy json; must contain an 'alphas' list (+ per_order_size, "
                          "inventory_cap, free_budget, ...) — reproduces a sweep config exactly")
    p.add_argument("-a", "--alpha-name", type = str, default = "obi_dev_60s")
    p.add_argument("-t", "--skew-threshold", type = float, default = 0.176139)
    p.add_argument("-s", "--per-order-size", type = float, default = 50)
    p.add_argument("-i", "--inventory-cap", type = float, default = 1000)
    p.add_argument("--budget", type = float, default = 250)
    p.add_argument("--universe", type = str, default = None,
                   help = "JSON file of explicit pairs/events to trade (bypasses discovery + gate)")
    p.add_argument("--series", type = str, default = "KXWCGAME")
    p.add_argument("--extra-series", type = str, default = None)
    p.add_argument("-n", "--top-n", type = int, default = 10)
    p.add_argument("--extra-top-n", type = int, default = 8)
    p.add_argument("--min-volume", type = float, default = 25_000)
    p.add_argument("--football", action = "store_true", help = "WCStrategy (KXWCGAME)")
    p.add_argument("--ignore-clock", action = "store_true",
                   help = "live testing: skip WC phase-gating (trade regardless of game clock)")
    p.add_argument("--forward-delay", type = float, default = FORWARD_DELAY_S)
    p.add_argument("--max-spread", type = float, default = 0.02)
    p.add_argument("--price-min", type = float, default = 0.05)
    p.add_argument("--price-max", type = float, default = 0.95)
    p.add_argument("--improve", action = "store_true")
    p.add_argument("--pair-risk", action = "store_true")
    p.add_argument("--combo-file", type = str, default = None)
    p.add_argument("--realistic", action = "store_true",
                   help = "AWS feed delays for the PAPER sim (no effect on live/ProdExchange)")
    p.add_argument("-d", "--duration", type = float, default = None, help = "hours")
    p.add_argument("--tag", type = str, default = None)
    args = p.parse_args()

    if args.config:
        c = json.load(open(args.config))
        # the config is the full strategy spec; apply every knob 1:1 onto args so live
        # reproduces the sweep exactly. It MUST carry an `alphas` list -> fail loudly
        # rather than silently fall back to the -t default threshold.
        for k, v in c.items():
            setattr(args, k, v)
        if not getattr(args, "alphas", None):
            sys.exit(f"--config {args.config}: no 'alphas' list. Legacy alpha/thr configs are "
                     "no longer supported; use {'alphas': [{'family','hl','threshold'}], ...}")
    args.combo = None
    if args.combo_file:
        args.combo = json.load(open(args.combo_file))
    # realistic delays drive the PAPER SimExchange; live (ProdExchange) ignores them
    if args.realistic or not args.live:
        from research.hft.exchange import REALISTIC_DELAYS
        for k, v in REALISTIC_DELAYS.items():
            setattr(args, k, v)
    if args.tag is None:
        from src.utils.timestamp import Timestamp
        mode = "live" if args.live else "paper"
        args.tag = f"{mode}_{Timestamp.now().et.strftime('%Y%m%d_%H%M%S')}_{args.alpha_name}_b{int(args.budget)}"
    return args


def trade_proc(args, pairs, extra_events, out_dir, q):
    """Trading child: attach the timing emitter (wraps the shared queue) and run.
    No file I/O (write_at_end=False); the logger persists everything."""
    args.timing_emit = TimingEmitter(q)
    asyncio.run(build_and_run(args, pairs, extra_events, Path(out_dir), write_at_end = False))
    q.put(None)                          # sentinel: trading finished -> logger drains + exits


def main():
    args = build_args()
    out_dir = OUTPUT_BASE / args.tag
    out_dir.mkdir(parents = True, exist_ok = True)
    print(f"Output dir: {out_dir}  mode={'LIVE (real orders)' if args.live else 'paper'}")

    pairs, extra_events, tickers = resolve_universe(args)
    if not tickers:
        print("No matching events found.")
        return
    print(f"universe: {len(tickers)} tickers")

    q = mp.Queue(maxsize = 200_000)
    p_log = mp.Process(target = logger_main, args = (str(out_dir), q), name = "logger")
    p_trade = mp.Process(target = trade_proc,
                         args = (args, pairs, extra_events, str(out_dir), q), name = "trading")
    p_log.start()
    p_trade.start()
    try:
        p_trade.join()
    except KeyboardInterrupt:
        p_trade.terminate()
    q.put(None)                          # ensure the logger gets a sentinel
    p_log.join(timeout = 15)
    if p_log.is_alive():
        p_log.terminate()
    print("run_live: both processes done")


if __name__ == "__main__":
    mp.set_start_method("fork")
    main()

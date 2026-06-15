"""
Live paper-trading runner for the passive MM strategy (strategy type "a").

Drives the exact same MMSimConsumer/PairMM/PassiveFillEngine stack as
mm_sim.py, but from the live websocket (orderbook_delta + trade) instead of a
recording. Fills are simulated locally with queue-position modeling; no real
orders are sent. Used to validate replay results under live conditions.

Outputs under /data/user_data/saksham3/kalshi_hft/sims/<tag>/:
  fills.csv     every simulated fill (markouts added at shutdown)
  summary.csv   params + aggregate metrics, same schema as mm_sim.py
"""

import argparse
import asyncio
import csv
import json
import os
import signal
import sys
import time
from collections import defaultdict
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.mm_sim import MMSimConsumer, compute_markouts, MARKOUT_HORIZONS_S, OUTPUT_BASE
from research.hft.passive_fill import FORWARD_DELAY_S
from research.hft.record_ticks import discover_extra_events
from research.hft.replay import TopOfBook
from research.signals.pairs import discover_top_pairs
from src.utils.api import WS_URL, fetch_series_fee, ws_auth_headers
from src.utils.orderbook import MarketBook


class LiveFeed:
    """Live websocket book feed exposing the Replayer interface (books, top)."""

    def __init__(self, tickers: list[str]):
        self.tickers = tickers
        self.books: dict[str, MarketBook] = defaultdict(MarketBook)
        self.consumer = None
        self.state_dir = None  # set by main; state.csv refreshed every status tick

    def top(self, ticker: str) -> TopOfBook:
        book = self.books[ticker]
        yb, ybq = book.yes.best_bid()
        nb, nbq = book.no.best_bid()
        ya = None if nb is None else round(1.0 - nb, 6)
        return TopOfBook(yes_bid = yb, yes_bid_qty = ybq, yes_ask = ya, yes_ask_qty = nbq)

    async def run(self, status_interval_s: float = 300):
        last_status = time.time()
        while True:
            headers = ws_auth_headers()
            try:
                async with websockets.connect(WS_URL, additional_headers = headers) as ws:
                    for i, channel in enumerate(("orderbook_delta", "trade")):
                        await ws.send(json.dumps({
                            "id": i + 1,
                            "cmd": "subscribe",
                            "params": {"channels": [channel], "market_tickers": self.tickers},
                        }))
                    print(f"Subscribed to {len(self.tickers)} tickers")

                    async for raw in ws:
                        data = json.loads(raw)
                        lts = time.time()
                        msg_type = data.get("type")
                        if msg_type == "orderbook_snapshot":
                            msg = data["msg"]
                            ticker = msg["market_ticker"]
                            book = self.books[ticker]
                            book.yes.load_snapshot(msg.get("yes_dollars_fp", []))
                            book.no.load_snapshot(msg.get("no_dollars_fp", []))
                            self.consumer.on_book(lts, ticker, None)
                        elif msg_type == "orderbook_delta":
                            msg = data["msg"]
                            ticker = msg["market_ticker"]
                            book = self.books[ticker]
                            side = book.yes if msg["side"] == "yes" else book.no
                            side.apply_delta(msg["price_dollars"], float(msg["delta_fp"]))
                            self.consumer.on_book(lts, ticker, msg)
                        elif msg_type == "trade":
                            self.consumer.on_trade(lts, data["msg"])
                        elif msg_type == "error":
                            print(f"  Server error: {data}")

                        if lts - last_status >= status_interval_s:
                            last_status = lts
                            pnl = self.consumer.pnl
                            mids = {t: h[-1][1] for t, h in self.consumer.mid_history.items() if h}
                            print(
                                f"[status] fills={len(self.consumer.fill_rows)} "
                                f"realized={pnl.realized_pnl:.2f} fees={pnl.fees_paid:.2f} "
                                f"net_mtm={pnl.net_total_pnl(prices = mids):.2f} "
                                f"open={sum(abs(p.qty) for p in pnl.positions.values()):.0f}"
                            )
                            if self.state_dir is not None:
                                self.consumer.dump_state(self.state_dir)
                                self.consumer.dump_orders(self.state_dir)
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                print(f"Connection error ({type(e).__name__}: {e}), reconnecting in 5s...")
                await asyncio.sleep(5)
                continue


def write_outputs(consumer, args, out_dir: Path):
    compute_markouts(consumer)
    fill_fields = list(consumer.fill_rows[0].keys()) if consumer.fill_rows else []
    with open(out_dir / "fills.csv", "w", newline = "") as f:
        w = csv.DictWriter(f, fieldnames = fill_fields)
        w.writeheader()
        w.writerows(consumer.fill_rows)
    consumer.dump_state(out_dir)
    consumer.dump_orders(out_dir)

    # Airtight PnL: settle positions whose markets have actually resolved
    from src.utils.api import fetch_market_result
    resolved = 0
    for ticker in list(consumer.pnl.positions.keys()):
        try:
            result = fetch_market_result(ticker)
        except Exception as e:
            print(f"  resolve lookup failed for {ticker}: {e}")
            continue
        if result == "yes":
            consumer.pnl.resolve(ticker, 1.0)
            resolved += 1
        elif result == "no":
            consumer.pnl.resolve(ticker, 0.0)
            resolved += 1
    if resolved:
        print(f"  settled {resolved} positions at official results")

    pnl = consumer.pnl
    last_mids = {t: h[-1][1] for t, h in consumer.mid_history.items() if h}
    contracts = sum(r["qty"] for r in consumer.fill_rows)
    markout_means = {}
    for h in MARKOUT_HORIZONS_S:
        vals = [(r[f"markout_{h}s"], r["qty"]) for r in consumer.fill_rows if r.get(f"markout_{h}s", "") != ""]
        markout_means[h] = (sum(m * q for m, q in vals) / sum(q for _, q in vals)) if vals else float("nan")

    summary = {
        "recording": "LIVE",
        "per_order_size": args.per_order_size,
        "inventory_cap": args.inventory_cap,
        "skew_threshold": args.skew_threshold,
        "alpha_name": args.alpha_name,
        "max_spread": args.max_spread,
        "improve": int(args.improve),
        "pair_risk": int(args.pair_risk),
        "n_fills": len(consumer.fill_rows),
        "contracts": contracts,
        "realized_pnl": round(pnl.realized_pnl, 4),
        "fees_paid": round(pnl.fees_paid, 4),
        "unrealized_pnl": round(pnl.mark_to_market(last_mids), 4),
        "net_pnl": round(pnl.net_total_pnl(prices = last_mids), 4),
        "open_contracts_end": round(sum(abs(p.qty) for p in pnl.positions.values()), 2),
        "net_pair_exposure_end": round(
            sum(abs(mm._pair_exposure()) for mm in consumer.strategies.values()), 2
        ),
        "peak_deployed_dollars": round(consumer.peak_deployed, 2),
        "markout_5s_cents": round(markout_means[5], 4),
        "markout_30s_cents": round(markout_means[30], 4),
        "markout_60s_cents": round(markout_means[60], 4),
        "markout_300s_cents": round(markout_means[300], 4),
    }
    with open(out_dir / "summary.csv", "w", newline = "") as f:
        w = csv.DictWriter(f, fieldnames = list(summary.keys()))
        w.writeheader()
        w.writerow(summary)

    print(f"\nWrote {out_dir}/fills.csv, summary.csv")
    print(f"\n{'=' * 64}\nLIVE PASSIVE MM SUMMARY")
    for k, v in summary.items():
        print(f"  {k:<24} {v}")
    print("=" * 64)


async def main():
    parser = argparse.ArgumentParser(description = "Live paper-trading passive MM (no real orders)")
    parser.add_argument("-n", "--top-n", type = int, default = 10)
    parser.add_argument("-c", "--category", type = str, default = "Sports")
    parser.add_argument("--series", type = str, default = None,
                        help = "Comma-separated series filter (e.g. KXMLBGAME,KXNBAGAME)")
    parser.add_argument("--extra-series", type = str, default = None,
                        help = "Comma-separated N-market series to trade single-market (e.g. KXWCGAME)")
    parser.add_argument("--extra-top-n", type = int, default = 8)
    parser.add_argument("--min-volume", type = float, default = 25_000,
                        help = "Min 24h volume (contracts) at discovery — dead-market filter; "
                               "the strict >=1M rule applies to the recorded dataset, not here "
                               "(pregame vol24h badly underestimates in-game liquidity)")
    parser.add_argument("--forward-delay", type = float, default = FORWARD_DELAY_S,
                        help = "One-way order-entry latency (s); fills require placement + delay <= trade ts")
    parser.add_argument("-s", "--per-order-size", type = float, default = 10)
    parser.add_argument("-i", "--inventory-cap", type = float, default = 30)
    parser.add_argument("-t", "--skew-threshold", type = float, default = 0.1)
    parser.add_argument("-a", "--alpha-name", type = str, default = "obi")
    parser.add_argument("--max-spread", type = float, default = 0.02)
    parser.add_argument("--price-min", type = float, default = 0.05)
    parser.add_argument("--price-max", type = float, default = 0.95)
    parser.add_argument("--improve", action = "store_true",
                        help = "Step one side 1 tick inside 2+ tick spreads (queue priority)")
    parser.add_argument("--pair-risk", action = "store_true",
                        help = "Cap/reduce net pair exposure instead of per-ticker inventory")
    parser.add_argument("--combo-file", type = str, default = None,
                        help = "combo weights JSON from fit_combo.py (use with -a combo)")
    parser.add_argument("-d", "--duration", type = float, default = None, help = "Hours to run")
    parser.add_argument("--tag", type = str, default = None)
    args = parser.parse_args()

    args.combo = None
    if args.combo_file:
        with open(args.combo_file) as f:
            args.combo = json.load(f)

    if args.tag is None:
        from src.utils.timestamp import Timestamp
        ts_str = Timestamp.now().et.strftime("%Y%m%d_%H%M%S")
        args.tag = (
            f"live_{ts_str}_s{args.per_order_size:g}_i{args.inventory_cap:g}"
            f"_t{args.skew_threshold:g}_{args.alpha_name}"
        )
    out_dir = OUTPUT_BASE / args.tag
    out_dir.mkdir(parents = True, exist_ok = True)
    print(f"Output dir: {out_dir}")

    from research.hft.discovery import discover_league_events
    all_series = ",".join(filter(None, [args.series, args.extra_series]))
    pairs, extra_events = discover_league_events(all_series, args.top_n)
    if not pairs and not extra_events:
        print("No matching events found.")
        return
    # Annotate with series fee info (consumer seeds its fee cache from this)
    fee_cache = {}
    for pair in pairs:
        series = pair["first_ticker"].split("-", 1)[0]
        if series not in fee_cache:
            fee_cache[series] = fetch_series_fee(series)
        pair["series"] = series
        pair["fee_multiplier"], pair["fee_type"] = fee_cache[series]
    for ev in extra_events:
        if ev["series"] not in fee_cache:
            fee_cache[ev["series"]] = fetch_series_fee(ev["series"])
        ev["fee_multiplier"], ev["fee_type"] = fee_cache[ev["series"]]

    # Dataset rules: liquidity floor + T-1h trading window (lookahead = run
    # duration so games starting mid-run are included and gated at quote time)
    from research.hft.game_times import game_start
    from research.hft.record_ticks import liquidity_window_gate
    now = time.time()
    lookahead = (args.duration or 12) * 3600
    pairs = liquidity_window_gate(pairs, lambda p: p["first_ticker"], now, args.min_volume, lookahead)
    extra_events = liquidity_window_gate(extra_events, lambda e: e["tickers"][0], now, args.min_volume, lookahead)
    args.game_starts = {}
    for pair in pairs:
        args.game_starts[pair["event_ticker"]] = game_start(pair["event_ticker"], pair["first_ticker"])
    for ev in extra_events:
        args.game_starts[ev["event_ticker"]] = game_start(ev["event_ticker"], ev["tickers"][0])
    print(f"after liquidity/window gate: {len(pairs)} pairs, {len(extra_events)} events")

    tickers = []
    for pair in pairs:
        tickers.extend([pair["first_ticker"], pair["second_ticker"]])
    for ev in extra_events:
        tickers.extend(ev["tickers"])

    feed = LiveFeed(tickers)
    consumer = MMSimConsumer(feed, args)
    feed.consumer = consumer
    feed.state_dir = out_dir
    consumer.on_meta(time.time(), {"pairs": pairs, "events": extra_events})

    try:
        if args.duration is not None:
            await asyncio.wait_for(feed.run(), timeout = args.duration * 3600)
        else:
            await feed.run()
    except asyncio.TimeoutError:
        print(f"\n--- Duration {args.duration}h elapsed ---")
    except KeyboardInterrupt:
        print("\nStopping live MM...")
    finally:
        write_outputs(consumer, args, out_dir)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
    asyncio.run(main())

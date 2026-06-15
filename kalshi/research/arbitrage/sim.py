"""
Live arb simulator for Kalshi multi-outcome events.

Connects to the WS feed, detects arbitrage opportunities, and simulates
trading them with incremental sizing. Tracks PnL via the generic PnL class.

Incremental sizing rules:
  - When an arb opens, trade the full available qty.
  - While the arb is active (same side), if qty increases, trade the delta.
  - If qty decreases or stays the same, do nothing.
  - When the arb closes or side flips, reset tracking.
"""

import argparse
import asyncio
import csv
import json
import os
import signal
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.utils.timestamp import Timestamp
from src.utils.api import ws_auth_headers, paginate, discover_top_events, WS_URL
from src.utils.orderbook import BookSide, MarketBook
from src.pnl import PnL

OUTPUT_DIR = Path("/data/user_data/saksham3/kalshi/arb_logs")


# --- Sim arb state ---

@dataclass
class SimArb:
    side: str
    filled_qty: float
    open_ts: Timestamp


# --- Simulator ---

class ArbSimulator:
    def __init__(self, events: list[dict], writer: csv.writer, log_file):
        self.events = events
        self.pnl = PnL(charge_fees = True, fee_model = "kalshi")
        self.ticker_to_event: dict[str, str] = {}
        for ev in events:
            for t in ev["tickers"]:
                self.ticker_to_event[t] = ev["event_ticker"]

        self.all_tickers = list(self.ticker_to_event.keys())
        self.books: dict[str, MarketBook] = defaultdict(MarketBook)
        self.active_sims: dict[str, SimArb] = {}
        self._seq: dict[int, int] = {}

        self.event_info = {ev["event_ticker"]: ev for ev in events}

        self._writer = writer
        self._log_file = log_file
        self._trade_count = 0
        self._total_contracts = 0.0
        self._arb_profit = 0.0

    def _check_and_trade(self, event_ticker: str, exchange_ts: str | None):
        ev = self.event_info[event_ticker]
        tickers = ev["tickers"]
        now = Timestamp.now()

        # Compute arb conditions (same logic as arb monitor)
        sum_yes_ask = 0.0
        sum_yes_bid = 0.0
        min_ask_qty = float("inf")
        min_bid_qty = float("inf")
        all_have_data = True

        # Per-leg prices for trade logging
        leg_yes_ask: dict[str, float] = {}
        leg_yes_bid: dict[str, float] = {}

        for t in tickers:
            book = self.books[t]
            yes_bid_price, yes_bid_qty = book.yes.best_bid()
            no_bid_price, no_bid_qty = book.no.best_bid()

            if no_bid_price is not None and no_bid_price > 0:
                yes_ask = 1.0 - no_bid_price
                sum_yes_ask += yes_ask
                min_ask_qty = min(min_ask_qty, no_bid_qty)
                leg_yes_ask[t] = yes_ask
            else:
                all_have_data = False

            if yes_bid_price is not None and yes_bid_price > 0:
                sum_yes_bid += yes_bid_price
                min_bid_qty = min(min_bid_qty, yes_bid_qty)
                leg_yes_bid[t] = yes_bid_price
            else:
                all_have_data = False

        long_arb = all_have_data and sum_yes_ask < 1.0
        short_arb = all_have_data and sum_yes_bid > 1.0

        active = self.active_sims.get(event_ticker)

        if long_arb:
            profit = round(1.0 - sum_yes_ask, 6)
            qty = min_ask_qty

            if active is None or active.side != "long":
                # New arb or side flipped — trade full qty
                self.active_sims[event_ticker] = SimArb(side = "long", filled_qty = qty, open_ts = now)
                self._execute_trade(event_ticker, "long", qty, profit, leg_yes_ask, now)
            else:
                # Same side — only trade if qty increased
                delta = qty - active.filled_qty
                if delta > 0:
                    active.filled_qty = qty
                    self._execute_trade(event_ticker, "long", delta, profit, leg_yes_ask, now)

        elif short_arb:
            profit = round(sum_yes_bid - 1.0, 6)
            qty = min_bid_qty

            if active is None or active.side != "short":
                self.active_sims[event_ticker] = SimArb(side = "short", filled_qty = qty, open_ts = now)
                self._execute_trade(event_ticker, "short", qty, profit, leg_yes_bid, now)
            else:
                delta = qty - active.filled_qty
                if delta > 0:
                    active.filled_qty = qty
                    self._execute_trade(event_ticker, "short", delta, profit, leg_yes_bid, now)

        else:
            if active is not None:
                del self.active_sims[event_ticker]

    def _execute_trade(self, event_ticker: str, side: str, qty: float, profit: float,
                       leg_prices: dict[str, float], ts: Timestamp):
        """Record simulated fills in PnL and log to CSV."""
        # Record per-leg trades in PnL
        for ticker, price in leg_prices.items():
            self.pnl.trade(ticker, side, qty, price)

        self._trade_count += 1
        self._total_contracts += qty

        # Arb profit is locked in at entry
        total_profit = qty * profit
        self._arb_profit += total_profit

        leg_details = ";".join(f"{t}:{p:.4f}" for t, p in leg_prices.items())
        self._writer.writerow([
            ts.readable(), event_ticker, side,
            f"{qty:.2f}", f"{profit:.6f}", f"{total_profit:.4f}",
            leg_details,
        ])
        self._log_file.flush()

        ev_title = self.event_info[event_ticker]["title"]
        print(f"  TRADE [{event_ticker}] {side.upper()} qty={qty:.0f} profit=${profit:.4f}/contract total=${total_profit:.4f}  cumulative=${self._arb_profit:.4f}  ({ev_title})")

    def _handle_snapshot(self, msg: dict):
        ticker = msg["market_ticker"]
        book = self.books[ticker]
        book.yes.load_snapshot(msg.get("yes_dollars_fp", []))
        book.no.load_snapshot(msg.get("no_dollars_fp", []))
        if ticker in self.ticker_to_event:
            self._check_and_trade(self.ticker_to_event[ticker], None)

    def _handle_delta(self, msg: dict):
        ticker = msg["market_ticker"]
        book = self.books[ticker]
        book_side = book.yes if msg["side"] == "yes" else book.no
        book_side.apply_delta(msg["price_dollars"], float(msg["delta_fp"]))
        if ticker in self.ticker_to_event:
            self._check_and_trade(self.ticker_to_event[ticker], msg.get("ts"))

    async def run(self):
        print(f"Connecting to {WS_URL}...")
        print(f"Monitoring {len(self.events)} events, {len(self.all_tickers)} markets")

        BATCH_SIZE = 100

        while True:
            headers = ws_auth_headers()
            try:
                async with websockets.connect(WS_URL, additional_headers = headers) as ws:
                    for i in range(0, len(self.all_tickers), BATCH_SIZE):
                        batch = self.all_tickers[i : i + BATCH_SIZE]
                        sub = {
                            "id": i // BATCH_SIZE + 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta"],
                                "market_tickers": batch,
                            },
                        }
                        await ws.send(json.dumps(sub))
                        print(f"  Subscribed batch {i // BATCH_SIZE + 1}: {len(batch)} tickers")

                    print("Listening for arb opportunities...\n")

                    async for raw in ws:
                        data = json.loads(raw)
                        msg_type = data.get("type")

                        if msg_type == "orderbook_snapshot":
                            sid = data["sid"]
                            self._seq[sid] = data["seq"]
                            self._handle_snapshot(data["msg"])

                        elif msg_type == "orderbook_delta":
                            sid = data["sid"]
                            expected = self._seq.get(sid, 0) + 1
                            actual = data["seq"]
                            if actual != expected:
                                print(f"  Seq gap sid={sid}: expected {expected}, got {actual}")
                            self._seq[sid] = actual
                            self._handle_delta(data["msg"])

                        elif msg_type == "error":
                            print(f"  Server error: {data}")

            except websockets.ConnectionClosed as e:
                print(f"Connection closed ({e.code}), reconnecting...")
                continue

    def print_summary(self):
        print(f"\n{'=' * 60}")
        print("ARB SIMULATION SUMMARY")
        print(f"{'=' * 60}")
        print(f"Total trades: {self._trade_count}")
        print(f"Total contracts: {self._total_contracts:.0f}")
        print(f"Locked-in arb profit: ${self._arb_profit:.4f}")
        print(f"Fees Paid: ${self.pnl.fees_paid:.4f}")
        print(f"Locked-in Net Arb Profit: ${self._arb_profit - self.pnl.fees_paid:.4f}")
        print(self.pnl.summary())
        print(f"{'=' * 60}\n")


TEST_DURATION_S = 300


async def main():
    parser = argparse.ArgumentParser(description = "Simulate arb trading on Kalshi events")
    parser.add_argument("-n", "--top-n", type = int, default = 10, help = "Number of top events by 24h volume")
    parser.add_argument("-c", "--category", type = str, default = "Sports", help = "Event category filter (default: Sports)")
    parser.add_argument("-m", "--max-markets", type = int, default = 10, help = "Skip events with more than this many markets (default: 10)")
    parser.add_argument("-d", "--duration", type = float, default = None, help = "Run for this many hours then exit cleanly (default: run until killed)")
    parser.add_argument("--test", action = "store_true", help = "Test mode: load and trade only a single event")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
    ts_str = Timestamp.now().et.strftime("%Y%m%d_%H%M%S")
    log_path = OUTPUT_DIR / f"arb_sim_{ts_str}.csv"
    log_file = open(log_path, "w", newline = "")
    writer = csv.writer(log_file)
    writer.writerow([
        "local_ts", "event_ticker", "side",
        "qty", "profit_per_contract", "total_profit",
        "leg_details",
    ])
    log_file.flush()
    print(f"Logging to {log_path}")

    try:
        category = args.category if args.category != "all" else None
        top_n = 1 if args.test else args.top_n
        events = discover_top_events(top_n, category = category, max_markets = args.max_markets)
        if not events:
            print("No matching events found.")
            return

        duration_s = args.duration * 3600 if args.duration is not None else (TEST_DURATION_S if args.test else None)

        sim = ArbSimulator(events, writer, log_file)
        try:
            if duration_s is not None:
                await asyncio.wait_for(sim.run(), timeout = duration_s)
            else:
                await sim.run()
        except asyncio.TimeoutError:
            print(f"\n--- Duration {duration_s}s elapsed, exiting ---\n")
        except KeyboardInterrupt:
            print("\nStopping simulator...")
        finally:
            sim.print_summary()
    finally:
        log_file.close()


if __name__ == "__main__":
    # Treat SIGTERM (from SLURM) like KeyboardInterrupt so finally blocks run
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
    asyncio.run(main())

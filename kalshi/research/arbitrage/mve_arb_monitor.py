"""
Monitors top active events on Kalshi for multi-outcome arbitrage.

Long arb:  sum(yes_ask_i) < 1  →  buy all YES aggressively, profit = 1 - sum
Short arb: sum(yes_bid_i) > 1  →  sell all YES aggressively, profit = sum - 1

Since yes_ask = 1 - no_bid, long arb ↔ sum(no_bid_i) > N - 1.
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

OUTPUT_DIR = Path("/data/user_data/saksham3/kalshi/arb_logs")


# --- Active arb tracking ---

@dataclass
class ActiveArb:
    side: str  # "long" or "short"
    profit_per_contract: float
    max_profit_per_contract: float
    min_qty: float
    open_local_ts: Timestamp
    open_exchange_ts: Timestamp | None


# --- Monitor ---

class ArbMonitor:
    def __init__(self, events: list[dict], writer: csv.writer, log_file):
        self.events = events
        self.ticker_to_event = {}
        for ev in events:
            for t in ev["tickers"]:
                self.ticker_to_event[t] = ev["event_ticker"]

        self.all_tickers = list(self.ticker_to_event.keys())
        self.books: dict[str, MarketBook] = defaultdict(MarketBook)
        self.active_arbs: dict[str, ActiveArb] = {}
        self._seq: dict[int, int] = {}

        self.event_info = {ev["event_ticker"]: ev for ev in events}

        self._writer = writer
        self._log_file = log_file

    def check_arb(self, event_ticker: str, exchange_ts: str | None):
        ev = self.event_info[event_ticker]
        tickers = ev["tickers"]
        now_local = Timestamp.now()
        exchange_ts_obj = Timestamp.from_iso(exchange_ts) if exchange_ts else None

        sum_yes_ask = 0.0
        sum_yes_bid = 0.0
        min_ask_qty = float("inf")
        min_bid_qty = float("inf")
        all_have_data = True

        for t in tickers:
            book = self.books[t]
            yes_bid_price, yes_bid_qty = book.yes.best_bid()
            no_bid_price, no_bid_qty = book.no.best_bid()

            if no_bid_price is not None and no_bid_price > 0:
                yes_ask = 1.0 - no_bid_price
                sum_yes_ask += yes_ask
                min_ask_qty = min(min_ask_qty, no_bid_qty)
            else:
                all_have_data = False

            if yes_bid_price is not None and yes_bid_price > 0:
                sum_yes_bid += yes_bid_price
                min_bid_qty = min(min_bid_qty, yes_bid_qty)
            else:
                all_have_data = False

        long_arb = all_have_data and sum_yes_ask < 1.0
        short_arb = all_have_data and sum_yes_bid > 1.0

        active = self.active_arbs.get(event_ticker)

        if long_arb:
            profit = round(1.0 - sum_yes_ask, 6)
            if active is None or active.side != "long":
                if active is not None:
                    self._close_arb(event_ticker, now_local, exchange_ts_obj)
                self.active_arbs[event_ticker] = ActiveArb(
                    side = "long",
                    profit_per_contract = profit,
                    max_profit_per_contract = profit,
                    min_qty = min_ask_qty,
                    open_local_ts = now_local,
                    open_exchange_ts = exchange_ts_obj,
                )
                print(f"  ARB OPEN  [{event_ticker}] LONG  profit=${profit:.4f}/contract  qty={min_ask_qty:.2f}")
            else:
                active.profit_per_contract = profit
                active.max_profit_per_contract = max(active.max_profit_per_contract, profit)
                active.min_qty = min_ask_qty

        elif short_arb:
            profit = round(sum_yes_bid - 1.0, 6)
            if active is None or active.side != "short":
                if active is not None:
                    self._close_arb(event_ticker, now_local, exchange_ts_obj)
                self.active_arbs[event_ticker] = ActiveArb(
                    side = "short",
                    profit_per_contract = profit,
                    max_profit_per_contract = profit,
                    min_qty = min_bid_qty,
                    open_local_ts = now_local,
                    open_exchange_ts = exchange_ts_obj,
                )
                print(f"  ARB OPEN  [{event_ticker}] SHORT profit=${profit:.4f}/contract  qty={min_bid_qty:.2f}")
            else:
                active.profit_per_contract = profit
                active.max_profit_per_contract = max(active.max_profit_per_contract, profit)
                active.min_qty = min_bid_qty

        else:
            if active is not None:
                self._close_arb(event_ticker, now_local, exchange_ts_obj)

    def _close_arb(self, event_ticker: str, close_local_ts: Timestamp, close_exchange_ts: Timestamp | None):
        arb = self.active_arbs.pop(event_ticker)
        ev = self.event_info[event_ticker]

        duration_local = close_local_ts - arb.open_local_ts
        duration_exchange = ""
        if arb.open_exchange_ts and close_exchange_ts:
            duration_exchange = close_exchange_ts - arb.open_exchange_ts

        self._writer.writerow([
            event_ticker, ev["title"], arb.side,
            f"{arb.profit_per_contract:.6f}", f"{arb.max_profit_per_contract:.6f}", f"{arb.min_qty:.2f}",
            arb.open_local_ts.readable(), arb.open_exchange_ts.readable() if arb.open_exchange_ts else "",
            close_local_ts.readable(), close_exchange_ts.readable() if close_exchange_ts else "",
            f"{duration_local:.3f}",
            f"{duration_exchange:.3f}" if isinstance(duration_exchange, float) else duration_exchange,
        ])
        self._log_file.flush()
        print(f"  ARB CLOSE [{event_ticker}] {arb.side.upper()} max_profit=${arb.max_profit_per_contract:.4f} close_profit=${arb.profit_per_contract:.4f} qty={arb.min_qty:.2f} duration={duration_local:.3f}s")

    def _handle_snapshot(self, msg: dict):
        ticker = msg["market_ticker"]
        book = self.books[ticker]
        book.yes.load_snapshot(msg.get("yes_dollars_fp", []))
        book.no.load_snapshot(msg.get("no_dollars_fp", []))
        if ticker in self.ticker_to_event:
            self.check_arb(self.ticker_to_event[ticker], None)

    def _handle_delta(self, msg: dict):
        ticker = msg["market_ticker"]
        book = self.books[ticker]
        side = book.yes if msg["side"] == "yes" else book.no
        side.apply_delta(msg["price_dollars"], float(msg["delta_fp"]))
        if ticker in self.ticker_to_event:
            self.check_arb(self.ticker_to_event[ticker], msg.get("ts"))

    async def run(self):
        print(f"Connecting to {WS_URL}...")
        print(f"Monitoring {len(self.events)} events, {len(self.all_tickers)} markets")

        # Subscribe in batches to avoid oversized messages
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

    def close(self):
        """Flush any open arbs as closed rows."""
        now = Timestamp.now()
        for et in list(self.active_arbs.keys()):
            self._close_arb(et, now, None)


REFRESH_INTERVAL_S = 8 * 3600


async def main():
    parser = argparse.ArgumentParser(description = "Monitor top Kalshi events for multi-outcome arbitrage")
    parser.add_argument("-n", "--top-n", type = int, default = 10, help = "Number of top events by 24h volume")
    parser.add_argument("-c", "--category", type = str, default = "Sports", help = "Event category filter (default: Sports, 'all' to disable)")
    parser.add_argument("-m", "--max-markets", type = int, default = 10, help = "Skip events with more than this many markets (default: 10)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
    log_path = OUTPUT_DIR / "arb_log.csv"
    log_file = open(log_path, "a", newline = "")
    writer = csv.writer(log_file)
    if log_path.stat().st_size == 0:
        writer.writerow([
            "event_ticker", "title", "side",
            "profit_per_contract", "max_profit_per_contract", "min_qty",
            "open_local_ts", "open_exchange_ts",
            "close_local_ts", "close_exchange_ts",
            "duration_local_s", "duration_exchange_s",
        ])
        log_file.flush()
    print(f"Logging to {log_path}")

    try:
        while True:
            category = args.category if args.category != "all" else None
            events = discover_top_events(args.top_n, category = category, max_markets = args.max_markets)
            if not events:
                print("No multivariate events found.")
                return

            monitor = ArbMonitor(events, writer, log_file)
            try:
                await asyncio.wait_for(monitor.run(), timeout = REFRESH_INTERVAL_S)
            except asyncio.TimeoutError:
                print(f"\n--- Refreshing top {args.top_n} events after {REFRESH_INTERVAL_S // 3600}h ---\n")
            finally:
                monitor.close()
    except KeyboardInterrupt:
        print("\nStopping monitor...")
    finally:
        log_file.close()


if __name__ == "__main__":
    # Treat SIGTERM (from SLURM) like KeyboardInterrupt so finally blocks run
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
    asyncio.run(main())

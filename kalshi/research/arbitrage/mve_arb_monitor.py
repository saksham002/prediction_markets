"""
Monitors top active events on Kalshi for multi-outcome arbitrage.

Long arb:  sum(yes_ask_i) < 1  →  buy all YES aggressively, profit = 1 - sum
Short arb: sum(yes_bid_i) > 1  →  sell all YES aggressively, profit = sum - 1

Since yes_ask = 1 - no_bid, long arb ↔ sum(no_bid_i) > N - 1.
"""

import argparse
import asyncio
import base64
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import requests
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(dotenv_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.utils.timestamp import Timestamp

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
OUTPUT_DIR = Path("/data/user_data/saksham3/kalshi/arb_logs")


# --- Auth ---

def _load_private_key():
    pk_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    with open(pk_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password = None)


def _sign(private_key, text: str) -> str:
    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf = padding.MGF1(hashes.SHA256()),
            salt_length = padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _ws_auth_headers():
    key_id = os.environ["KALSHI_KEY_ID"]
    private_key = _load_private_key()
    timestamp_ms = str(int(time.time() * 1000))
    message = timestamp_ms + "GET" + "/trade-api/ws/v2"
    signature = _sign(private_key, message)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


# --- REST: discover top multivariate events ---

def api_get(url, params = None):
    for attempt in range(5):
        resp = requests.get(url, params = params, timeout = 30)
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()


def paginate(endpoint, params = None, key = None, max_per_page = 1000):
    if key is None:
        key = endpoint.strip("/").split("/")[-1]
    params = dict(params or {})
    params["limit"] = max_per_page
    all_items = []
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        data = api_get(f"{BASE_URL}/{endpoint}", params = params).json()
        items = data.get(key, [])
        all_items.extend(items)
        cursor = data.get("cursor", "")
        if not cursor or not items:
            break
        time.sleep(0.15)
    return all_items



def discover_top_events(n: int):
    """Find top N active events (with 2+ markets) by total volume."""
    print("Fetching active events with nested markets...")
    events = paginate(
        "events",
        params = {"status": "open", "with_nested_markets": True},
        key = "events",
        max_per_page = 200,
    )
    print(f"  {len(events)} active events found")

    scored = []
    for ev in events:
        mkts = ev.get("markets", [])
        if len(mkts) < 2:
            continue
        total_vol = sum(float(m["volume_fp"]) for m in mkts)
        scored.append((ev, mkts, total_vol))

    scored.sort(key = lambda x: x[2], reverse = True)
    top = scored[:n]

    result = []
    for ev, mkts, total_vol in top:
        et = ev["event_ticker"]
        result.append({
            "event_ticker": et,
            "title": ev.get("title", et),
            "volume": total_vol,
            "markets": mkts,
            "tickers": [m["ticker"] for m in mkts],
        })
        print(f"  [{et}] volume={total_vol:.0f} markets={len(mkts)} title={ev.get('title', '')[:60]}")

    return result


# --- Orderbook state ---

@dataclass
class BookSide:
    levels: dict[str, float] = field(default_factory = dict)

    def apply_delta(self, price: str, delta: float):
        current = self.levels.get(price, 0.0)
        new_qty = current + delta
        if new_qty <= 0:
            self.levels.pop(price, None)
        else:
            self.levels[price] = new_qty

    def best_bid(self) -> tuple[float | None, float | None]:
        if not self.levels:
            return None, None
        best_price = max(self.levels.keys(), key = lambda p: float(p))
        return float(best_price), self.levels[best_price]

    def load_snapshot(self, levels: list[list[str]]):
        self.levels.clear()
        for price, qty in levels:
            q = float(qty)
            if q > 0:
                self.levels[price] = q


@dataclass
class MarketBook:
    yes: BookSide = field(default_factory = BookSide)
    no: BookSide = field(default_factory = BookSide)


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
    def __init__(self, events: list[dict]):
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


        OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
        log_path = OUTPUT_DIR / "arb_log.csv"
        self._log_file = open(log_path, "a", newline = "")
        self._writer = csv.writer(self._log_file)
        if log_path.stat().st_size == 0:
            self._writer.writerow([
                "event_ticker", "title", "side",
                "profit_per_contract", "max_profit_per_contract", "min_qty",
                "open_local_ts", "open_exchange_ts",
                "close_local_ts", "close_exchange_ts",
                "duration_local_s", "duration_exchange_s",
            ])

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
            headers = _ws_auth_headers()
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
        now = Timestamp.now()
        for et in list(self.active_arbs.keys()):
            self._close_arb(et, now, None)
        self._log_file.close()


async def main():
    parser = argparse.ArgumentParser(description = "Monitor top Kalshi events for multi-outcome arbitrage")
    parser.add_argument("-n", "--top-n", type = int, default = 10, help = "Number of top events by volume")
    args = parser.parse_args()

    events = discover_top_events(args.top_n)
    if not events:
        print("No multivariate events found.")
        return

    monitor = ArbMonitor(events)
    try:
        await monitor.run()
    except KeyboardInterrupt:
        print("\nStopping monitor...")
    finally:
        monitor.close()


if __name__ == "__main__":
    asyncio.run(main())

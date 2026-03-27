"""
Per-second signal logger for top N Kalshi markets.

Subscribes to orderbook_delta + trade channels for the highest-volume markets,
maintains live orderbooks, and runs TradeFillMA at multiple half-lives.

Every second, writes one row per market:
  local_ts, exchange_ts, ticker, yes_bid, no_bid,
  tfma_1s_e, tfma_10s_e, tfma_1m_e, tfma_5m_e, tfma_15m_e, tfma_30m_e, tfma_1h_e,
  tfma_1s_l, tfma_10s_l, tfma_1m_l, tfma_5m_l, tfma_15m_l, tfma_30m_l, tfma_1h_l

(e = exchange time decay, l = local time decay)
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

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.signals.trade_fill_ma import TradeFillMA, HALF_LIVES_TIME
from src.utils.timestamp import Timestamp

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
OUTPUT_DIR = Path("/data/user_data/saksham3/kalshi/signal_logs")


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


# --- REST: discover top markets ---

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


def discover_top_markets(n: int) -> list[dict]:
    """Find top N active markets by volume across all active markets."""
    print("Fetching all active markets...")
    all_markets = paginate(
        "markets",
        params = {"status": "open"},
        key = "markets",
    )
    print(f"  {len(all_markets)} active markets found")

    all_markets.sort(key = lambda m: float(m["volume_fp"]), reverse = True)
    top = all_markets[:n]

    for m in top:
        print(f"  [{m['ticker']}] volume={float(m['volume_fp']):.0f} title={m.get('title', '')[:60]}")

    return top


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


# --- Signal Logger ---

HL_LABELS = list(HALF_LIVES_TIME.keys())


class SignalLogger:
    def __init__(self, markets: list[dict]):
        self.tickers = [m["ticker"] for m in markets]
        self.books: dict[str, MarketBook] = defaultdict(MarketBook)
        self._seq: dict[int, int] = {}
        # orderbook_delta ts is ISO string, trade ts is unix epoch int
        self._last_exchange_ts: dict[str, int] = {}

        # One TradeFillMA per ticker per time source, each tracking all half-lives
        self._signals_exchange: dict[str, TradeFillMA] = {}
        self._signals_local: dict[str, TradeFillMA] = {}

        for ticker in self.tickers:
            self._signals_exchange[ticker] = TradeFillMA(
                ticker,
                half_life_seconds = HALF_LIVES_TIME,
                time_source = "exchange",
            )
            self._signals_local[ticker] = TradeFillMA(
                ticker,
                half_life_seconds = HALF_LIVES_TIME,
                time_source = "local",
            )

        # CSV setup
        OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
        ts_str = Timestamp.now().et.strftime("%Y%m%d_%H%M%S")
        log_path = OUTPUT_DIR / f"signal_log_{ts_str}.csv"
        self._log_file = open(log_path, "w", newline = "")
        self._writer = csv.writer(self._log_file)

        header = ["local_ts", "exchange_ts", "ticker", "yes_bid", "no_bid"]
        for label in HL_LABELS:
            header.append(f"tfma_{label}_e")
        for label in HL_LABELS:
            header.append(f"tfma_{label}_l")
        self._writer.writerow(header)

        print(f"Logging to {log_path}")

    def _handle_snapshot(self, msg: dict):
        ticker = msg["market_ticker"]
        book = self.books[ticker]
        book.yes.load_snapshot(msg.get("yes_dollars_fp", []))
        book.no.load_snapshot(msg.get("no_dollars_fp", []))

    def _handle_delta(self, msg: dict):
        ticker = msg["market_ticker"]
        book = self.books[ticker]
        side = book.yes if msg["side"] == "yes" else book.no
        side.apply_delta(msg["price_dollars"], float(msg["delta_fp"]))
        # orderbook_delta ts is ISO — convert to epoch for consistent storage
        ts = msg.get("ts")
        if ts:
            self._last_exchange_ts[ticker] = int(Timestamp.from_iso(ts).epoch)

    def _handle_trade(self, msg: dict):
        """Trade msg ts is unix epoch int."""
        ticker = msg["market_ticker"]
        if ticker not in self._signals_exchange:
            return
        ts = msg.get("ts")
        if ts is not None:
            self._last_exchange_ts[ticker] = int(ts)
        self._signals_exchange[ticker].on_message("trade", msg)
        self._signals_local[ticker].on_message("trade", msg)

    def _write_snapshot(self):
        """Write one row per ticker with current state."""
        local_ts = Timestamp.now()

        for ticker in self.tickers:
            book = self.books[ticker]
            yes_bid_price, _ = book.yes.best_bid()
            no_bid_price, _ = book.no.best_bid()

            exchange_epoch = self._last_exchange_ts.get(ticker)
            if exchange_epoch is not None:
                exchange_str = Timestamp(exchange_epoch).readable()
            else:
                exchange_str = ""

            row = [
                local_ts.readable(),
                exchange_str,
                ticker,
                f"{yes_bid_price:.4f}" if yes_bid_price is not None else "",
                f"{no_bid_price:.4f}" if no_bid_price is not None else "",
            ]

            exchange_vals = self._signals_exchange[ticker].values()
            local_vals = self._signals_local[ticker].values()
            for label in HL_LABELS:
                v = exchange_vals[label]
                row.append(f"{v:.6f}" if v is not None else "")
            for label in HL_LABELS:
                v = local_vals[label]
                row.append(f"{v:.6f}" if v is not None else "")

            self._writer.writerow(row)

        self._log_file.flush()

    async def _periodic_log(self):
        """Write snapshot every second."""
        while True:
            await asyncio.sleep(1)
            self._write_snapshot()

    async def run(self):
        print(f"Connecting to {WS_URL}...")
        print(f"Monitoring {len(self.tickers)} markets")

        BATCH_SIZE = 100

        while True:
            headers = _ws_auth_headers()
            try:
                async with websockets.connect(WS_URL, additional_headers = headers) as ws:
                    # Subscribe to orderbook_delta
                    for i in range(0, len(self.tickers), BATCH_SIZE):
                        batch = self.tickers[i : i + BATCH_SIZE]
                        sub = {
                            "id": i // BATCH_SIZE + 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta"],
                                "market_tickers": batch,
                            },
                        }
                        await ws.send(json.dumps(sub))
                        print(f"  Subscribed orderbook_delta batch {i // BATCH_SIZE + 1}: {len(batch)} tickers")

                    # Subscribe to trade channel
                    for i in range(0, len(self.tickers), BATCH_SIZE):
                        batch = self.tickers[i : i + BATCH_SIZE]
                        sub = {
                            "id": 1000 + i // BATCH_SIZE + 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["trade"],
                                "market_tickers": batch,
                            },
                        }
                        await ws.send(json.dumps(sub))
                        print(f"  Subscribed trade batch {i // BATCH_SIZE + 1}: {len(batch)} tickers")

                    print("Listening and logging every second...\n")

                    log_task = asyncio.create_task(self._periodic_log())

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

                        elif msg_type == "trade":
                            self._handle_trade(data["msg"])

                        elif msg_type == "error":
                            print(f"  Server error: {data}")

            except websockets.ConnectionClosed as e:
                print(f"Connection closed ({e.code}), reconnecting...")
                log_task.cancel()
                continue

    def close(self):
        self._log_file.close()


async def main():
    parser = argparse.ArgumentParser(description = "Per-second signal logger for top Kalshi markets")
    parser.add_argument("-n", "--top-n", type = int, default = 20, help = "Number of top markets by volume")
    args = parser.parse_args()

    markets = discover_top_markets(args.top_n)
    if not markets:
        print("No active markets found.")
        return

    logger = SignalLogger(markets)
    try:
        await logger.run()
    except KeyboardInterrupt:
        print("\nStopping logger...")
    finally:
        logger.close()


if __name__ == "__main__":
    asyncio.run(main())

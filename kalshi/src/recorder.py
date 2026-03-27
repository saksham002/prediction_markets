"""
Records best yes/no bid prices and quantities for Kalshi markets
via the orderbook_delta WebSocket channel.

Maintains an in-memory orderbook from snapshots + deltas, extracts
best bid on each update, and writes timestamped records to a CSV.
"""

import asyncio
import base64
import csv
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env"))

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


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


@dataclass
class BookSide:
    """Price levels for one side (yes or no). Keyed by price string."""
    levels: dict[str, float] = field(default_factory = dict)

    def apply_delta(self, price: str, delta: float):
        current = self.levels.get(price, 0.0)
        new_qty = current + delta
        if new_qty <= 0:
            self.levels.pop(price, None)
        else:
            self.levels[price] = new_qty

    def best_bid(self) -> tuple[Optional[str], Optional[float]]:
        """Returns (price, quantity) of the highest bid, or (None, None)."""
        if not self.levels:
            return None, None
        best_price = max(self.levels.keys(), key = lambda p: float(p))
        return best_price, self.levels[best_price]

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


class Recorder:
    """
    Subscribes to orderbook_delta for given market tickers,
    maintains best bid state, and writes changes to CSV.
    """

    def __init__(self, tickers: list[str], output_dir: str | Path):
        self.tickers = tickers
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents = True, exist_ok = True)
        self.books: dict[str, MarketBook] = defaultdict(MarketBook)
        self._prev_best: dict[str, tuple] = {}
        self._writers: dict[str, csv.writer] = {}
        self._files: list = []
        self._seq: dict[int, int] = {}

    def _get_writer(self, ticker: str) -> csv.writer:
        if ticker not in self._writers:
            path = self.output_dir / f"{ticker}.csv"
            f = open(path, "a", newline = "")
            self._files.append(f)
            writer = csv.writer(f)
            if path.stat().st_size == 0:
                writer.writerow([
                    "local_ts", "exchange_ts",
                    "yes_bid_price", "yes_bid_qty",
                    "no_bid_price", "no_bid_qty",
                ])
            self._writers[ticker] = writer
        return self._writers[ticker]

    def _record(self, ticker: str, exchange_ts: Optional[str]):
        book = self.books[ticker]
        yes_price, yes_qty = book.yes.best_bid()
        no_price, no_qty = book.no.best_bid()

        current = (yes_price, yes_qty, no_price, no_qty)
        if current == self._prev_best.get(ticker):
            return
        self._prev_best[ticker] = current

        writer = self._get_writer(ticker)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            exchange_ts or "",
            yes_price or "", yes_qty or "",
            no_price or "", no_qty or "",
        ])

    def _handle_snapshot(self, msg: dict):
        ticker = msg["market_ticker"]
        book = self.books[ticker]
        book.yes.load_snapshot(msg.get("yes_dollars_fp", []))
        book.no.load_snapshot(msg.get("no_dollars_fp", []))
        self._record(ticker, None)

    def _handle_delta(self, msg: dict):
        ticker = msg["market_ticker"]
        book = self.books[ticker]
        side = book.yes if msg["side"] == "yes" else book.no
        side.apply_delta(msg["price_dollars"], float(msg["delta_fp"]))
        self._record(ticker, msg.get("ts"))

    async def run(self):
        headers = _ws_auth_headers()
        print(f"Connecting to {WS_URL}...")

        async for ws in websockets.connect(WS_URL, additional_headers = headers):
            try:
                sub = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": self.tickers,
                    },
                }
                await ws.send(json.dumps(sub))
                print(f"Subscribed to {len(self.tickers)} market(s): {self.tickers}")

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
                            print(f"Seq gap on sid={sid}: expected {expected}, got {actual}")
                        self._seq[sid] = actual
                        self._handle_delta(data["msg"])

                    elif msg_type == "error":
                        print(f"Server error: {data}")

            except websockets.ConnectionClosed as e:
                print(f"Connection closed ({e.code}), reconnecting...")
                continue
            finally:
                self._flush()

    def _flush(self):
        for f in self._files:
            f.flush()

    def close(self):
        for f in self._files:
            f.close()
        self._files.clear()
        self._writers.clear()


async def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python recorder.py <ticker1> [ticker2] ... [--output-dir DIR]")
        sys.exit(1)

    tickers = []
    output_dir = Path("/data/user_data/saksham3/kalshi/recordings")
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--output-dir":
            output_dir = args[i + 1]
            i += 2
        else:
            tickers.append(args[i])
            i += 1

    recorder = Recorder(tickers, output_dir)
    try:
        await recorder.run()
    except KeyboardInterrupt:
        print("\nStopping recorder...")
    finally:
        recorder.close()


if __name__ == "__main__":
    asyncio.run(main())

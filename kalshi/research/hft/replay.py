"""
Replay engine for raw tick recordings produced by record_ticks.py.

Reads a ticks_*.jsonl.gz file, maintains per-market orderbooks, and
dispatches each message to a consumer in recorded order. Consumers
implement (any subset of):

  on_meta(lts, pairs)                 # pair metadata at (re)subscription
  on_book(lts, ticker, delta_msg)     # after book update; delta_msg None for snapshots
  on_trade(lts, msg)                  # public trade print

Top-of-book convention: Kalshi books hold YES bids and NO bids only.
yes_ask = 1 - best_no_bid, with the NO bid qty as the ask size.
"""

import gzip
import json
import zlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.utils.orderbook import MarketBook


@dataclass
class TopOfBook:
    yes_bid: float | None = None
    yes_bid_qty: float | None = None
    yes_ask: float | None = None
    yes_ask_qty: float | None = None

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round(self.yes_ask - self.yes_bid, 6)


class Replayer:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.books: dict[str, MarketBook] = defaultdict(MarketBook)

    def top(self, ticker: str) -> TopOfBook:
        book = self.books[ticker]
        yb, ybq = book.yes.best_bid()
        nb, nbq = book.no.best_bid()
        ya = None if nb is None else round(1.0 - nb, 6)
        return TopOfBook(yes_bid = yb, yes_bid_qty = ybq, yes_ask = ya, yes_ask_qty = nbq)

    def run(self, consumer):
        on_meta = getattr(consumer, "on_meta", None)
        on_book = getattr(consumer, "on_book", None)
        on_trade = getattr(consumer, "on_trade", None)

        n_lines = 0
        with gzip.open(self.path, "rt") as f:
            try:
                lines = iter(f)
            except (EOFError, zlib.error):
                return 0
            while True:
                # Tolerate decompression failure mid-file (live tail / crashed writer)
                try:
                    line = next(lines)
                except StopIteration:
                    break
                except (EOFError, zlib.error) as e:
                    print(f"  (stream ended early at line {n_lines}: {e})")
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # truncated tail line from a crash/flush boundary
                n_lines += 1
                lts = rec["lts"]

                if "meta" in rec:
                    if on_meta is not None:
                        on_meta(lts, rec["meta"])
                    continue

                data = rec["d"]
                msg_type = data.get("type")
                if msg_type == "orderbook_snapshot":
                    msg = data["msg"]
                    ticker = msg["market_ticker"]
                    book = self.books[ticker]
                    book.yes.load_snapshot(msg.get("yes_dollars_fp", []))
                    book.no.load_snapshot(msg.get("no_dollars_fp", []))
                    if on_book is not None:
                        on_book(lts, ticker, None)
                elif msg_type == "orderbook_delta":
                    msg = data["msg"]
                    ticker = msg["market_ticker"]
                    book = self.books[ticker]
                    side = book.yes if msg["side"] == "yes" else book.no
                    side.apply_delta(msg["price_dollars"], float(msg["delta_fp"]))
                    if on_book is not None:
                        on_book(lts, ticker, msg)
                elif msg_type == "trade":
                    if on_trade is not None:
                        on_trade(lts, data["msg"])
        return n_lines

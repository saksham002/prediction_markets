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
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.utils.orderbook import MarketBook
# TopOfBook + the market-only book view live in market_view (re-exported here so
# existing `from research.hft.replay import TopOfBook` callers keep working).
from research.hft.market_view import MarketView, TopOfBook


class Replayer:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        # MarketView owns the books and subtracts the own-resting ledger on reads.
        # The trading consumer injects our orders into the book (via SimExchange) and
        # populates the ledger, so reads recover market-only; analysis consumers never
        # register orders (empty ledger) -> reads == raw.
        self.view = MarketView()
        self.books: dict[str, MarketBook] = self.view.books

    def top(self, ticker: str) -> TopOfBook:
        return self.view.top(ticker)

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
                # stamp the per-event timing context for any timing-instrumented
                # consumer (gated -> inert/None-safe for analysis consumers)
                if getattr(consumer, "_timing", None) is not None:
                    consumer._evt = {"exchange_ts": data.get("msg", {}).get("ts_ms"),
                                     "read_ts": lts}
                if msg_type == "orderbook_snapshot":
                    msg = data["msg"]
                    ticker = msg["market_ticker"]
                    # through the view (same book-maintenance path as ProdExchange/LiveFeed)
                    self.view.apply_snapshot(ticker, msg.get("yes_dollars_fp", []),
                                             msg.get("no_dollars_fp", []))
                    if on_book is not None:
                        on_book(lts, ticker, None)
                elif msg_type == "orderbook_delta":
                    msg = data["msg"]
                    ticker = msg["market_ticker"]
                    # market delta -> through the view so it clamps the market portion
                    # and never drains our own resting qty (no-op when we hold nothing)
                    self.view.apply_delta(ticker, msg["side"], msg["price_dollars"],
                                          float(msg["delta_fp"]), is_own = False)
                    if on_book is not None:
                        on_book(lts, ticker, msg)
                elif msg_type == "trade":
                    if on_trade is not None:
                        on_trade(lts, data["msg"])
        return n_lines

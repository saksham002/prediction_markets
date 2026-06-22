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
    def __init__(self, path: str | Path, filter_own: bool = True):
        self.path = Path(path)
        # MarketView owns the books and subtracts the own-resting ledger on reads.
        # The trading consumer injects our orders into the book (via SimExchange) and
        # populates the ledger, so reads recover market-only; analysis consumers never
        # register orders (empty ledger) -> reads == raw.
        self.view = MarketView()
        self.books: dict[str, MarketBook] = self.view.books
        # filter_own: drop OUR OWN footprint from a LIVE-TRADED recording so the replay
        # is a clean market-only backtest -- skip own orderbook_deltas (Kalshi tags them
        # with client_order_id) and our public trade prints (trade_id matches one of our
        # fills, collected in a pre-scan). A no-op for untraded recordings (nothing
        # tagged, no fills) -> bit-identical replay.
        self.filter_own = filter_own
        # reconstruction of OUR resting qty by (ticker, side) -> {price: qty}, built from
        # the skipped own deltas, so aggregated/untagged snapshots can be made market-only
        self._own_resting: dict = {}

    def top(self, ticker: str) -> TopOfBook:
        return self.view.top(ticker)

    def _track_own_resting(self, msg: dict):
        """Fold a skipped own delta into our reconstructed resting book (place +, fill/
        cancel -), so _strip_own can subtract it from aggregated snapshots."""
        book = self._own_resting.setdefault((msg["market_ticker"], msg["side"]), {})
        price = msg["price_dollars"]
        q = book.get(price, 0.0) + float(msg["delta_fp"])
        if q > 1e-9:
            book[price] = q
        else:
            book.pop(price, None)

    def _strip_own(self, ticker: str, side: str, levels):
        """Subtract our reconstructed resting qty from aggregated snapshot levels ->
        market-only. Snapshots carry no client_order_id, so this is the only way to
        remove our footprint from them."""
        own = self._own_resting.get((ticker, side))
        if not own:
            return levels
        out = []
        for price, qty in levels:
            q = float(qty) - own.get(price, 0.0)
            if q > 1e-9:
                out.append([price, q])
        return out

    def _scan_own(self):
        """Pre-pass: mark our fill trade_ids (so is_own_trade can drop our public trade
        prints, which may arrive before their fill in the stream). Truncation-tolerant."""
        try:
            with gzip.open(self.path, "rt") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    d = rec.get("d")
                    if d and d.get("type") == "fill":
                        tid = d.get("msg", {}).get("trade_id")
                        if tid is not None:
                            self.view.mark_own_fill(tid)
        except (EOFError, zlib.error, OSError):
            pass

    def run(self, consumer):
        on_meta = getattr(consumer, "on_meta", None)
        on_book = getattr(consumer, "on_book", None)
        on_trade = getattr(consumer, "on_trade", None)

        if self.filter_own:
            self._scan_own()            # collect our fill trade_ids before the main pass
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
                    yes_lv = msg.get("yes_dollars_fp", [])
                    no_lv = msg.get("no_dollars_fp", [])
                    if self.filter_own:
                        # aggregated snapshot bakes in our resting qty (untagged) ->
                        # subtract our reconstruction to recover the market-only book
                        yes_lv = self._strip_own(ticker, "yes", yes_lv)
                        no_lv = self._strip_own(ticker, "no", no_lv)
                    # through the view (same book-maintenance path as ProdExchange/LiveFeed)
                    self.view.apply_snapshot(ticker, yes_lv, no_lv)
                    if on_book is not None:
                        on_book(lts, ticker, None)
                elif msg_type == "orderbook_delta":
                    msg = data["msg"]
                    # our own order's delta (place/fill/cancel) -> not market liquidity;
                    # drop it (and fold into our resting reconstruction for snapshot stripping)
                    if self.filter_own and self.view.is_own_delta(msg):
                        self._track_own_resting(msg)
                        continue
                    ticker = msg["market_ticker"]
                    # market delta -> through the view so it clamps the market portion
                    # and never drains our own resting qty (no-op when we hold nothing)
                    self.view.apply_delta(ticker, msg["side"], msg["price_dollars"],
                                          float(msg["delta_fp"]), is_own = False)
                    if on_book is not None:
                        on_book(lts, ticker, msg)
                elif msg_type == "trade":
                    # the public print of one of our own fills -> drop (alpha/flow + fill
                    # engine must not see our own trade)
                    if self.filter_own and self.view.is_own_trade(data["msg"]):
                        continue
                    if on_trade is not None:
                        on_trade(lts, data["msg"])
        return n_lines

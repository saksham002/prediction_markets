"""
Per-minute signal logger for top N Kalshi paired sports markets.

Subscribes to ticker + trade channels for the highest-volume paired events,
maintains top-of-book state, and runs TradeFillMA in both single-market and
pair-market modes at multiple half-lives.

Once per minute, writes one row per still-active market:
  local_ts, exchange_ts, ticker, event_ticker, pair_position, yes_bid, no_bid,
  tfma_{hl}_e, tfma_{hl}_l,                       (single-market, raw)
  tfma_pw_{hl}_e, tfma_pw_{hl}_l,                 (single-market, price-weighted)
  pair_tfma_{hl}_e, pair_tfma_{hl}_l,             (pair-market, raw)
  pair_tfma_pw_{hl}_e, pair_tfma_pw_{hl}_l        (pair-market, price-weighted)

Markets whose close_time has passed are considered resolved and skipped.
"""

import argparse
import asyncio
import csv
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
import websockets

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.signals.pairs import discover_top_pairs
from research.signals.trade_fill_ma import TradeFillMA, HALF_LIVES_TIME
from src.utils.api import WS_URL, ws_auth_headers
from src.utils.timestamp import Timestamp

OUTPUT_DIR = Path("/data/user_data/saksham3/kalshi/signal_logs")

HL_LABELS = list(HALF_LIVES_TIME.keys())

LOG_INTERVAL_S = 60


@dataclass
class TickerState:
    yes_bid: float | None = None
    no_bid: float | None = None
    yes_ask: float | None = None
    no_ask: float | None = None
    yes_bid_size: float | None = None
    no_bid_size: float | None = None
    yes_ask_size: float | None = None
    no_ask_size: float | None = None


class SignalLogger:
    def __init__(self, pairs: list[dict], writer: csv.writer, log_file):
        self.pairs = pairs
        self.pair_by_ticker: dict[str, str] = {}
        self.tickers: dict[str, TickerState] = {}
        self._last_exchange_ts: dict[str, int] = {}

        # Single-mode TradeFillMA per ticker, pair-mode per event, both time sources
        self._signals_exchange: dict[str, TradeFillMA] = {}
        self._signals_local: dict[str, TradeFillMA] = {}
        self._pair_signals_exchange: dict[str, TradeFillMA] = {}
        self._pair_signals_local: dict[str, TradeFillMA] = {}

        self.all_tickers: list[str] = []
        self.ticker_close_time: dict[str, Timestamp] = {}

        for pair in pairs:
            ft = pair["first_ticker"]
            st = pair["second_ticker"]
            self.pair_by_ticker[ft] = pair["event_ticker"]
            self.pair_by_ticker[st] = pair["event_ticker"]
            self.tickers[ft] = TickerState()
            self.tickers[st] = TickerState()
            self.all_tickers.extend([ft, st])
            self.ticker_close_time[ft] = Timestamp.from_iso(pair["first_close_time"])
            self.ticker_close_time[st] = Timestamp.from_iso(pair["second_close_time"])

            for ticker in (ft, st):
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

            self._pair_signals_exchange[pair["event_ticker"]] = TradeFillMA(
                pair_tickers = (ft, st),
                half_life_seconds = HALF_LIVES_TIME,
                time_source = "exchange",
            )
            self._pair_signals_local[pair["event_ticker"]] = TradeFillMA(
                pair_tickers = (ft, st),
                half_life_seconds = HALF_LIVES_TIME,
                time_source = "local",
            )

        self._writer = writer
        self._log_file = log_file

    def _market_closed(self, ticker: str, now_epoch: float | None = None) -> bool:
        if now_epoch is None:
            now_epoch = Timestamp.now().epoch
        return now_epoch >= self.ticker_close_time[ticker].epoch

    def _handle_ticker(self, msg: dict):
        ticker = msg["market_ticker"]
        if ticker not in self.tickers:
            return
        if self._market_closed(ticker):
            return
        state = self.tickers[ticker]
        if "yes_bid_dollars" in msg:
            yb = msg["yes_bid_dollars"]
            state.yes_bid = float(yb) if yb else None
        if "no_bid_dollars" in msg:
            nb = msg["no_bid_dollars"]
            state.no_bid = float(nb) if nb else None
        if "yes_ask_dollars" in msg:
            ya = msg["yes_ask_dollars"]
            state.yes_ask = float(ya) if ya else None
        if "no_ask_dollars" in msg:
            na = msg["no_ask_dollars"]
            state.no_ask = float(na) if na else None
        if "yes_bid_size_fp" in msg:
            ybs = msg["yes_bid_size_fp"]
            state.yes_bid_size = float(ybs) if ybs else None
        if "no_bid_size_fp" in msg:
            nbs = msg["no_bid_size_fp"]
            state.no_bid_size = float(nbs) if nbs else None
        if "yes_ask_size_fp" in msg:
            yas = msg["yes_ask_size_fp"]
            state.yes_ask_size = float(yas) if yas else None
        if "no_ask_size_fp" in msg:
            nas = msg["no_ask_size_fp"]
            state.no_ask_size = float(nas) if nas else None
        ts = msg.get("ts")
        if ts:
            self._last_exchange_ts[ticker] = int(ts)

    def _handle_trade(self, msg: dict):
        """Trade msg ts is unix epoch int."""
        ticker = msg["market_ticker"]
        if ticker not in self._signals_exchange:
            return
        if self._market_closed(ticker):
            return
        self._last_exchange_ts[ticker] = int(msg["ts"])

        self._signals_exchange[ticker].on_message("trade", msg)
        self._signals_local[ticker].on_message("trade", msg)

        event_ticker = self.pair_by_ticker[ticker]
        self._pair_signals_exchange[event_ticker].on_message("trade", msg)
        self._pair_signals_local[event_ticker].on_message("trade", msg)

    def _write_snapshot(self):
        """Write one row per still-active ticker with current state."""
        local_ts = Timestamp.now()

        for pair in self.pairs:
            event_ticker = pair["event_ticker"]
            pair_exchange_vals = self._pair_signals_exchange[event_ticker].values()
            pair_local_vals = self._pair_signals_local[event_ticker].values()
            pair_exchange_pw = self._pair_signals_exchange[event_ticker].values_pw()
            pair_local_pw = self._pair_signals_local[event_ticker].values_pw()

            for idx, ticker in enumerate((pair["first_ticker"], pair["second_ticker"])):
                if self._market_closed(ticker, now_epoch = local_ts.epoch):
                    continue
                state = self.tickers[ticker]
                yes_bid_price = state.yes_bid
                no_bid_price = state.no_bid

                exchange_epoch = self._last_exchange_ts.get(ticker)
                if exchange_epoch is not None:
                    exchange_str = Timestamp(exchange_epoch).readable()
                else:
                    exchange_str = ""

                pair_position = "first" if idx == 0 else "second"

                row = [
                    local_ts.readable(),
                    exchange_str,
                    ticker,
                    event_ticker,
                    pair_position,
                    f"{yes_bid_price:.4f}" if yes_bid_price is not None else "",
                    f"{no_bid_price:.4f}" if no_bid_price is not None else "",
                ]

                exchange_vals = self._signals_exchange[ticker].values()
                local_vals = self._signals_local[ticker].values()
                exchange_pw = self._signals_exchange[ticker].values_pw()
                local_pw = self._signals_local[ticker].values_pw()
                for label in HL_LABELS:
                    v = exchange_vals[label]
                    row.append(f"{v:.6f}" if v is not None else "")
                for label in HL_LABELS:
                    v = local_vals[label]
                    row.append(f"{v:.6f}" if v is not None else "")
                for label in HL_LABELS:
                    v = exchange_pw[label]
                    row.append(f"{v:.6f}" if v is not None else "")
                for label in HL_LABELS:
                    v = local_pw[label]
                    row.append(f"{v:.6f}" if v is not None else "")
                for label in HL_LABELS:
                    v = pair_exchange_vals[label]
                    row.append(f"{v:.6f}" if v is not None else "")
                for label in HL_LABELS:
                    v = pair_local_vals[label]
                    row.append(f"{v:.6f}" if v is not None else "")
                for label in HL_LABELS:
                    v = pair_exchange_pw[label]
                    row.append(f"{v:.6f}" if v is not None else "")
                for label in HL_LABELS:
                    v = pair_local_pw[label]
                    row.append(f"{v:.6f}" if v is not None else "")

                self._writer.writerow(row)

        self._log_file.flush()

    async def _periodic_log(self):
        """Write snapshot every LOG_INTERVAL_S seconds."""
        while True:
            await asyncio.sleep(LOG_INTERVAL_S)
            self._write_snapshot()

    async def run(self):
        print(f"Connecting to {WS_URL}...")
        print(f"Monitoring {len(self.pairs)} events, {len(self.all_tickers)} markets")

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
                                "channels": ["ticker"],
                                "market_tickers": batch,
                            },
                        }
                        await ws.send(json.dumps(sub))
                        print(f"  Subscribed ticker batch {i // BATCH_SIZE + 1}: {len(batch)} tickers")

                    for i in range(0, len(self.all_tickers), BATCH_SIZE):
                        batch = self.all_tickers[i : i + BATCH_SIZE]
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

                    print(f"Listening and logging every {LOG_INTERVAL_S}s...\n")

                    log_task = asyncio.create_task(self._periodic_log())
                    try:
                        async for raw in ws:
                            data = json.loads(raw)
                            msg_type = data.get("type")

                            if msg_type == "ticker":
                                self._handle_ticker(data["msg"])
                            elif msg_type == "trade":
                                self._handle_trade(data["msg"])
                            elif msg_type == "error":
                                print(f"  Server error: {data}")
                    finally:
                        log_task.cancel()
                        try:
                            await log_task
                        except asyncio.CancelledError:
                            pass

            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                print(f"Connection error ({type(e).__name__}: {e}), reconnecting in 5s...")
                await asyncio.sleep(5)
                continue


async def main():
    parser = argparse.ArgumentParser(description = "Per-minute signal logger for top Kalshi paired sports markets")
    parser.add_argument("-n", "--top-n", type = int, default = 10, help = "Number of top sports pairs")
    parser.add_argument("-c", "--category", type = str, default = "Sports", help = "Event category")
    parser.add_argument("-r", "--rerank-interval", type = float, default = 8, help = "Re-rank pairs every this many hours (default: 8)")
    parser.add_argument("-d", "--duration", type = float, default = None, help = "Run for this many hours then exit cleanly (default: run until killed)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
    ts_str = Timestamp.now().et.strftime("%Y%m%d_%H%M%S")
    log_path = OUTPUT_DIR / f"signal_log_{ts_str}.csv"
    log_file = open(log_path, "w", newline = "")
    writer = csv.writer(log_file)

    header = ["local_ts", "exchange_ts", "ticker", "event_ticker", "pair_position", "yes_bid", "no_bid"]
    for label in HL_LABELS:
        header.append(f"tfma_{label}_e")
    for label in HL_LABELS:
        header.append(f"tfma_{label}_l")
    for label in HL_LABELS:
        header.append(f"tfma_pw_{label}_e")
    for label in HL_LABELS:
        header.append(f"tfma_pw_{label}_l")
    for label in HL_LABELS:
        header.append(f"pair_tfma_{label}_e")
    for label in HL_LABELS:
        header.append(f"pair_tfma_{label}_l")
    for label in HL_LABELS:
        header.append(f"pair_tfma_pw_{label}_e")
    for label in HL_LABELS:
        header.append(f"pair_tfma_pw_{label}_l")
    writer.writerow(header)
    log_file.flush()
    print(f"Logging to {log_path}")

    rerank_s = args.rerank_interval * 3600
    duration_s = args.duration * 3600 if args.duration is not None else None
    start_epoch = Timestamp.now().epoch

    try:
        while True:
            elapsed = Timestamp.now().epoch - start_epoch
            if duration_s is not None and elapsed >= duration_s:
                break

            while True:
                try:
                    pairs = discover_top_pairs(args.top_n, args.category)
                    break
                except requests.exceptions.RequestException as e:
                    print(f"discover_top_pairs failed ({type(e).__name__}: {e}), retrying in 60s...")
                    await asyncio.sleep(60)
            if not pairs:
                print("No matching sports pairs found.")
                return

            logger = SignalLogger(pairs, writer, log_file)
            this_iter_timeout = rerank_s
            if duration_s is not None:
                this_iter_timeout = min(rerank_s, duration_s - elapsed)
            try:
                await asyncio.wait_for(logger.run(), timeout = this_iter_timeout)
            except asyncio.TimeoutError:
                if duration_s is not None and (Timestamp.now().epoch - start_epoch) >= duration_s:
                    print(f"\n--- Duration {args.duration}h elapsed, exiting ---\n")
                    break
                print(f"\n--- Re-ranking top {args.top_n} pairs after {args.rerank_interval}h ---\n")
    except KeyboardInterrupt:
        print("\nStopping logger...")
    finally:
        log_file.close()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
    asyncio.run(main())

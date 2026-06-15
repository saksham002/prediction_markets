"""
Live paper trader for paired TradeFillMA signals on Kalshi sports winner markets.

The signal is defined on two-market winner events. Positive values favor the
first-listed team in the event title, while negative values favor the second.

Position management is incremental, capped by a per-pair position limit:
  - Bullish-first state: long first YES + short second YES, both up to `position_limit` contracts.
  - Bullish-second state: short first YES + long second YES, both up to `position_limit` contracts.
  - Each tick, we top up (or unwind) toward the signal's target by min(headroom, opposing book qty).
  - Trades are skipped on a leg whose best bid is < 0.05 or best ask > 0.95.
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

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.signals.pairs import discover_top_pairs
from research.signals.trade_fill_ma import TradeFillMA
from src.pnl import PnL
from src.utils.api import WS_URL, ws_auth_headers
from src.utils.timestamp import Timestamp

OUTPUT_DIR = Path("/data/user_data/saksham3/kalshi/signal_logs")
SIGNAL_HALF_LIFE_SECONDS = {"10s": 10}
SIGNAL_LABEL = "10s"

PRICE_MIN = 0.05
PRICE_MAX = 0.95


@dataclass
class TickerState:
    yes_bid: float | None = None
    yes_ask: float | None = None
    yes_bid_size: float | None = None
    yes_ask_size: float | None = None


class SignalSimulator:
    def __init__(self, pairs: list[dict], threshold: float, position_limit: float, writer: csv.writer, log_file):
        self.pairs = pairs
        self.threshold = threshold
        self.position_limit = position_limit
        self.writer = writer
        self.log_file = log_file
        self.pnl = PnL(charge_fees = True, fee_model = "kalshi")
        self.tickers: dict[str, TickerState] = {}
        self.pair_by_event = {pair["event_ticker"] : pair for pair in pairs}
        self.pair_by_ticker: dict[str, str] = {}
        self.signals: dict[str, TradeFillMA] = {}
        # Signed pair position: positive = bullish_first (long first / short second),
        # negative = bullish_second (short first / long second).
        self.position: dict[str, float] = {}
        self._fill_count = 0

        for pair in pairs:
            first_ticker = pair["first_ticker"]
            second_ticker = pair["second_ticker"]
            self.pair_by_ticker[first_ticker] = pair["event_ticker"]
            self.pair_by_ticker[second_ticker] = pair["event_ticker"]
            self.tickers[first_ticker] = TickerState()
            self.tickers[second_ticker] = TickerState()
            self.position[pair["event_ticker"]] = 0.0
            self.signals[pair["event_ticker"]] = TradeFillMA(
                pair_tickers = (first_ticker, second_ticker),
                half_life_seconds = SIGNAL_HALF_LIFE_SECONDS,
                time_source = "exchange",
            )

        self.all_tickers = list(self.pair_by_ticker.keys())

    def _record_fill(self, event_ticker: str, ticker: str, leg: str, action: str,
                     qty: float, price: float, signed_position: float, signal_value: float):
        pair = self.pair_by_event[event_ticker]
        self.writer.writerow([
            Timestamp.now().readable(),
            event_ticker,
            ticker,
            pair["title"],
            leg,
            action,
            f"{qty:.2f}",
            f"{price:.6f}",
            f"{signed_position:.2f}",
            f"{signal_value:.6f}",
            f"{self.threshold:.6f}",
            f"{self.pnl.realized_pnl:.6f}",
            f"{self.pnl.fees_paid:.6f}",
            f"{self.pnl.net_total_pnl():.6f}",
        ])
        self.log_file.flush()

    def _target_position(self, signal_value: float) -> float:
        if signal_value >= self.threshold:
            return self.position_limit
        if signal_value <= -self.threshold:
            return -self.position_limit
        return 0.0

    def _check_and_trade(self, event_ticker: str):
        signal_value = self.signals[event_ticker].values_pw()[SIGNAL_LABEL]
        if signal_value is None:
            return

        pair = self.pair_by_event[event_ticker]
        first = self.tickers[pair["first_ticker"]]
        second = self.tickers[pair["second_ticker"]]

        target = self._target_position(signal_value)
        current = self.position[event_ticker]
        delta = target - current
        if delta == 0:
            return

        if delta > 0:
            # Move toward bullish_first: buy first YES at ask, sell second YES at bid.
            first_price = first.yes_ask
            first_size = first.yes_ask_size
            second_price = second.yes_bid
            second_size = second.yes_bid_size
            if (first_price is None or first_size is None or
                second_price is None or second_size is None):
                return
            if first_price > PRICE_MAX or second_price < PRICE_MIN:
                return
            qty = min(delta, first_size, second_size)
            if qty <= 0:
                return
            self.pnl.trade(pair["first_ticker"], "long", qty, first_price)
            self.pnl.trade(pair["second_ticker"], "short", qty, second_price)
            self.position[event_ticker] = current + qty
            new_pos = self.position[event_ticker]
            self._fill_count += 1
            self._record_fill(event_ticker, pair["first_ticker"], "first", "buy",
                              qty, first_price, new_pos, signal_value)
            self._record_fill(event_ticker, pair["second_ticker"], "second", "sell",
                              qty, second_price, new_pos, signal_value)
            print(
                f"  FILL [{event_ticker}] +{qty:.0f} → pos={new_pos:.0f} "
                f"first_ask={first_price:.4f} second_bid={second_price:.4f} signal={signal_value:.2f}"
            )
        else:
            # Move toward bullish_second: sell first YES at bid, buy second YES at ask.
            first_price = first.yes_bid
            first_size = first.yes_bid_size
            second_price = second.yes_ask
            second_size = second.yes_ask_size
            if (first_price is None or first_size is None or
                second_price is None or second_size is None):
                return
            if first_price < PRICE_MIN or second_price > PRICE_MAX:
                return
            qty = min(-delta, first_size, second_size)
            if qty <= 0:
                return
            self.pnl.trade(pair["first_ticker"], "short", qty, first_price)
            self.pnl.trade(pair["second_ticker"], "long", qty, second_price)
            self.position[event_ticker] = current - qty
            new_pos = self.position[event_ticker]
            self._fill_count += 1
            self._record_fill(event_ticker, pair["first_ticker"], "first", "sell",
                              qty, first_price, new_pos, signal_value)
            self._record_fill(event_ticker, pair["second_ticker"], "second", "buy",
                              qty, second_price, new_pos, signal_value)
            print(
                f"  FILL [{event_ticker}] {-qty:.0f} → pos={new_pos:.0f} "
                f"first_bid={first_price:.4f} second_ask={second_price:.4f} signal={signal_value:.2f}"
            )

    def _handle_ticker(self, msg: dict):
        ticker = msg["market_ticker"]
        if ticker not in self.tickers:
            return
        state = self.tickers[ticker]
        if "yes_bid_dollars" in msg:
            yb = msg["yes_bid_dollars"]
            state.yes_bid = float(yb) if yb else None
        if "yes_ask_dollars" in msg:
            ya = msg["yes_ask_dollars"]
            state.yes_ask = float(ya) if ya else None
        if "yes_bid_size_fp" in msg:
            ybs = msg["yes_bid_size_fp"]
            state.yes_bid_size = float(ybs) if ybs else None
        if "yes_ask_size_fp" in msg:
            yas = msg["yes_ask_size_fp"]
            state.yes_ask_size = float(yas) if yas else None

    def _handle_trade(self, msg: dict):
        ticker = msg["market_ticker"]
        if ticker not in self.pair_by_ticker:
            return
        event_ticker = self.pair_by_ticker[ticker]
        self.signals[event_ticker].on_message("trade", msg)
        self._check_and_trade(event_ticker)

    async def run(self):
        print(f"Connecting to {WS_URL}...")
        print(f"Monitoring {len(self.pairs)} events, {len(self.all_tickers)} markets")

        while True:
            headers = ws_auth_headers()
            try:
                async with websockets.connect(WS_URL, additional_headers = headers) as ws:
                    sub = {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["ticker", "trade"],
                            "market_tickers": self.all_tickers,
                        },
                    }
                    await ws.send(json.dumps(sub))
                    print(f"Subscribed to {len(self.all_tickers)} tickers")

                    async for raw in ws:
                        data = json.loads(raw)
                        msg_type = data["type"]

                        if msg_type == "ticker":
                            self._handle_ticker(data["msg"])
                        elif msg_type == "trade":
                            self._handle_trade(data["msg"])
                        elif msg_type == "error":
                            print(f"  Server error: {data}")

            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                print(f"Connection error ({type(e).__name__}: {e}), reconnecting in 5s...")
                await asyncio.sleep(5)
                continue

    def print_summary(self):
        print(f"\n{'=' * 60}")
        print("SIGNAL PAPER TRADER SUMMARY")
        print(f"{'=' * 60}")
        print(f"Fills executed: {self._fill_count}")
        print(f"Final positions:")
        for event_ticker, pos in self.position.items():
            if pos != 0:
                print(f"  {event_ticker}: {pos:+.0f}")
        print(self.pnl.summary())
        print(f"Net Total PnL: ${self.pnl.net_total_pnl():.4f}")
        print(f"{'=' * 60}\n")


async def main():
    parser = argparse.ArgumentParser(description = "Live paper trader for paired TradeFillMA sports signals")
    parser.add_argument("-n", "--top-n", type = int, default = 10, help = "Number of top sports pairs")
    parser.add_argument("-c", "--category", type = str, default = "Sports", help = "Event category")
    parser.add_argument("-t", "--threshold", type = float, required = True, help = "Signal threshold for entry")
    parser.add_argument("-l", "--position-limit", type = float, default = 100, help = "Per-pair position limit (contracts each leg)")
    parser.add_argument("-d", "--duration", type = float, default = None, help = "Run for this many hours then exit cleanly (default: run until killed)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
    pos_limit_str = str(args.position_limit).replace(".", "p")
    threshold_str = str(args.threshold).replace(".", "p")
    log_path = OUTPUT_DIR / f"signal_sim_thr_{threshold_str}_limit_{pos_limit_str}.csv"
    log_file = open(log_path, "w", newline = "")
    writer = csv.writer(log_file)
    writer.writerow([
        "local_ts",
        "event_ticker",
        "ticker",
        "title",
        "leg",
        "action",
        "qty",
        "price",
        "signed_position",
        "signal",
        "threshold",
        "realized_pnl",
        "fees_paid",
        "net_total_pnl",
    ])
    log_file.flush()
    print(f"Logging to {log_path}")
    print(f"Threshold: {args.threshold}  Position limit: {args.position_limit}")

    simulator = None
    try:
        pairs = discover_top_pairs(args.top_n, args.category)
        if not pairs:
            print("No matching sports pairs found.")
            return
        simulator = SignalSimulator(pairs, args.threshold, args.position_limit, writer, log_file)
        if args.duration is not None:
            await asyncio.wait_for(simulator.run(), timeout = args.duration * 3600)
        else:
            await simulator.run()
    except asyncio.TimeoutError:
        print(f"\n--- Duration {args.duration}h elapsed, exiting ---\n")
    except KeyboardInterrupt:
        print("\nStopping simulator...")
    finally:
        if simulator is not None:
            simulator.print_summary()
        log_file.close()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
    asyncio.run(main())

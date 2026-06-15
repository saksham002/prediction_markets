"""
Passive paper trader for paired TradeFillMA signals on Kalshi sports winner markets.

Passive variant of sim.py: never cross the book. Rest BUY orders at the best bid
on the appropriate side of each leg. A full fill is assumed the moment the
opposite side of the book crosses the resting price — an optimistic proxy,
kept simple on purpose.

Position management:
  - Bullish-first  (signal >=  threshold): BUY first_YES @ first_YES.bid,
    BUY second_NO @ second_NO.bid (= 1 - second_YES.ask).
  - Bullish-second (signal <= -threshold): BUY first_NO @ first_NO.bid
    (= 1 - first_YES.ask), BUY second_YES @ second_YES.bid.
  - position_limit caps the total shares bet in the active direction, summed
    across both legs (long first_YES + long second_NO for bullish-first,
    mirrored for bullish-second).
  - per_order_size is the qty sent to each of the two legs per placement round.
  - Orders are (re)quoted only on alpha update (trade message) when the
    signal is past threshold; ticker updates never place orders.
  - Fills and fees realize only at fill time, checked on every ticker update.
  - A placement is skipped entirely if either leg's passive bid is outside
    [PRICE_MIN, PRICE_MAX].
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
from src.utils.passive_order import PassiveOrder
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


class PassiveSignalSimulator:
    def __init__(
        self,
        pairs: list[dict],
        threshold: float,
        position_limit: float,
        per_order_size: float,
        writer: csv.writer,
        log_file,
    ):
        self.pairs = pairs
        self.threshold = threshold
        self.position_limit = position_limit
        self.per_order_size = per_order_size
        self.writer = writer
        self.log_file = log_file
        self.pnl = PnL(charge_fees = True, fee_model = "kalshi")
        self.tickers: dict[str, TickerState] = {}
        self.pair_by_event = {pair["event_ticker"] : pair for pair in pairs}
        self.pair_by_ticker: dict[str, str] = {}
        self.signals: dict[str, TradeFillMA] = {}
        # Shares long on YES and NO sides per ticker.
        self.long_yes: dict[str, float] = {}
        self.long_no: dict[str, float] = {}
        # Resting passive orders keyed by (event_ticker, ticker, side).
        self.resting: dict[tuple[str, str, str], PassiveOrder] = {}
        self._fill_count = 0

        for pair in pairs:
            first = pair["first_ticker"]
            second = pair["second_ticker"]
            self.pair_by_ticker[first] = pair["event_ticker"]
            self.pair_by_ticker[second] = pair["event_ticker"]
            self.tickers[first] = TickerState()
            self.tickers[second] = TickerState()
            for tkr in (first, second):
                self.long_yes[tkr] = 0.0
                self.long_no[tkr] = 0.0
            self.signals[pair["event_ticker"]] = TradeFillMA(
                pair_tickers = (first, second),
                half_life_seconds = SIGNAL_HALF_LIFE_SECONDS,
                time_source = "exchange",
            )

        self.all_tickers = list(self.pair_by_ticker.keys())

    def _direction_total(self, event_ticker: str, direction: int) -> float:
        """Total shares betting in the given direction (+1 = bullish_first, -1 = bullish_second)."""
        pair = self.pair_by_event[event_ticker]
        first = pair["first_ticker"]
        second = pair["second_ticker"]
        if direction == 1:
            return self.long_yes[first] + self.long_no[second]
        return self.long_no[first] + self.long_yes[second]

    def _record_fill(self, event_ticker: str, ticker: str, leg: str, side: str,
                     qty: float, price: float, signed_position: float, signal_value: float):
        pair = self.pair_by_event[event_ticker]
        self.writer.writerow([
            Timestamp.now().readable(),
            event_ticker,
            ticker,
            pair["title"],
            leg,
            f"buy_{side}",
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

    def _apply_fill(self, order: PassiveOrder, event_ticker: str, leg: str):
        """Realize a passive fill: update position, charge fees via PnL, log."""
        ticker = order.ticker
        side = order.side
        qty = order.qty
        price = order.price
        pair = self.pair_by_event[event_ticker]
        first = pair["first_ticker"]

        # Track long-by-side for position-limit accounting; book the trade in YES
        # space for PnL (BUY NO @ P == SHORT YES @ 1-P with identical payoffs & fees).
        if side == "yes":
            self.long_yes[ticker] += qty
            self.pnl.trade(ticker, "long", qty, price, is_maker = True)
        else:
            self.long_no[ticker] += qty
            self.pnl.trade(ticker, "short", qty, 1.0 - price, is_maker = True)

        # Direction inferred from (leg, side): first-YES and second-NO bet on A;
        # first-NO and second-YES bet on B. Sign the position accordingly.
        if (ticker == first and side == "yes") or (ticker != first and side == "no"):
            signed_position = self._direction_total(event_ticker, 1)
        else:
            signed_position = -self._direction_total(event_ticker, -1)

        signal_value = self.signals[event_ticker].values_pw()[SIGNAL_LABEL]
        if signal_value is None:
            signal_value = 0.0

        self._fill_count += 1
        self._record_fill(event_ticker, ticker, leg, side, qty, price, signed_position, signal_value)
        print(
            f"  FILL [{event_ticker}] {leg} BUY_{side.upper()} {qty:.0f}@{price:.4f} "
            f"→ signed_pos={signed_position:+.0f} signal={signal_value:.2f}"
        )

    def _check_fills(self, ticker: str):
        """For each resting order on this ticker, fill if opposite side has crossed."""
        if ticker not in self.pair_by_ticker:
            return
        event_ticker = self.pair_by_ticker[ticker]
        state = self.tickers[ticker]
        pair = self.pair_by_event[event_ticker]
        first = pair["first_ticker"]

        to_fill_keys = []
        for key, order in self.resting.items():
            if order.ticker != ticker:
                continue
            if order.check_fill(state.yes_bid, state.yes_ask):
                to_fill_keys.append(key)

        for key in to_fill_keys:
            order = self.resting.pop(key)
            leg = "first" if order.ticker == first else "second"
            self._apply_fill(order, event_ticker, leg)

    def _desired_passive_quotes(self, event_ticker: str, direction: int):
        """
        Desired (ticker, side, price) for each of the two passive legs.
        Returns None if either leg's price is unavailable or outside [PRICE_MIN, PRICE_MAX].
        """
        pair = self.pair_by_event[event_ticker]
        first = pair["first_ticker"]
        second = pair["second_ticker"]
        first_state = self.tickers[first]
        second_state = self.tickers[second]

        if direction == 1:
            first_price = first_state.yes_bid
            second_price = None if second_state.yes_ask is None else 1.0 - second_state.yes_ask
            leg_a = (first, "yes", first_price)
            leg_b = (second, "no", second_price)
        else:
            first_price = None if first_state.yes_ask is None else 1.0 - first_state.yes_ask
            second_price = second_state.yes_bid
            leg_a = (first, "no", first_price)
            leg_b = (second, "yes", second_price)

        for _, _, price in (leg_a, leg_b):
            if price is None:
                return None
            if price < PRICE_MIN or price > PRICE_MAX:
                return None
        return (leg_a, leg_b)

    def _cancel_non_matching(self, event_ticker: str, desired):
        """Cancel resting orders for event_ticker that don't match the desired quotes."""
        if desired is None:
            keys_to_drop = [k for k in self.resting if k[0] == event_ticker]
        else:
            keys_to_drop = []
            for key, order in self.resting.items():
                if key[0] != event_ticker:
                    continue
                matches = any(
                    order.ticker == tkr and order.side == side and order.price == price
                    for (tkr, side, price) in desired
                )
                if not matches:
                    keys_to_drop.append(key)
        for key in keys_to_drop:
            del self.resting[key]

    def _quote_passive(self, event_ticker: str):
        """(Re)quote the pair's passive orders based on the current alpha value."""
        signal_value = self.signals[event_ticker].values_pw()[SIGNAL_LABEL]
        if signal_value is None:
            self._cancel_non_matching(event_ticker, None)
            return

        if signal_value >= self.threshold:
            direction = 1
        elif signal_value <= -self.threshold:
            direction = -1
        else:
            self._cancel_non_matching(event_ticker, None)
            return

        desired = self._desired_passive_quotes(event_ticker, direction)
        if desired is None:
            self._cancel_non_matching(event_ticker, None)
            return

        self._cancel_non_matching(event_ticker, desired)

        # Position-limit check: only place if adding per_order_size to both legs
        # would keep the directional total within position_limit.
        total = self._direction_total(event_ticker, direction)
        if total + 2 * self.per_order_size > self.position_limit:
            return

        for (tkr, side, price) in desired:
            key = (event_ticker, tkr, side)
            if key in self.resting:
                continue  # already resting at the desired price (survived cancel step)
            self.resting[key] = PassiveOrder(
                ticker = tkr,
                side = side,
                price = price,
                qty = self.per_order_size,
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
        # Ticker updates only trigger fills on resting orders; they do not place orders.
        self._check_fills(ticker)

    def _handle_trade(self, msg: dict):
        ticker = msg["market_ticker"]
        if ticker not in self.pair_by_ticker:
            return
        event_ticker = self.pair_by_ticker[ticker]
        self.signals[event_ticker].on_message("trade", msg)
        self._quote_passive(event_ticker)

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
        print("PASSIVE SIGNAL PAPER TRADER SUMMARY")
        print(f"{'=' * 60}")
        print(f"Fills executed: {self._fill_count}")
        print(f"Resting orders at shutdown: {len(self.resting)}")
        print(f"Final positions (long_yes, long_no) per ticker:")
        for tkr in sorted(self.long_yes.keys()):
            ly = self.long_yes[tkr]
            ln = self.long_no[tkr]
            if ly or ln:
                print(f"  {tkr}: long_yes={ly:.0f} long_no={ln:.0f}")
        print(self.pnl.summary())
        print(f"Net Total PnL: ${self.pnl.net_total_pnl():.4f}")
        print(f"{'=' * 60}\n")


async def main():
    parser = argparse.ArgumentParser(
        description = "Passive paper trader for paired TradeFillMA sports signals"
    )
    parser.add_argument("-n", "--top-n", type = int, default = 10, help = "Number of top sports pairs")
    parser.add_argument("-c", "--category", type = str, default = "Sports", help = "Event category")
    parser.add_argument("-t", "--threshold", type = float, required = True, help = "Signal threshold for entry")
    parser.add_argument("-l", "--position-limit", type = float, default = 100,
                        help = "Max total shares bet per direction per event (summed across both legs)")
    parser.add_argument("-s", "--per-order-size", type = float, required = True,
                        help = "Qty of each passive order sent to each of the two legs per placement")
    parser.add_argument("-d", "--duration", type = float, default = None,
                        help = "Run for this many hours then exit cleanly (default: run until killed)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
    pos_limit_str = str(args.position_limit).replace(".", "p")
    threshold_str = str(args.threshold).replace(".", "p")
    order_size_str = str(args.per_order_size).replace(".", "p")
    log_path = (
        OUTPUT_DIR
        / f"signal_sim_pass_thr_{threshold_str}_limit_{pos_limit_str}_ord_{order_size_str}.csv"
    )
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
    print(
        f"Threshold: {args.threshold}  Position limit: {args.position_limit}  "
        f"Per-order size: {args.per_order_size}"
    )

    simulator = None
    try:
        pairs = discover_top_pairs(args.top_n, args.category)
        if not pairs:
            print("No matching sports pairs found.")
            return
        simulator = PassiveSignalSimulator(
            pairs, args.threshold, args.position_limit, args.per_order_size, writer, log_file,
        )
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

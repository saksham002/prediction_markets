"""
3-leg arbitrage scanner for N-outcome events (WC soccer win/draw/win).

Two structures, both riskless at settlement:
  YES-arb: buy YES on all 3 legs. Exactly one pays $1 -> profit/contract =
           1 - sum(yes asks) - 3 taker fees.
  NO-arb:  buy NO on all 3 legs. Exactly two pay $1 -> profit/contract =
           2 - sum(no asks) - 3 taker fees.

Size = min displayed quantity at the touch across the 3 legs. Executions are
aggressive (taking the ask) — the explicitly sanctioned exception to the
passive-only rule, since the basket is riskless.

Replay mode: scans a recording, logs every opportunity window and simulated
profit. (Live mode can reuse the same consumer on LiveFeed later.)
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.replay import Replayer
from src.pnl import PnL

MIN_EDGE = 0.000  # extra margin (dollars/contract) required beyond fees


class ArbScanConsumer:
    def __init__(self, replayer, taker_fee_multiplier = 1.0):
        self.replayer = replayer
        self.events: dict[str, list[str]] = {}
        self.by_ticker: dict[str, str] = {}
        self.fee_mult: dict[str, float] = {}
        self.opportunities: list[dict] = []
        self._open_window: dict[tuple, dict] = {}

    def on_meta(self, lts, meta):
        for ev in meta.get("events", []):
            if len(ev["tickers"]) < 3:
                continue
            self.events[ev["event_ticker"]] = ev["tickers"]
            for t in ev["tickers"]:
                self.by_ticker[t] = ev["event_ticker"]
                self.fee_mult[t] = ev.get("fee_multiplier", 1.0)

    def _taker_fee(self, qty, price, mult):
        import math
        return math.ceil(0.07 * mult * qty * price * (1 - price) * 100) / 100

    def _check(self, lts, event):
        tickers = self.events[event]
        yes_asks, no_asks, yes_qty, no_qty = [], [], [], []
        for t in tickers:
            tob = self.replayer.top(t)
            if tob.yes_ask is None or tob.yes_bid is None:
                return
            yes_asks.append(tob.yes_ask)
            yes_qty.append(tob.yes_ask_qty or 0)
            # NO ask = 1 - yes_bid; size = displayed at the yes bid
            no_asks.append(round(1 - tob.yes_bid, 6))
            no_qty.append(tob.yes_bid_qty or 0)

        for kind, asks, qtys, payout in (("YES", yes_asks, yes_qty, 1.0),
                                         ("NO", no_asks, no_qty, 2.0)):
            total = sum(asks)
            qty = min(qtys)
            key = (event, kind)
            if qty <= 0:
                continue
            fees = sum(self._taker_fee(qty, p, self.fee_mult[t])
                       for p, t in zip(asks, tickers)) / qty
            edge = payout - total - fees
            if edge > MIN_EDGE:
                win = self._open_window.get(key)
                if win is None:
                    self._open_window[key] = {
                        "event": event, "kind": kind, "start": lts, "end": lts,
                        "best_edge": edge, "best_qty": qty, "sum_asks": total,
                    }
                else:
                    win["end"] = lts
                    if edge > win["best_edge"]:
                        win["best_edge"] = edge
                        win["best_qty"] = qty
                        win["sum_asks"] = total
            elif key in self._open_window:
                self.opportunities.append(self._open_window.pop(key))

    def on_book(self, lts, ticker, delta_msg):
        event = self.by_ticker.get(ticker)
        if event is not None:
            self._check(lts, event)

    def on_trade(self, lts, msg):
        pass

    def finish(self):
        self.opportunities.extend(self._open_window.values())
        self._open_window.clear()


def main():
    parser = argparse.ArgumentParser(description = "3-leg arb scan on a recording")
    parser.add_argument("recordings", nargs = "+")
    args = parser.parse_args()

    total = 0
    for rec in args.recordings:
        replayer = Replayer(rec)
        consumer = ArbScanConsumer(replayer)
        replayer.run(consumer)
        consumer.finish()
        print(f"\n{Path(rec).name}: {len(consumer.events)} multi-events scanned, "
              f"{len(consumer.opportunities)} arb windows")
        for o in sorted(consumer.opportunities, key = lambda x: -x["best_edge"] * x["best_qty"])[:15]:
            dur = o["end"] - o["start"]
            profit = o["best_edge"] * o["best_qty"]
            print(f"  {o['kind']:<3} {o['event']:<30} edge={o['best_edge']*100:５.2f}c/ct "
                  f"qty={o['best_qty']:.0f} dur={dur:.1f}s sum={o['sum_asks']:.3f} "
                  f"profit~${profit:.2f}")
        total += len(consumer.opportunities)
    print(f"\nTOTAL arb windows: {total}")


if __name__ == "__main__":
    main()

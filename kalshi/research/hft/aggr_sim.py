"""
Fully aggressive simulator (strategy type "b") on a tick recording.

When the pair alpha crosses +/- threshold, trade the FIRST leg aggressively
toward the target position, taking the ENTIRE displayed quantity at the touch
(buy at best ask / sell at best bid), capped by the remaining distance to the
position limit. Square-off happens when the alpha decays back inside the
threshold (target 0) or flips. Taker fees per series fee schedule.

Single-leg by design: a winner-pair's legs are anti-correlated, so trading
both legs (as research/signals/sim.py does) doubles the same exposure and
doubles fees. Outputs match mm_sim.py (fills.csv + summary.csv) for direct
comparison.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import PairAlphaEngine, SingleAlphaEngine
from research.hft.replay import Replayer
from src.pnl import PnL

OUTPUT_BASE = Path("/data/user_data/saksham3/kalshi_hft/sims")
MARKOUT_HORIZONS_S = [5, 30, 60, 300]


class PairAggr:
    def __init__(self, pair, consumer, params):
        self.pair = pair
        self.consumer = consumer
        self.params = params
        self.first_ticker = pair["first_ticker"]
        self.second_ticker = pair["second_ticker"]
        self.alpha_engine = PairAlphaEngine(
            pair, consumer.replayer.books, combo = getattr(params, "combo", None)
        )
        self.position = 0.0   # signed first-leg YES contracts

    def _target(self, alpha: float) -> float:
        if alpha >= self.params.threshold:
            return self.params.position_limit
        if alpha <= -self.params.threshold:
            return -self.params.position_limit
        return 0.0

    def check_trade(self, lts: float):
        alpha = self.alpha_engine.values(now = lts)[self.params.alpha_name]
        if alpha is None:
            return
        tob = self.consumer.replayer.top(self.first_ticker)
        if tob.yes_bid is None or tob.yes_ask is None or tob.spread is None:
            return
        if tob.spread < 0.005 or tob.spread > self.params.max_spread + 1e-9:
            return
        if tob.yes_bid < self.params.price_min or tob.yes_ask > self.params.price_max:
            return

        target = self._target(alpha)
        delta = target - self.position
        if delta > 0:
            qty = min(delta, tob.yes_ask_qty or 0.0)
            if qty <= 0:
                return
            price = tob.yes_ask
            self.consumer.pnl.trade(self.first_ticker, "long", qty, price, is_maker = False)
            self.position += qty
            side = "buy"
            yes_space_price = price
        elif delta < 0:
            qty = min(-delta, tob.yes_bid_qty or 0.0)
            if qty <= 0:
                return
            price = tob.yes_bid
            self.consumer.pnl.trade(self.first_ticker, "short", qty, price, is_maker = False)
            self.position -= qty
            side = "sell"
            yes_space_price = price
        else:
            return

        self.consumer.log_fill(
            lts, self.pair["event_ticker"], self.first_ticker, side, price,
            yes_space_price, qty, self.position, alpha, tob.mid, tob.spread,
        )


class SingleAggr:
    """Aggressive trade-following on ONE market (soccer legs): when the alpha
    exceeds +/- threshold, take the touch toward +/- limit; flat when the
    alpha decays back inside the threshold. Designed for LARGE thresholds —
    a handful of trades per game."""

    def __init__(self, event_ticker: str, ticker: str, consumer, params):
        self.pair = {"event_ticker": event_ticker}
        self.event_ticker = event_ticker
        self.ticker = ticker
        self.first_ticker = ticker
        self.consumer = consumer
        self.params = params
        self.alpha_engine = SingleAlphaEngine(
            ticker, consumer.replayer.books, combo = getattr(params, "combo", None))
        self.position = 0.0
        self._latch = 0  # hysteresis: -1/0/+1 current latched direction

    def _target(self, alpha: float) -> float:
        # Enter when the alpha crosses +/- threshold; HOLD until it crosses
        # zero in the other direction (no churn while it oscillates near T)
        if self._latch == 0:
            if alpha >= self.params.threshold:
                self._latch = 1
            elif alpha <= -self.params.threshold:
                self._latch = -1
        elif self._latch == 1 and alpha <= 0:
            self._latch = -1 if alpha <= -self.params.threshold else 0
        elif self._latch == -1 and alpha >= 0:
            self._latch = 1 if alpha >= self.params.threshold else 0
        return self._latch * self.params.position_limit

    def check_trade(self, lts: float):
        alpha = self.alpha_engine.value_of(self.params.alpha_name, lts)
        if alpha is None:
            return
        tob = self.consumer.replayer.top(self.ticker)
        if tob.yes_bid is None or tob.yes_ask is None or tob.spread is None:
            return
        if tob.spread < 0.005 or tob.spread > self.params.max_spread + 1e-9:
            return
        if tob.yes_bid < self.params.price_min or tob.yes_ask > self.params.price_max:
            return
        target = self._target(alpha)
        delta = target - self.position
        if delta > 0:
            qty = min(delta, tob.yes_ask_qty or 0.0)
            if qty <= 0:
                return
            price = tob.yes_ask
            self.consumer.pnl.trade(self.ticker, "long", qty, price, is_maker = False)
            self.position += qty
            side, ysp = "buy", price
        elif delta < 0:
            qty = min(-delta, tob.yes_bid_qty or 0.0)
            if qty <= 0:
                return
            price = tob.yes_bid
            self.consumer.pnl.trade(self.ticker, "short", qty, price, is_maker = False)
            self.position -= qty
            side, ysp = "sell", price
        else:
            return
        self.consumer.log_fill(lts, self.event_ticker, self.ticker, side, price,
                               ysp, qty, self.position, alpha, tob.mid, tob.spread)


class AggrSimConsumer:
    def __init__(self, replayer: Replayer, params):
        self.replayer = replayer
        self.params = params
        self.pnl = PnL(charge_fees = True, fee_model = "kalshi")
        self.strategies: dict[str, PairAggr] = {}
        self.strat_by_ticker: dict[str, PairAggr] = {}
        self.mid_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.fill_rows: list[dict] = []
        self.last_mid: dict[str, float] = {}

    def on_meta(self, lts: float, meta):
        series_filter = getattr(self.params, "series", None)
        series_set = ({s.strip() for s in series_filter.split(",") if s.strip()}
                      if series_filter else None)
        pairs = meta.get("pairs", []) if isinstance(meta, dict) else meta
        if series_set is not None:
            pairs = [p for p in pairs if p["series"] in series_set]
        for pair in pairs:
            event_ticker = pair["event_ticker"]
            series = pair["series"]
            self.pnl.series_fees[series] = (pair["fee_multiplier"], pair["fee_type"])
            for tkr in (pair["first_ticker"], pair["second_ticker"]):
                self.pnl.market_to_series[tkr] = series
            if event_ticker in self.strategies:
                continue
            strat = PairAggr(pair, self, self.params)
            self.strategies[event_ticker] = strat
            self.strat_by_ticker[pair["first_ticker"]] = strat
            self.strat_by_ticker[pair["second_ticker"]] = strat
        if isinstance(meta, dict):
            events = meta.get("events", [])
            if series_set is not None:
                events = [e for e in events if e["series"] in series_set]
            for ev in events:
                self.pnl.series_fees[ev["series"]] = (ev["fee_multiplier"], ev["fee_type"])
                for tkr in ev["tickers"]:
                    self.pnl.market_to_series[tkr] = ev["series"]
                    key = f"{ev['event_ticker']}:{tkr}"
                    if key in self.strategies:
                        continue
                    strat = SingleAggr(ev["event_ticker"], tkr, self, self.params)
                    self.strategies[key] = strat
                    self.strat_by_ticker[tkr] = strat

    def _record_mid(self, lts: float, ticker: str):
        tob = self.replayer.top(ticker)
        mid = tob.mid
        if mid is None or self.last_mid.get(ticker) == mid:
            return
        self.last_mid[ticker] = mid
        self.mid_history[ticker].append((lts, mid))

    def on_book(self, lts: float, ticker: str, delta_msg):
        strat = self.strat_by_ticker.get(ticker)
        if strat is None:
            return
        self._record_mid(lts, ticker)
        strat.alpha_engine.on_book(lts, ticker)
        strat.check_trade(lts)

    def on_trade(self, lts: float, msg: dict):
        ticker = msg["market_ticker"]
        strat = self.strat_by_ticker.get(ticker)
        if strat is None:
            return
        strat.alpha_engine.on_trade(lts, msg)
        self._record_mid(lts, ticker)
        strat.check_trade(lts)

    def log_fill(self, lts, event_ticker, ticker, side, price, yes_space_price,
                 qty, position_after, alpha, mid, spread):
        self.fill_rows.append({
            "lts": lts,
            "event_ticker": event_ticker,
            "ticker": ticker,
            "side": side,
            "price": price,
            "yes_space_price": yes_space_price,
            "qty": qty,
            "reason": "taker",
            "inventory_after": position_after,
            "alpha": alpha if alpha is not None else "",
            "mid": mid if mid is not None else "",
            "spread": spread if spread is not None else "",
            "realized_pnl": self.pnl.realized_pnl,
            "fees_paid": self.pnl.fees_paid,
        })


def compute_markouts(consumer):
    hist_arrays = {}
    for ticker, hist in consumer.mid_history.items():
        hist_arrays[ticker] = (np.array([h[0] for h in hist]), np.array([h[1] for h in hist]))
    for row in consumer.fill_rows:
        ticker = row["ticker"]
        direction = 1.0 if row["side"] == "buy" else -1.0
        if ticker not in hist_arrays:
            for h in MARKOUT_HORIZONS_S:
                row[f"markout_{h}s"] = ""
            continue
        ts_arr, mid_arr = hist_arrays[ticker]
        for h in MARKOUT_HORIZONS_S:
            target = row["lts"] + h
            idx = np.searchsorted(ts_arr, target, side = "right") - 1
            if idx < 0 or target > ts_arr[-1]:
                row[f"markout_{h}s"] = ""
            else:
                row[f"markout_{h}s"] = round(
                    (mid_arr[idx] - row["yes_space_price"]) * direction * 100.0, 4
                )


def main():
    parser = argparse.ArgumentParser(description = "Aggressive (taker) simulator on a tick recording")
    parser.add_argument("recording", help = "Path to ticks_*.jsonl.gz")
    parser.add_argument("-t", "--threshold", type = float, required = True)
    parser.add_argument("-l", "--position-limit", type = float, default = 100)
    parser.add_argument("-a", "--alpha-name", type = str, default = "tfma_pw_10s")
    parser.add_argument("--max-spread", type = float, default = 0.01)
    parser.add_argument("--price-min", type = float, default = 0.05)
    parser.add_argument("--price-max", type = float, default = 0.95)
    parser.add_argument("--combo-file", type = str, default = None,
                        help = "combo weights JSON from fit_combo.py (use with -a combo)")
    parser.add_argument("--series", type = str, default = None,
                        help = "Comma-separated series filter")
    parser.add_argument("--tag", type = str, default = None)
    args = parser.parse_args()

    args.combo = None
    if args.combo_file:
        import json
        with open(args.combo_file) as f:
            args.combo = json.load(f)

    if args.tag is None:
        args.tag = (
            f"aggr_{Path(args.recording).name.split('.')[0]}"
            f"_t{args.threshold:g}_l{args.position_limit:g}_{args.alpha_name}"
        )
    out_dir = OUTPUT_BASE / args.tag
    out_dir.mkdir(parents = True, exist_ok = True)

    replayer = Replayer(args.recording)
    consumer = AggrSimConsumer(replayer, args)
    print(f"Replaying {args.recording}...")
    n = replayer.run(consumer)
    print(f"  {n} messages, {len(consumer.strategies)} pairs, {len(consumer.fill_rows)} fills")

    compute_markouts(consumer)

    fill_fields = list(consumer.fill_rows[0].keys()) if consumer.fill_rows else []
    with open(out_dir / "fills.csv", "w", newline = "") as f:
        w = csv.DictWriter(f, fieldnames = fill_fields)
        w.writeheader()
        w.writerows(consumer.fill_rows)

    pnl = consumer.pnl
    last_mids = {t: h[-1][1] for t, h in consumer.mid_history.items() if h}
    contracts = sum(r["qty"] for r in consumer.fill_rows)
    markout_means = {}
    for h in MARKOUT_HORIZONS_S:
        vals = [(r[f"markout_{h}s"], r["qty"]) for r in consumer.fill_rows if r[f"markout_{h}s"] != ""]
        markout_means[h] = (sum(m * q for m, q in vals) / sum(q for _, q in vals)) if vals else float("nan")

    summary = {
        "recording": Path(args.recording).name,
        "per_order_size": "touch",
        "inventory_cap": args.position_limit,
        "skew_threshold": args.threshold,
        "alpha_name": args.alpha_name,
        "max_spread": args.max_spread,
        "n_fills": len(consumer.fill_rows),
        "contracts": contracts,
        "realized_pnl": round(pnl.realized_pnl, 4),
        "fees_paid": round(pnl.fees_paid, 4),
        "unrealized_pnl": round(pnl.mark_to_market(last_mids), 4),
        "net_pnl": round(pnl.net_total_pnl(prices = last_mids), 4),
        "open_contracts_end": round(sum(abs(p.qty) for p in pnl.positions.values()), 2),
        "markout_5s_cents": round(markout_means[5], 4),
        "markout_30s_cents": round(markout_means[30], 4),
        "markout_60s_cents": round(markout_means[60], 4),
        "markout_300s_cents": round(markout_means[300], 4),
    }
    with open(out_dir / "summary.csv", "w", newline = "") as f:
        w = csv.DictWriter(f, fieldnames = list(summary.keys()))
        w.writeheader()
        w.writerow(summary)

    print(f"\nWrote {out_dir}/fills.csv, summary.csv")
    print(f"\n{'=' * 64}\nAGGRESSIVE SIM SUMMARY  (tag {args.tag})")
    for k, v in summary.items():
        print(f"  {k:<24} {v}")
    print("=" * 64)


if __name__ == "__main__":
    main()

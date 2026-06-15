"""Aggregation alpha study: signed EMA over trades, new resting orders, and
PURE cancels (negative deltas not explained by a trade within the pairing
window), in each leg's own YES space.

Signs (YES space): trade taker=yes +, taker=no -; new buy-YES +, new buy-NO -;
pure cancel of buy-YES -, pure cancel of buy-NO +.
Weights: w_trade/w_cancel/w_new (default 1/3 each); trades use level factor 1,
new/cancel use 1/k where k = price-level rank from the best bid (best = 1).

Outputs: corr of agg_{hl} vs 120s/180s forward return (WC pool) per half-life,
and lead-lag cross-correlation of the best agg HL vs tfma_30s (raw + pw) and
OBI — testing the hypothesis that the aggregation alpha LEADS both.
"""
import math
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from alphas import market_obi
from plot_alpha import DATASET, GAMES
from replay import Replayer

HLS = {"1s": 1.0, "5s": 5.0, "10s": 10.0, "30s": 30.0, "60s": 60.0, "300s": 300.0}
W_TRADE = W_CANCEL = W_NEW = 1.0 / 3.0
PENDING_WINDOW_S = 1.0
HORIZONS = (120.0, 180.0)
LAGS = list(range(-30, 31, 5))


class AggFlowConsumer:
    def __init__(self, replayer):
        self.replayer = replayer
        self.tickers: set = set()
        self.decays = {k: math.log(2) / v for k, v in HLS.items()}
        self.ema = defaultdict(lambda: {k: 0.0 for k in HLS})       # agg alpha
        self.tfma_raw = defaultdict(float)                           # 30s raw
        self.tfma_pw = defaultdict(float)                            # 30s taker-pw
        self.last_ts: dict = {}
        self.pending = defaultdict(deque)  # ticker -> (ts, side, price, qty, k)
        self.samples = defaultdict(list)
        self._last_sample: dict = {}

    def on_meta(self, lts, meta):
        for ev in meta.get("events", []):
            if ev["series"] == "KXWCGAME":
                self.tickers.update(ev["tickers"])

    def _decay_to(self, tkr, ts):
        last = self.last_ts.get(tkr)
        if last is not None:
            dt = max(ts - last, 0.0)
            for k, d in self.decays.items():
                self.ema[tkr][k] *= math.exp(-d * dt)
            f30 = math.exp(-self.decays["30s"] * dt)
            self.tfma_raw[tkr] *= f30
            self.tfma_pw[tkr] *= f30
        self.last_ts[tkr] = ts

    def _add(self, tkr, contribution):
        for k in HLS:
            self.ema[tkr][k] += contribution

    def _level_rank(self, tkr, side, price):
        """Rank of price among occupied bid levels on `side` (best = 1)."""
        book = self.replayer.books[tkr]
        bside = book.yes if side == "yes" else book.no
        best, _ = bside.best_bid()
        if best is None:
            return 1
        better = sum(1 for p, q in bside.levels.items() if q > 0 and float(p) > price)
        return better + 1

    def _flush_pending(self, tkr, now):
        """Expire unmatched reductions -> pure cancels."""
        dq = self.pending[tkr]
        while dq and now - dq[0][0] > PENDING_WINDOW_S:
            ts, side, price, qty, k = dq.popleft()
            if qty <= 0:
                continue
            sign = -1.0 if side == "yes" else 1.0
            self._decay_to(tkr, ts)
            self._add(tkr, sign * qty * W_CANCEL / k)

    def on_book(self, lts, ticker, msg):
        if ticker not in self.tickers or msg is None:
            return  # snapshot or out-of-scope market
        m = msg  # replayer passes the inner delta msg directly
        ts = float(m.get("ts_ms", lts * 1000)) / 1000.0
        side = m["side"]
        price = float(m["price_dollars"])
        delta = float(m["delta_fp"])
        self._flush_pending(ticker, ts)
        if delta > 0:
            k = self._level_rank(ticker, side, price)
            sign = 1.0 if side == "yes" else -1.0
            self._decay_to(ticker, ts)
            self._add(ticker, sign * delta * W_NEW / k)
        else:
            k = self._level_rank(ticker, side, price)
            self.pending[ticker].append((ts, side, price, -delta, k))
        self._sample(lts, ticker)

    def on_trade(self, lts, msg):
        tkr = msg["market_ticker"]
        if tkr not in self.tickers:
            return
        qty = float(msg["count_fp"])
        side = msg["taker_side"]
        if "yes_price_dollars" in msg:
            yes_p = float(msg["yes_price_dollars"])
        else:
            yes_p = 1.0 - float(msg["no_price_dollars"])
        ts = float(msg.get("ts", lts))
        self._flush_pending(tkr, ts)
        # Net this trade against pending reductions on the consumed book side
        consumed_side = "no" if side == "yes" else "yes"
        consumed_price = round(1.0 - yes_p, 6) if side == "yes" else yes_p
        remaining = qty
        for i, (pts, pside, pprice, pqty, pk) in enumerate(self.pending[tkr]):
            if remaining <= 0:
                break
            if pside == consumed_side and abs(pprice - consumed_price) < 1e-9:
                used = min(pqty, remaining)
                self.pending[tkr][i] = (pts, pside, pprice, pqty - used, pk)
                remaining -= used
        sign = 1.0 if side == "yes" else -1.0
        self._decay_to(tkr, ts)
        self._add(tkr, sign * qty * W_TRADE)
        self.tfma_raw[tkr] += sign * qty
        taker_p = yes_p if side == "yes" else 1.0 - yes_p
        self.tfma_pw[tkr] += sign * qty * taker_p
        self._sample(lts, tkr)

    def _sample(self, lts, tkr):
        if lts - self._last_sample.get(tkr, 0.0) < 1.0:
            return
        top = self.replayer.top(tkr)
        mid = top.mid
        if mid is None:
            return
        book = self.replayer.books[tkr]
        obi = market_obi(book)
        last = self.last_ts.get(tkr)
        dt = max(lts - last, 0.0) if last is not None else 0.0
        row = [lts, mid]
        for k, d in self.decays.items():
            row.append(self.ema[tkr][k] * math.exp(-d * dt))
        f30 = math.exp(-self.decays["30s"] * dt)
        row.append(self.tfma_raw[tkr] * f30)
        row.append(self.tfma_pw[tkr] * f30)
        row.append(obi if obi is not None else np.nan)
        self.samples[tkr].append(row)
        self._last_sample[tkr] = lts


def main():
    cols = ["lts", "mid"] + [f"agg_{k}" for k in HLS] + ["tfma_raw_30s", "tfma_pw_30s", "obi"]
    series = []
    for game, g in GAMES.items():
        replayer = Replayer(DATASET / g["file"])
        consumer = AggFlowConsumer(replayer)
        replayer.run(consumer)
        for tkr, rows in consumer.samples.items():
            series.append(np.array(rows))
        print(f"{game}: {sum(len(r) for r in consumer.samples.values())} samples, "
              f"{len(consumer.samples)} legs")

    ci = {c: i for i, c in enumerate(cols)}
    print(f"\ncorr vs forward return, WC pool (w_t = w_c = w_n = 1/3, 1/k levels):")
    print(f"{'alpha':>14} " + " ".join(f"{int(h)}s_fwd" for h in HORIZONS))
    best, best_r = None, 0.0
    for c in [f"agg_{k}" for k in HLS] + ["tfma_raw_30s", "tfma_pw_30s", "obi"]:
        rs = []
        for h in HORIZONS:
            xs, ys = [], []
            for arr in series:
                ts = arr[:, 0]
                idx = np.searchsorted(ts, ts + h, side = "right") - 1
                valid = (idx >= 0) & (ts + h <= ts[-1]) & ~np.isnan(arr[:, ci[c]])
                xs.append(arr[valid, ci[c]])
                ys.append((arr[idx[valid], 1] - arr[valid, 1]) * 100.0)
            x, y = np.concatenate(xs), np.concatenate(ys)
            rs.append(float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 else float("nan"))
        print(f"{c:>14} " + " ".join(f"{r:+.4f}" for r in rs))
        if c.startswith("agg_") and abs(rs[0]) > abs(best_r):
            best, best_r = c, rs[0]
    print(f"\nbest agg HL: {best} (r={best_r:+.4f} @120s)")

    for agg_col in dict.fromkeys([best, "agg_30s"]):
        print(f"\nlead-lag: corr({agg_col}(t), X(t+lag)) — positive-lag peak => agg LEADS X")
        print(f"{'lag_s':>6} {'tfma_raw_30s':>13} {'tfma_pw_30s':>12} {'obi':>8}")
        for lag in LAGS:
            out = []
            for other in ("tfma_raw_30s", "tfma_pw_30s", "obi"):
                xs, ys = [], []
                for arr in series:
                    ts = arr[:, 0]
                    idx = np.searchsorted(ts, ts + lag, side = "right") - 1
                    ok = (idx >= 0) & (idx < len(ts))
                    if lag >= 0:
                        ok &= ts + lag <= ts[-1]
                    a = arr[ok, ci[agg_col]]
                    b = arr[idx[ok], ci[other]]
                    m = ~np.isnan(a) & ~np.isnan(b)
                    xs.append(a[m])
                    ys.append(b[m])
                x, y = np.concatenate(xs), np.concatenate(ys)
                out.append(float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else float("nan"))
            print(f"{lag:>6} {out[0]:>13.4f} {out[1]:>12.4f} {out[2]:>8.4f}")


if __name__ == "__main__":
    main()

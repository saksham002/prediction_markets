"""
AggFlowMA: EMA aggregation of trades, new resting orders, and PURE cancels.

Per event, in YES space of the (first) market:
  trade taker=yes +, taker=no -          (weight w_trade, level factor 1)
  new resting buy-YES +, buy-NO -        (weight w_new x level factor)
  pure cancel of buy-YES -, of buy-NO +  (weight w_cancel x level factor)

Pure cancel = negative orderbook delta NOT explained by a trade within the
pairing window (Kalshi's orderbook_delta carries no cancel/fill flag, so this
inference is the only mechanism; recordings show delta-before-trade ordering).

Level factors for new/cancel, tracked as two parallel EMA sets:
  level-weighted:  1/k where k = price-level rank from the best bid (best = 1)
  price-weighted:  the level's own-market YES-space price

Pair mode mirrors the second ticker (A-YES and B-NO are the same direction),
as in TradeFillMA.
"""

import math
from collections import deque

from src.utils.feps import is_pos, lte


PENDING_WINDOW_S = 1.0


class AggFlowMA:
    def __init__(self, view, ticker: str | None = None, *,
                 pair_tickers: tuple[str, str] | None = None,
                 half_life_seconds: dict[str, float],
                 w_trade: float = 1 / 3, w_cancel: float = 1 / 3, w_new: float = 1 / 3):
        if (ticker is None) == (pair_tickers is None):
            raise ValueError("Provide exactly one of ticker or pair_tickers")
        # MarketView (market-only level reads); behaviour-neutral in sim
        self.view = view
        self.single_ticker = ticker
        self.pair_tickers = pair_tickers
        self.w_trade = w_trade
        self.w_cancel = w_cancel
        self.w_new = w_new
        self._decays = {label: math.log(2) / hl for label, hl in half_life_seconds.items()}
        self._ema_lvl = {label: 0.0 for label in self._decays}
        self._ema_pw = {label: 0.0 for label in self._decays}
        # Gross (unsigned) EMAs at the same decay -> net/gross flow-imbalance
        # ratio in [-1,1] (scale-free; the obi analog for flow)
        self._ema_lvl_gross = {label: 0.0 for label in self._decays}
        self._ema_pw_gross = {label: 0.0 for label in self._decays}
        self._last_time: float | None = None
        self._pending: deque = deque()  # (ts, ticker, side, price_f, qty, lvl_f, pw_f)
        self._n_events = 0

    def _sign(self, ticker: str, side: str) -> float | None:
        if self.single_ticker is not None:
            if ticker != self.single_ticker:
                return None
            return 1.0 if side == "yes" else -1.0
        first, second = self.pair_tickers
        if ticker == first:
            return 1.0 if side == "yes" else -1.0
        if ticker == second:
            return -1.0 if side == "yes" else 1.0
        return None

    def _decay_to(self, ts: float):
        if self._last_time is not None:
            dt = max(ts - self._last_time, 0.0)
            if dt > 0:
                for label, d in self._decays.items():
                    f = math.exp(-d * dt)
                    self._ema_lvl[label] *= f
                    self._ema_pw[label] *= f
                    self._ema_lvl_gross[label] *= f
                    self._ema_pw_gross[label] *= f
        self._last_time = ts

    def _add(self, ts: float, lvl_contribution: float, pw_contribution: float):
        self._decay_to(ts)
        for label in self._decays:
            self._ema_lvl[label] += lvl_contribution
            self._ema_pw[label] += pw_contribution
            self._ema_lvl_gross[label] += abs(lvl_contribution)
            self._ema_pw_gross[label] += abs(pw_contribution)
        self._n_events += 1

    def _level_factors(self, ticker: str, side: str, price_f: float) -> tuple[float, float]:
        """(1/k rank factor, own-market YES-space price) for a level. Reads
        market-only levels (our own resting qty excluded when live)."""
        levels = self.view.market_levels(ticker, side)
        better = sum(1 for p, q in levels.items() if is_pos(q) and float(p) > price_f)
        yes_price = price_f if side == "yes" else 1.0 - price_f
        return 1.0 / (better + 1), yes_price

    def _flush_pending(self, now: float):
        while self._pending and now - self._pending[0][0] > PENDING_WINDOW_S:
            ts, ticker, side, price_f, qty, lvl_f, pw_f = self._pending.popleft()
            if lte(qty, 0):
                continue
            sign = self._sign(ticker, side)
            cancel_sign = -sign  # pulled liquidity is the opposite signal
            self._add(ts, cancel_sign * qty * self.w_cancel * lvl_f,
                      cancel_sign * qty * self.w_cancel * pw_f)

    def on_delta(self, ticker: str, side: str, price_f: float, delta: float, ts: float):
        if self._sign(ticker, side) is None:
            return
        self._flush_pending(ts)
        if delta > 0:
            lvl_f, pw_f = self._level_factors(ticker, side, price_f)
            sign = self._sign(ticker, side)
            self._add(ts, sign * delta * self.w_new * lvl_f,
                      sign * delta * self.w_new * pw_f)
        else:
            lvl_f, pw_f = self._level_factors(ticker, side, price_f)
            self._pending.append((ts, ticker, side, price_f, -delta, lvl_f, pw_f))

    def on_trade(self, msg: dict):
        ticker = msg["market_ticker"]
        side = msg["taker_side"]
        sign = self._sign(ticker, side)
        if sign is None:
            return
        qty = float(msg["count_fp"])
        if "yes_price_dollars" in msg:
            yes_p = float(msg["yes_price_dollars"])
        else:
            yes_p = 1.0 - float(msg["no_price_dollars"])
        ts = float(msg["ts"]) if msg.get("ts") is not None else (self._last_time or 0.0)
        self._flush_pending(ts)
        # Net this trade against pending reductions it explains (consumed book
        # side is the opposite of the taker side, on the same market)
        consumed_side = "no" if side == "yes" else "yes"
        consumed_price = round(1.0 - yes_p, 6) if side == "yes" else yes_p
        remaining = qty
        for i, ent in enumerate(self._pending):
            if remaining <= 0:
                break
            pts, ptkr, pside, pprice, pqty, plvl, ppw = ent
            if ptkr == ticker and pside == consumed_side and abs(pprice - consumed_price) < 1e-9:
                used = min(pqty, remaining)
                self._pending[i] = (pts, ptkr, pside, pprice, pqty - used, plvl, ppw)
                remaining -= used
        self._add(ts, sign * qty * self.w_trade, sign * qty * self.w_trade)

    def _values(self, ema: dict, now: float | None) -> dict[str, float | None]:
        if self._n_events == 0:
            return {label: None for label in ema}
        dt = max(now - self._last_time, 0.0) if (now is not None and self._last_time is not None) else 0.0
        out = {}
        for label, d in self._decays.items():
            out[label] = ema[label] * math.exp(-d * dt) if dt > 0 else ema[label]
        return out

    def values_lvl(self, now: float | None = None) -> dict[str, float | None]:
        return self._values(self._ema_lvl, now)

    def values_pw(self, now: float | None = None) -> dict[str, float | None]:
        return self._values(self._ema_pw, now)

    def _values_ratio(self, ema, gross):
        if self._n_events == 0:
            return {label: None for label in ema}
        return {label: (ema[label] / g if is_pos(g := gross[label]) else None) for label in ema}

    def values_lvl_ratio(self, now: float | None = None) -> dict[str, float | None]:
        """Net/gross level-weighted flow ratio in [-1,1] (decay cancels -> staleness-invariant)."""
        return self._values_ratio(self._ema_lvl, self._ema_lvl_gross)

    def values_pw_ratio(self, now: float | None = None) -> dict[str, float | None]:
        """Net/gross price-weighted flow ratio in [-1,1]."""
        return self._values_ratio(self._ema_pw, self._ema_pw_gross)

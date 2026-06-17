"""
Passive market-making simulator (strategy type "a": never crosses the book)
on Kalshi paired sports markets, driven by a raw tick recording.

Per leg of each pair, rest fixed-size BUY orders on the YES and/or NO side,
joining the current best bid (queue position modeled by PassiveFillEngine).
A pair-space alpha skews quoting:

  leg_alpha = +alpha on the first leg, -alpha on the second
  YES bid on  iff (leg_alpha > -T and inventory + S <= cap) or inventory < 0
  NO  bid on  iff (leg_alpha < +T and inventory - S >= -cap) or inventory > 0

so the adding side is pulled when the alpha is against it or the inventory
cap is hit, while the reducing side is always quoted (fast passive square-off).
Legs are quoted only when the spread is <= --max-spread and the price is
inside [0.05, 0.95]. Fees: maker schedule per series (from recording meta).

Outputs under /data/user_data/saksham3/kalshi_hft/sims/<tag>/:
  fills.csv     every simulated fill with alpha/mid/markout context
  summary.csv   one row of params + aggregate metrics (for grid collection)
"""

import argparse
import csv
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import PairAlphaEngine, SingleAlphaEngine, market_obi
from research.hft.passive_fill import FORWARD_DELAY_S, PassiveFillEngine, price_key
from src.utils.feps import is_pos, is_neg, is_zero, gte, lte
from research.hft.replay import Replayer
from src.pnl import PnL

OUTPUT_BASE = Path("/data/user_data/saksham3/kalshi_hft/sims")
MARKOUT_HORIZONS_S = [5, 30, 60, 300]
STATE_LOG_INTERVAL_S = 5.0


class WriteRateLimiter:
    """Kalshi basic tier allows 10 write ops/sec (orders + cancels). Quote
    actions beyond the rolling-1s budget are skipped and retried on a later
    event — the same backpressure a live trader would face."""

    def __init__(self, max_per_sec: float = 10):
        self.max = max_per_sec
        self.stamps: deque = deque()

    def try_acquire(self, now: float, n: int = 1) -> bool:
        while self.stamps and now - self.stamps[0] >= 1.0:
            self.stamps.popleft()
        if len(self.stamps) + n > self.max:
            return False
        for _ in range(n):
            self.stamps.append(now)
        return True


class PairMM:
    def __init__(self, pair, consumer, params):
        self.pair = pair
        self.consumer = consumer
        self.params = params
        self.first_ticker = pair["first_ticker"]
        self.second_ticker = pair["second_ticker"]
        self.alpha_engine = PairAlphaEngine(
            pair, consumer.replayer.books, combo = getattr(params, "combo", None),
            track_agg = "agg" in params.alpha_name,
        )
        self.inventory: dict[str, float] = {self.first_ticker: 0.0, self.second_ticker: 0.0}
        # (ticker, side) -> list of resting order ids (>1 with --ladder)
        self.resting: dict[tuple[str, str], list[int]] = {}

    def _leg_tob(self, ticker: str):
        return self.consumer.replayer.top(ticker)

    def _pair_exposure(self) -> float:
        """Net pair-space exposure: positive = net long first team."""
        return self.inventory[self.first_ticker] - self.inventory[self.second_ticker]

    def _exit_quotes(self, ticker: str) -> dict:
        """Liquidation-only passive quotes (same-ticker netting), never priced
        worse than the aggressive entry VWAP -/+ aggro_profit (when known)."""
        p = self.params
        tob = self._leg_tob(ticker)
        if tob.yes_bid is None or tob.yes_ask is None or tob.spread is None:
            return {}
        if tob.spread < 0.005 or tob.spread > p.max_spread + 1e-9:
            return {}
        tkr_inv = self.inventory[ticker]
        entry_px = getattr(self, "_entry_px", None)
        if tkr_inv < 0:
            # Buy back below entry: never bid above entry - profit_offset
            px = tob.yes_bid
            if entry_px is not None:
                px = min(px, round(entry_px - p.aggro_profit, 6))
            if px < 0.01:
                return {}
            return {"yes": [(px, min(p.per_order_size, -tkr_inv))]}
        if tkr_inv > 0:
            # Sell above entry: never offer below entry + profit_offset
            px = tob.yes_ask
            if entry_px is not None:
                px = max(px, round(entry_px + p.aggro_profit, 6))
            if px > 0.99:
                return {}
            return {"no": [(round(1.0 - px, 6), min(p.per_order_size, tkr_inv))]}
        return {}

    def _liquidate_quotes(self, ticker: str) -> dict:
        """Reduce-only passive quotes (NO alpha, NO price-band restriction) to
        work out of a non-zero position when the touch is outside [price_min,
        price_max] and normal alpha-MM is suppressed. {} when flat."""
        tob = self._leg_tob(ticker)
        if tob.yes_bid is None or tob.yes_ask is None:
            return {}
        inv = self.inventory[ticker]
        S = self.params.per_order_size
        if is_neg(inv):                       # short yes -> bid to cover at the touch
            return {"yes": [(tob.yes_bid, min(S, -inv))]}
        if is_pos(inv):                       # long yes -> offer to sell at the touch
            return {"no": [(round(1.0 - tob.yes_ask, 6), min(S, inv))]}
        return {}

    def _desired_sides(self, ticker: str, leg_alpha: float, lts: float = 0.0) -> dict:
        """Map of side -> [(price, size)] quotes to rest, per the skew rules."""
        p = self.params
        # Dataset rule: no trading earlier than 1h before the game starts
        game_starts = getattr(p, "game_starts", None)
        if game_starts:
            start = game_starts.get(self.pair["event_ticker"])
            if start is not None and lts and lts < start - 3600:
                return {}
        # Hybrid aggro-entry mode: passive quotes are EXIT-ONLY (same-ticker
        # netting); entries happen aggressively in requote's latch block
        if getattr(p, "aggro_entry", None) or getattr(p, "aggro_neg", None):
            return self._exit_quotes(ticker)
        tob = self._leg_tob(ticker)
        if tob.yes_bid is None or tob.yes_ask is None:
            return {}
        if tob.spread is None or tob.spread > p.max_spread + 1e-9:
            return {}
        # Never quote into a transiently crossed/locked book (mid-sweep states)
        if tob.spread < 0.005:
            return {}
        if tob.yes_bid < p.price_min or tob.yes_ask > p.price_max:
            # outside the quotable band: no alpha MM, but still work out of any
            # open position (reduce-only, no alpha); {} when flat
            return self._liquidate_quotes(ticker)

        # Global budget guard. Note cross-leg "reducing" fills still consume
        # cash (locked pairs), so over budget we allow ONLY same-ticker
        # netting quotes (they free cash), sized to the open position. The
        # 0.6*S term pre-reserves room for one more fill at typical prices.
        budget = getattr(p, "budget", None)
        S = p.per_order_size
        over_budget = (
            budget is not None
            and self.consumer._deployed_dollars() + 0.6 * S > budget
        )

        # Risk variable the caps/skew operate on: per-ticker inventory by
        # default, or net pair exposure mapped into this leg's yes-space
        # (--pair-risk: buying YES here and NO on the other leg are the same
        # pair direction, so either leg's fills can flatten it).
        if p.pair_risk:
            sign = 1.0 if ticker == self.first_ticker else -1.0
            inv = self._pair_exposure() * sign
        else:
            inv = self.inventory[ticker]
        S = p.per_order_size
        yes_price = tob.yes_bid
        no_price = round(1.0 - tob.yes_ask, 6)

        # Improve mode: with a 2+ tick spread, step one side 1 tick inside for
        # instant queue priority (still passive; capture = spread - 1 tick).
        # Prefer the inventory-reducing side, else the alpha-favored side.
        if p.improve and tob.spread >= 0.019:
            if is_pos(inv):
                improve_side = "no"
            elif is_neg(inv):
                improve_side = "yes"
            elif leg_alpha >= p.skew_threshold:
                improve_side = "yes"
            elif leg_alpha <= -p.skew_threshold:
                improve_side = "no"
            else:
                improve_side = None
            if improve_side == "yes":
                yes_price = round(yes_price + 0.01, 6)
            elif improve_side == "no":
                no_price = round(no_price + 0.01, 6)

        # Alpha-proportional sizing: scale the order toward S as |alpha|
        # approaches size_ref (floor 10% of S). Position limits stay fixed.
        size = S
        size_ref = getattr(p, "size_ref", None)
        if size_ref:
            size = max(0.1 * S, S * min(1.0, abs(leg_alpha) / size_ref))

        if yes_price > p.price_max + 1e-9:
            yes_price = None  # improve step pushed the quote outside bounds
        if no_price > 1.0 - p.price_min + 1e-9:
            no_price = None

        # Quote shaping for the exposure-ADDING side only; the reducing side
        # always joins best for the fastest passive square-off.
        depth_quote = getattr(p, "depth_quote", False)
        ladder = getattr(p, "ladder", False)

        def shape(side: str, price: float | None, sz: float) -> list[tuple[float, float]]:
            if price is None:
                return []
            adding = (side == "yes" and gte(inv, 0)) or (side == "no" and lte(inv, 0))
            floor = p.price_min if side == "yes" else 1.0 - p.price_max
            deeper = round(price - 0.01, 6)
            if adding and ladder and deeper >= floor - 1e-9:
                return [(price, sz / 2), (deeper, sz / 2)]
            if adding and depth_quote:
                if deeper >= floor - 1e-9:
                    return [(deeper, sz)]
                return []
            return [(price, sz)]

        if over_budget:
            # Only same-ticker netting frees cash; cap size at the open
            # position so the net never flips into new (cash-consuming) risk
            tkr_inv = self.inventory[ticker]
            if is_neg(tkr_inv) and yes_price is not None:
                return {"yes": [(yes_price, min(size, -tkr_inv))]}
            if is_pos(tkr_inv) and no_price is not None:
                return {"no": [(no_price, min(size, tkr_inv))]}
            return {}

        # square_off: the market-maker stance that always keeps the reducing
        # side quoted so inventory mean-reverts to flat. When off, only the
        # alpha-favored side is quoted (within position limits) and signal-
        # aligned inventory is allowed to ride to the cap. A pure-reduce order
        # is capped to |inv| so a fill flattens the position, never flips it.
        square_off = getattr(p, "square_off", False)
        desired = {}
        add_yes = leg_alpha > -p.skew_threshold and inv + S <= p.inventory_cap
        reduce_yes = square_off and is_neg(inv)
        if yes_price is not None and (add_yes or reduce_yes):
            sz = size if add_yes else min(size, -inv)
            desired["yes"] = shape("yes", yes_price, sz)
        add_no = leg_alpha < p.skew_threshold and inv - S >= -p.inventory_cap
        reduce_no = square_off and is_pos(inv)
        if no_price is not None and (add_no or reduce_no):
            sz = size if add_no else min(size, inv)
            desired["no"] = shape("no", no_price, sz)
        return desired

    def _leg_alpha_per_leg(self, lts: float, ticker: str, leg_sign: float) -> float:
        # Per-leg mode: the leg's OWN book imbalance (no pair averaging),
        # gated by sign agreement with the leg-signed pair momentum
        obi_leg = market_obi(self.consumer.replayer.books[ticker])
        mom = self.alpha_engine.value_of("mom_5s", lts)
        if obi_leg is None or mom is None:
            return 0.0
        if obi_leg * (mom * leg_sign) > 0:
            return obi_leg
        return 0.0

    def requote(self, lts: float):
        fill_engine = self.consumer.fill_engine
        max_queue = getattr(self.params, "max_queue_ahead", None)
        limiter = self.consumer.rate_limiter
        per_leg = getattr(self.params, "per_leg_alpha", False)
        if not per_leg:
            # Single lazy alpha computation per event, mirrored across legs
            base = self.alpha_engine.value_of(self.params.alpha_name, lts)
            base = 0.0 if base is None else base
        event = self.pair["event_ticker"]
        expo = self._pair_exposure()
        for ticker, leg_sign in ((self.first_ticker, 1.0), (self.second_ticker, -1.0)):
            leg_alpha = (self._leg_alpha_per_leg(lts, ticker, leg_sign)
                         if per_leg else base * leg_sign)
            desired = self._desired_sides(ticker, leg_alpha, lts)
            for side in ("yes", "no"):
                key = (ticker, side)
                want_by_key = {price_key(p): s for p, s in desired.get(side, [])}
                kept = []
                for oid in self.resting.get(key, []):
                    order = fill_engine.orders.get(oid)
                    if order is None:
                        continue
                    # Passive invariant: only rest where real liquidity backs the
                    # level; a level emptied by a cancel is unsupported -> pull it
                    supported = is_pos(fill_engine.displayed(ticker, side, order.price))
                    if supported and order.price in want_by_key:
                        kept.append(oid)               # keep queue position
                        del want_by_key[order.price]
                    elif limiter.try_acquire(lts):
                        fill_engine.cancel(oid)
                        self.consumer.log_order(lts, event, ticker, side, "cancel",
                                                order.price_f, order.remaining, leg_alpha,
                                                order.queue_ahead, expo)
                    else:
                        kept.append(oid)               # rate-limited: cancel retries later
                        self.consumer.log_order(lts, event, ticker, side, "rate_limited",
                                                order.price_f, order.remaining, leg_alpha,
                                                order.queue_ahead, expo)
                for pk, size in want_by_key.items():
                    # Passive invariant: never create a price level with no real
                    # backing (displayed <= 0, e.g. an improve-inside quote)
                    if lte(fill_engine.displayed(ticker, side, pk), 0):
                        continue
                    # Queue-depth guard: a fresh join behind a huge displayed
                    # queue only fills when the level breaks (worst selection)
                    if max_queue is not None and fill_engine.displayed(ticker, side, pk) > max_queue:
                        continue
                    if not limiter.try_acquire(lts):
                        continue                        # rate-limited: place retries later
                    oid = fill_engine.place(lts, ticker, side, float(pk), size)
                    kept.append(oid)
                    self.consumer.log_order(lts, event, ticker, side, "place",
                                            float(pk), size, leg_alpha,
                                            fill_engine.orders[oid].queue_ahead, expo)
                if kept:
                    self.resting[key] = kept
                else:
                    self.resting.pop(key, None)

    def on_fill(self, fill):
        order = fill.order
        ticker = order.ticker
        price = order.price_f
        if order.side == "yes":
            self.inventory[ticker] += fill.qty
            yes_space_price = price
            self.consumer.pnl.trade(ticker, "long", fill.qty, price, is_maker = True)
        else:
            self.inventory[ticker] -= fill.qty
            yes_space_price = round(1.0 - price, 6)
            self.consumer.pnl.trade(ticker, "short", fill.qty, yes_space_price, is_maker = True)
        if lte(order.remaining, 0):
            key = (ticker, order.side)
            oids = [oid for oid in self.resting.get(key, []) if oid != order.order_id]
            if oids:
                self.resting[key] = oids
            else:
                self.resting.pop(key, None)

        alpha = self.alpha_engine.value_of(self.params.alpha_name, fill.lts)
        tob = self._leg_tob(ticker)
        self.consumer.log_fill(
            fill, self.pair["event_ticker"], yes_space_price,
            self.inventory[ticker], alpha, tob.mid, tob.spread,
        )
        self.consumer.log_order(fill.lts, self.pair["event_ticker"], ticker, order.side,
                                "fill", order.price_f, fill.qty, alpha,
                                order.queue_ahead, self._pair_exposure())


class SingleMM:
    """Passive MM on ONE market of an N-outcome event (soccer win/draw/win).
    Same quoting rules as PairMM but signals and risk are per-market: the
    pair mirror and cross-leg netting do not exist here."""

    def __init__(self, event_ticker: str, ticker: str, consumer, params):
        self.pair = {"event_ticker": event_ticker}
        self.event_ticker = event_ticker
        self.ticker = ticker
        # PairMM._desired_sides pair-risk branch reads these; with a single
        # market the pair exposure degenerates to plain inventory
        self.first_ticker = ticker
        self.second_ticker = None
        self.consumer = consumer
        self.params = params
        self.alpha_engine = SingleAlphaEngine(
            ticker, consumer.replayer.books, combo = getattr(params, "combo", None),
            track_agg = "agg" in params.alpha_name,
            track_obi_ma = ("obi_ma" in params.alpha_name or "obi_dev" in params.alpha_name))
        self.inventory: dict[str, float] = {ticker: 0.0}
        self.resting: dict[tuple[str, str], list[int]] = {}
        self._latch = 0  # aggro-entry hysteresis direction
        self._armed = False  # entry allowed only until liquidation starts
        self._peak = 0.0  # peak |inventory| since latch set
        self._entry_px = None  # aggressive entry VWAP (YES price) for TP/SL
        self._sibs = None  # other legs of the same event (lazy)

    def _siblings(self):
        if self._sibs is None:
            self._sibs = [mm for mm in self.consumer.strategies.values()
                          if isinstance(mm, SingleMM)
                          and mm.event_ticker == self.event_ticker
                          and mm.ticker != self.ticker]
        return self._sibs

    def _leg_tob(self, ticker: str):
        return self.consumer.replayer.top(ticker)

    def _pair_exposure(self) -> float:
        return self.inventory[self.ticker]

    # Reuse PairMM quoting/booking logic with per-market settings
    _desired_sides = PairMM._desired_sides
    _exit_quotes = PairMM._exit_quotes
    _liquidate_quotes = PairMM._liquidate_quotes
    on_fill = PairMM.on_fill

    def _aggro_entry(self, lts: float, alpha: float):
        """Latched aggressive entry: take the touch toward +/-aggro_limit when
        the alpha crosses +/-aggro_entry; the latch re-arms on zero-cross.
        Exits are handled by the passive reduce-only quotes."""
        p = self.params
        inv = self.inventory[self.ticker]
        if is_zero(inv):
            self._entry_px = None
        elif self._entry_px is not None:
            # Stop loss: price moved aggro_stop against entry -> take the touch
            tob = self._leg_tob(self.ticker)
            stop_long = (is_pos(inv) and tob.yes_bid is not None
                         and tob.yes_bid <= self._entry_px - p.aggro_stop + 1e-9)
            stop_short = (is_neg(inv) and tob.yes_ask is not None
                          and tob.yes_ask >= self._entry_px + p.aggro_stop - 1e-9)
            if (stop_long or stop_short) and self.consumer.rate_limiter.try_acquire(lts):
                if stop_long:
                    qty = min(inv, tob.yes_bid_qty or 0.0)
                    if qty > 0:
                        self.consumer.pnl.trade(self.ticker, "short", qty, tob.yes_bid, is_maker = False)
                        self.inventory[self.ticker] -= qty
                        self.consumer.log_order(lts, self.event_ticker, self.ticker, "no",
                                                "taker_stop", tob.yes_bid, qty, alpha, None,
                                                self.inventory[self.ticker])
                else:
                    qty = min(-inv, tob.yes_ask_qty or 0.0)
                    if qty > 0:
                        self.consumer.pnl.trade(self.ticker, "long", qty, tob.yes_ask, is_maker = False)
                        self.inventory[self.ticker] += qty
                        self.consumer.log_order(lts, self.event_ticker, self.ticker, "yes",
                                                "taker_stop", tob.yes_ask, qty, alpha, None,
                                                self.inventory[self.ticker])
                self._armed = False
                if is_zero(self.inventory[self.ticker]):
                    self._entry_px = None
                return
        if getattr(p, "aggro_cross", False):
            sibs = self._siblings()
            if not sibs:
                return
            vals = [s.alpha_engine.value_of(p.alpha_name, lts) for s in sibs]
            vals = [0.0 if v is None else v for v in vals]
            if getattr(p, "aggro_neg", None):
                # 3-leg spike trade: own alpha > t_pos (skipped when t_pos is
                # None) AND every sibling < -t_neg -> buy YES here and buy NO
                # on both siblings, all aggressively. Single one-sided condition.
                t_pos = p.aggro_entry
                if not all(v <= -p.aggro_neg for v in vals):
                    return
                if t_pos is not None and alpha < t_pos:
                    return
                self._take(lts, 1, alpha)
                for s in sibs:
                    s._take(lts, -1, s.alpha_engine.value_of(p.alpha_name, lts) or 0.0)
                return
            # Symmetric cross-leg condition: own tfma_pw beyond +/-t AND every
            # other leg beyond the opposite threshold. No latch — entries may
            # fire again even mid-liquidation while the condition holds.
            if alpha >= p.aggro_entry and all(v <= -p.aggro_entry for v in vals):
                self._latch = 1
            elif alpha <= -p.aggro_entry and all(v >= p.aggro_entry for v in vals):
                self._latch = -1
            else:
                return
        else:
            if self._latch == 0:
                if alpha >= p.aggro_entry:
                    self._latch = 1
                elif alpha <= -p.aggro_entry:
                    self._latch = -1
                if self._latch != 0:
                    self._armed = True
                    self._peak = abs(inv)
            elif self._latch == 1 and alpha <= 0:
                self._latch = 0
                self._armed = False
            elif self._latch == -1 and alpha >= 0:
                self._latch = 0
                self._armed = False
            # Once a passive exit reduces the position below its peak, entries stay
            # disarmed until the alpha zero-crosses and re-triggers the latch
            if abs(inv) < self._peak - 1e-9:
                self._armed = False
            if self._latch == 0 or not self._armed:
                return
        if (self._latch > 0 and inv >= p.aggro_limit) or (self._latch < 0 and -inv >= p.aggro_limit):
            return
        self._take(lts, self._latch, alpha)

    def _take(self, lts: float, direction: int, alpha: float):
        """Aggressive taker entry toward +/-aggro_limit in YES space."""
        p = self.params
        inv = self.inventory[self.ticker]
        tob = self._leg_tob(self.ticker)
        if tob.yes_bid is None or tob.yes_ask is None or tob.spread is None:
            return
        if tob.spread < 0.005 or tob.yes_bid < p.price_min or tob.yes_ask > p.price_max:
            return
        budget = getattr(p, "budget", None)
        if not self.consumer.rate_limiter.try_acquire(lts):
            return
        if direction > 0:
            qty = min(p.aggro_limit - inv, tob.yes_ask_qty or 0.0)
            price = tob.yes_ask
            cost = qty * price
            if lte(qty, 0) or (budget and self.consumer._deployed_dollars() + cost > budget):
                return
            self.consumer.pnl.trade(self.ticker, "long", qty, price, is_maker = False)
            self.inventory[self.ticker] += qty
        else:
            qty = min(p.aggro_limit + inv, tob.yes_bid_qty or 0.0)
            price = tob.yes_bid
            cost = qty * (1.0 - price)
            if lte(qty, 0) or (budget and self.consumer._deployed_dollars() + cost > budget):
                return
            self.consumer.pnl.trade(self.ticker, "short", qty, price, is_maker = False)
            self.inventory[self.ticker] -= qty
        prev = abs(inv)
        if self._entry_px is None or is_zero(prev):
            self._entry_px = price
        else:
            self._entry_px = (self._entry_px * prev + price * qty) / (prev + qty)
        self._peak = abs(self.inventory[self.ticker])
        self.consumer.log_order(lts, self.event_ticker, self.ticker,
                                "yes" if direction > 0 else "no", "taker_entry",
                                price, qty, alpha, None, self.inventory[self.ticker])

    def requote(self, lts: float):
        alpha = self.alpha_engine.value_of(self.params.alpha_name, lts)
        if alpha is None:
            alpha = 0.0
        if getattr(self.params, "aggro_entry", None) or getattr(self.params, "aggro_neg", None):
            self._aggro_entry(lts, alpha)
        desired = self._desired_sides(self.ticker, alpha, lts)
        self._apply_desired(lts, desired, alpha)

    def _apply_desired(self, lts: float, desired: dict, alpha: float):
        """Reconcile resting orders against a desired {side: [(price, size)]}."""
        fill_engine = self.consumer.fill_engine
        max_queue = getattr(self.params, "max_queue_ahead", None)
        limiter = self.consumer.rate_limiter
        expo = self.inventory[self.ticker]
        for side in ("yes", "no"):
            key = (self.ticker, side)
            want_by_key = {price_key(p): s for p, s in desired.get(side, [])}
            kept = []
            for oid in self.resting.get(key, []):
                order = fill_engine.orders.get(oid)
                if order is None:
                    continue
                supported = is_pos(fill_engine.displayed(self.ticker, side, order.price))
                if supported and order.price in want_by_key:
                    kept.append(oid)
                    del want_by_key[order.price]
                elif limiter.try_acquire(lts):
                    fill_engine.cancel(oid)
                    self.consumer.log_order(lts, self.event_ticker, self.ticker, side, "cancel",
                                            order.price_f, order.remaining, alpha,
                                            order.queue_ahead, expo)
                else:
                    kept.append(oid)
                    self.consumer.log_order(lts, self.event_ticker, self.ticker, side, "rate_limited",
                                            order.price_f, order.remaining, alpha,
                                            order.queue_ahead, expo)
            for pk, size in want_by_key.items():
                if lte(fill_engine.displayed(self.ticker, side, pk), 0):
                    continue                            # don't create a non-existent level
                if max_queue is not None and fill_engine.displayed(self.ticker, side, pk) > max_queue:
                    continue
                if not limiter.try_acquire(lts):
                    continue
                oid = fill_engine.place(lts, self.ticker, side, float(pk), size)
                kept.append(oid)
                self.consumer.log_order(lts, self.event_ticker, self.ticker, side, "place",
                                        float(pk), size, alpha,
                                        fill_engine.orders[oid].queue_ahead, expo)
            if kept:
                self.resting[key] = kept
            else:
                self.resting.pop(key, None)


class WCStrategy(SingleMM):
    """Phase-aware WC (soccer) strategy on the exact game clock:
      - no trading before KO+5min, and during half-time;
      - main phase (5'->85'): alpha-skew MM, square_off=False (ride signal-
        aligned inventory, only add on the alpha-favored side within limits);
      - liquidation phase (85'->end): reduce-only, and only when the alpha
        supports the exit direction; otherwise hold (resolve at settlement).
    Falls back to plain SingleMM behaviour if no game clock is available."""

    LIQUIDATE_MIN = 85

    def __init__(self, event_ticker, ticker, consumer, params):
        super().__init__(event_ticker, ticker, consumer, params)
        from espn_clock import clocks_for
        self.clock = clocks_for(event_ticker)

    def _phase(self, lts: float) -> str:
        c = self.clock
        if not c or "ko" not in c:
            return "main"
        if lts < c["ko"] + 300:                       # before KO + 5min
            return "notrade"
        if "ht" in c and "sh" in c and c["ht"] <= lts < c["sh"]:
            return "notrade"                          # half-time
        sh = c.get("sh", c["ko"] + 3600)
        if lts >= sh + (self.LIQUIDATE_MIN - 45) * 60:  # 85' = 2H restart + 40min
            return "liquidate"
        return "main"

    def _cancel_all(self, lts: float):
        fe = self.consumer.fill_engine
        lim = self.consumer.rate_limiter
        for side in ("yes", "no"):
            key = (self.ticker, side)
            kept = []
            for oid in self.resting.get(key, []):
                order = fe.orders.get(oid)
                if order is None:
                    continue
                if lim.try_acquire(lts):
                    fe.cancel(oid)
                    self.consumer.log_order(lts, self.event_ticker, self.ticker, side, "cancel",
                                            order.price_f, order.remaining, None,
                                            order.queue_ahead, self.inventory[self.ticker])
                else:
                    kept.append(oid)
            if kept:
                self.resting[key] = kept
            else:
                self.resting.pop(key, None)

    def requote(self, lts: float):
        phase = self._phase(lts)
        if phase == "notrade":
            self._cancel_all(lts)
            return
        if phase == "main":
            super().requote(lts)
            return
        # liquidation: reduce-only, alpha-gated
        alpha = self.alpha_engine.value_of(self.params.alpha_name, lts)
        alpha = 0.0 if alpha is None else alpha
        inv = self.inventory[self.ticker]
        support = (is_pos(inv) and alpha <= 0) or (is_neg(inv) and alpha >= 0)
        if not support:
            self._cancel_all(lts)
            return
        self._apply_desired(lts, self._exit_quotes(self.ticker), alpha)


class MMSimConsumer:
    def __init__(self, replayer: Replayer, params):
        self.replayer = replayer
        self.params = params
        self.fill_engine = PassiveFillEngine(
            replayer.books,
            forward_delay = getattr(params, "forward_delay", FORWARD_DELAY_S),
        )
        self.pnl = PnL(charge_fees = True, fee_model = "kalshi")
        self.strategies: dict[str, PairMM] = {}
        self.mm_by_ticker: dict[str, PairMM] = {}
        # Per-leg mid history (lts, mid) for markouts
        self.mid_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.fill_rows: list[dict] = []
        self.last_mid: dict[str, float] = {}
        self.peak_deployed: float = 0.0
        self.rate_limiter = WriteRateLimiter(getattr(params, "write_rate", 10))
        # Periodic per-strategy state snapshots for visualization/refit
        self.state_rows: list[dict] = []
        self._last_state_log: dict[str, float] = {}
        # Order decisions happen only on trades or top-of-book moves
        self._last_quote: dict[str, tuple] = {}
        # Generic decision log: one row per place/cancel/fill/skip with the
        # context that explains it (works for any strategy; drives viz)
        self.order_rows: list[dict] = []

    def log_order(self, lts: float, event: str, ticker: str, side: str, action: str,
                  price, size, alpha, queue_ahead = None, exposure = None):
        tob = self.replayer.top(ticker)
        self.order_rows.append({
            "lts": round(lts, 3),
            "event": event,
            "ticker": ticker,
            "side": side,
            "action": action,           # place | cancel | fill | rate_limited
            "price": price,
            "size": size,
            "queue_ahead": round(queue_ahead, 1) if queue_ahead is not None else "",
            "alpha": round(alpha, 6) if alpha is not None else "",
            "mid": round(tob.mid, 4) if tob.mid is not None else "",
            "spread": tob.spread if tob.spread is not None else "",
            "exposure": round(exposure, 1) if exposure is not None else "",
            "deployed": round(self._deployed_dollars(), 2),
        })

    def dump_orders(self, out_dir):
        if not self.order_rows:
            return
        with open(Path(out_dir) / "orders.csv", "w", newline = "") as f:
            w = csv.DictWriter(f, fieldnames = list(self.order_rows[0].keys()))
            w.writeheader()
            w.writerows(self.order_rows)

    def on_meta(self, lts: float, meta: dict):
        series_filter = getattr(self.params, "series", None)
        series_set = None
        if series_filter:
            series_set = {s.strip() for s in series_filter.split(",") if s.strip()}

        for pair in meta.get("pairs", []):
            if series_set is not None and pair["series"] not in series_set:
                continue
            event_ticker = pair["event_ticker"]
            # Seed series fee info from the recording so no API access is needed
            series = pair["series"]
            self.pnl.series_fees[series] = (pair["fee_multiplier"], pair["fee_type"])
            for tkr in (pair["first_ticker"], pair["second_ticker"]):
                self.pnl.market_to_series[tkr] = series
            if event_ticker in self.strategies:
                continue
            mm = PairMM(pair, self, self.params)
            self.strategies[event_ticker] = mm
            self.mm_by_ticker[pair["first_ticker"]] = mm
            self.mm_by_ticker[pair["second_ticker"]] = mm

        # N-outcome events (soccer): one independent SingleMM per market
        for ev in meta.get("events", []):
            if series_set is not None and ev["series"] not in series_set:
                continue
            self.pnl.series_fees[ev["series"]] = (ev["fee_multiplier"], ev["fee_type"])
            for tkr in ev["tickers"]:
                self.pnl.market_to_series[tkr] = ev["series"]
                key = f"{ev['event_ticker']}:{tkr}"
                if key in self.strategies:
                    continue
                cls = (WCStrategy if getattr(self.params, "football", False)
                       and ev["series"] in ("KXWCGAME", "KXINTLFRIENDLYGAME")
                       else SingleMM)
                mm = cls(ev["event_ticker"], tkr, self, self.params)
                self.strategies[key] = mm
                self.mm_by_ticker[tkr] = mm

    def _record_mid(self, lts: float, ticker: str):
        tob = self.replayer.top(ticker)
        mid = tob.mid
        if mid is None or self.last_mid.get(ticker) == mid:
            return
        self.last_mid[ticker] = mid
        self.mid_history[ticker].append((lts, mid))

    def _deployed_dollars(self) -> float:
        """Cash tied up in open positions at entry prices (NO cost = 1 - yes price)."""
        total = 0.0
        for pos in self.pnl.positions.values():
            cost = pos.avg_price if pos.side == "long" else 1.0 - pos.avg_price
            total += pos.qty * cost
        return total

    def _process_fills(self, fills):
        for fill in fills:
            mm = self.mm_by_ticker.get(fill.order.ticker)
            if mm is not None:
                mm.on_fill(fill)
        if fills:
            self.peak_deployed = max(self.peak_deployed, self._deployed_dollars())

    def _maybe_log_state(self, lts: float, mm):
        """Throttled per-strategy snapshot: odds, alpha, position, PnL components."""
        key = mm.pair["event_ticker"] if mm.second_ticker is not None else f"{mm.event_ticker}:{mm.ticker}"
        last = self._last_state_log.get(key)
        if last is not None and lts - last < STATE_LOG_INTERVAL_S:
            return
        self._last_state_log[key] = lts
        if mm.second_ticker is not None:
            mid = mm.alpha_engine.pair_mid()
            open_qty = abs(mm.inventory[mm.first_ticker]) + abs(mm.inventory[mm.second_ticker])
        else:
            mid = mm.alpha_engine._mid()
            open_qty = abs(mm.inventory[mm.ticker])
        alpha = mm.alpha_engine.value_of(self.params.alpha_name, lts)
        tickers = ([mm.first_ticker, mm.second_ticker]
                   if mm.second_ticker is not None else [mm.ticker])
        real_ev = sum(self.pnl.realized_by_ticker.get(t, 0.0) for t in tickers)
        fees_ev = sum(self.pnl.fees_by_ticker.get(t, 0.0) for t in tickers)
        self.state_rows.append({
            "lts": round(lts, 3),
            "event": key,
            "mid": round(mid, 4) if mid is not None else "",
            "alpha": round(alpha, 6) if alpha is not None else "",
            "exposure": round(mm._pair_exposure(), 1),
            "open_contracts": round(open_qty, 1),
            "deployed_total": round(self._deployed_dollars(), 2),
            "realized_total": round(self.pnl.realized_pnl, 4),
            "fees_total": round(self.pnl.fees_paid, 4),
            "realized_event": round(real_ev, 4),
            "fees_event": round(fees_ev, 4),
        })

    def dump_state(self, out_dir: Path):
        if not self.state_rows:
            return
        with open(Path(out_dir) / "state.csv", "w", newline = "") as f:
            w = csv.DictWriter(f, fieldnames = list(self.state_rows[0].keys()))
            w.writeheader()
            w.writerows(self.state_rows)

    def on_book(self, lts: float, ticker: str, delta_msg):
        mm = self.mm_by_ticker.get(ticker)
        if mm is None:
            return
        if delta_msg is None:
            fills = self.fill_engine.on_snapshot(lts, ticker)
        else:
            self.fill_engine.record_delta(
                lts, ticker, delta_msg["side"], delta_msg["price_dollars"], float(delta_msg["delta_fp"])
            )
            # Aggregation alpha consumes every delta (no-op unless tracked)
            mm.alpha_engine.on_delta(lts, ticker, delta_msg)
            fills = self.fill_engine.on_book(lts, ticker)
        # Order decisions only when the quote moved (or we got filled);
        # deep-book deltas update the fill engine but trigger no requote --
        # EXCEPT a reduction that empties a level where we rest, so the strategy
        # can pull an order left at an unsupported (no-real-backing) level.
        tob = self.replayer.top(ticker)
        quote = (tob.yes_bid, tob.yes_ask)
        moved = quote != self._last_quote.get(ticker)
        self._last_quote[ticker] = quote
        rest_emptied = False
        if delta_msg is not None and float(delta_msg["delta_fp"]) < 0:
            dside, dpk = delta_msg["side"], delta_msg["price_dollars"]
            if lte(self.fill_engine.displayed(ticker, dside, dpk), 0):
                for oid in mm.resting.get((ticker, dside), []):
                    o = self.fill_engine.orders.get(oid)
                    if o is not None and o.price == dpk:
                        rest_emptied = True
                        break
        if not (moved or fills or rest_emptied):
            return
        self._record_mid(lts, ticker)
        mm.alpha_engine.on_book(lts, ticker)
        self._process_fills(fills)
        mm.requote(lts)
        self._maybe_log_state(lts, mm)

    def on_trade(self, lts: float, msg: dict):
        ticker = msg["market_ticker"]
        mm = self.mm_by_ticker.get(ticker)
        if mm is None:
            return
        mm.alpha_engine.on_trade(lts, msg)
        fills = self.fill_engine.on_trade(lts, msg)
        self._record_mid(lts, ticker)
        self._process_fills(fills)
        mm.requote(lts)
        self._maybe_log_state(lts, mm)

    def log_fill(self, fill, event_ticker, yes_space_price, inventory_after, alpha, mid, spread):
        self.fill_rows.append({
            "lts": fill.lts,
            "event_ticker": event_ticker,
            "ticker": fill.order.ticker,
            "side": fill.order.side,
            "price": fill.order.price_f,
            "yes_space_price": yes_space_price,
            "qty": fill.qty,
            "reason": fill.reason,
            "inventory_after": inventory_after,
            "alpha": alpha if alpha is not None else "",
            "mid": mid if mid is not None else "",
            "spread": spread if spread is not None else "",
            "realized_pnl": self.pnl.realized_pnl,
            "fees_paid": self.pnl.fees_paid,
        })


def compute_markouts(consumer):
    """Per-fill mid markouts (cents, signed so positive = good for us)."""
    hist_arrays = {}
    for ticker, hist in consumer.mid_history.items():
        hist_arrays[ticker] = (np.array([h[0] for h in hist]), np.array([h[1] for h in hist]))

    for row in consumer.fill_rows:
        ticker = row["ticker"]
        direction = 1.0 if row["side"] == "yes" else -1.0
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
    parser = argparse.ArgumentParser(description = "Passive MM simulator on a tick recording")
    parser.add_argument("recording", help = "Path to ticks_*.jsonl.gz")
    parser.add_argument("-s", "--per-order-size", type = float, default = 10)
    parser.add_argument("-i", "--inventory-cap", type = float, default = 30)
    parser.add_argument("-t", "--skew-threshold", type = float, default = 25)
    parser.add_argument("-a", "--alpha-name", type = str, default = "tfma_pw_10s")
    parser.add_argument("--max-spread", type = float, default = 0.01,
                        help = "Only quote when spread <= this (default 0.01 = 1 tick)")
    parser.add_argument("--price-min", type = float, default = 0.05)
    parser.add_argument("--price-max", type = float, default = 0.95)
    parser.add_argument("--improve", action = "store_true",
                        help = "Step one side 1 tick inside 2+ tick spreads (queue priority)")
    parser.add_argument("--pair-risk", action = "store_true",
                        help = "Cap/reduce net pair exposure instead of per-ticker inventory")
    parser.add_argument("--square-off", action = "store_true",
                        help = "MM stance: always quote the reducing side to mean-revert inventory to flat (off = only quote alpha-favored side within position limits)")
    parser.add_argument("--series", type = str, default = None,
                        help = "Comma-separated series filter (e.g. KXMLBGAME,KXNBAGAME)")
    parser.add_argument("--forward-delay", type = float, default = FORWARD_DELAY_S,
                        help = "One-way order-entry latency (s); fills require placement + delay <= trade ts")
    parser.add_argument("--size-ref", type = float, default = None,
                        help = "Alpha magnitude at which order size reaches full S (alpha-proportional sizing)")
    parser.add_argument("--max-queue-ahead", type = float, default = None,
                        help = "Skip fresh joins behind more than this many displayed contracts")
    parser.add_argument("--depth-quote", action = "store_true",
                        help = "Rest adding-side quotes 1 tick below best (sweep capture)")
    parser.add_argument("--ladder", action = "store_true",
                        help = "Split adding-side size across best and best-1 tick")
    parser.add_argument("--per-leg-alpha", action = "store_true",
                        help = "Quote each leg from its own book OBI (mom-gated) instead of the pair average")
    parser.add_argument("--budget", type = float, default = 1000,
                        help = "Global deployed-dollars cap; adding quotes suppressed above it")
    parser.add_argument("--resolve", action = "store_true",
                        help = "Settle leftover positions at actual market results via REST (airtight PnL)")
    parser.add_argument("--write-rate", type = float, default = 10,
                        help = "Max order writes/sec (Kalshi basic tier = 10)")
    parser.add_argument("--combo-file", type = str, default = None,
                        help = "combo weights JSON from fit_combo.py (use with -a combo)")
    parser.add_argument("--aggro-entry", type = float, default = None,
                        help = "Hybrid mode: latched aggressive entry threshold on alpha; passive quotes become exit-only")
    parser.add_argument("--aggro-limit", type = float, default = 300,
                        help = "Max position taken per aggressive entry (hybrid mode)")
    parser.add_argument("--aggro-profit", type = float, default = 0.02,
                        help = "Hybrid mode: passive exit never priced worse than entry +/- this (covers fees)")
    parser.add_argument("--aggro-stop", type = float, default = 0.05,
                        help = "Hybrid mode: aggressive stop-loss when price moves this far against entry")
    parser.add_argument("--aggro-cross", action = "store_true",
                        help = "Entry requires own alpha beyond +/-t AND all sibling legs beyond the opposite threshold")
    parser.add_argument("--aggro-neg", type = float, default = None,
                        help = "3-leg spike trade: siblings < -t_neg (own > t_pos = --aggro-entry, optional) -> buy YES here + NO on siblings")
    parser.add_argument("--football", action = "store_true",
                        help = "WC/soccer phase strategy: no-trade pre-5'/half-time, MM 5'->85', liquidate-only 85'->end (uses ESPN game clocks)")
    parser.add_argument("--tag", type = str, default = None, help = "Output dir name")
    args = parser.parse_args()

    args.combo = None
    if args.combo_file:
        import json
        with open(args.combo_file) as f:
            args.combo = json.load(f)

    if args.tag is None:
        args.tag = (
            f"mm_{Path(args.recording).name.split('.')[0]}"
            f"_s{args.per_order_size:g}_i{args.inventory_cap:g}"
            f"_t{args.skew_threshold:g}_{args.alpha_name}"
            f"{'_imp' if args.improve else ''}"
            f"{'_pr' if args.pair_risk else ''}"
            f"{'_' + args.series if args.series else ''}"
        )
    out_dir = OUTPUT_BASE / args.tag
    out_dir.mkdir(parents = True, exist_ok = True)

    replayer = Replayer(args.recording)
    consumer = MMSimConsumer(replayer, args)
    print(f"Replaying {args.recording}...")
    n = replayer.run(consumer)
    print(f"  {n} messages, {len(consumer.strategies)} pairs, {len(consumer.fill_rows)} fills")

    compute_markouts(consumer)

    fill_fields = list(consumer.fill_rows[0].keys()) if consumer.fill_rows else []
    with open(out_dir / "fills.csv", "w", newline = "") as f:
        w = csv.DictWriter(f, fieldnames = fill_fields)
        w.writeheader()
        w.writerows(consumer.fill_rows)
    consumer.dump_state(out_dir)
    consumer.dump_orders(out_dir)

    # Optional: settle remaining positions at the actual market results
    # (recorded markets are typically resolved by replay time)
    if args.resolve:
        from src.utils.api import fetch_market_result
        for ticker in list(consumer.pnl.positions.keys()):
            try:
                result = fetch_market_result(ticker)
            except Exception as e:
                print(f"  resolve lookup failed for {ticker}: {e}")
                continue
            if result == "yes":
                consumer.pnl.resolve(ticker, 1.0)
            elif result == "no":
                consumer.pnl.resolve(ticker, 0.0)

    # Aggregate metrics
    pnl = consumer.pnl
    last_mids = {t: h[-1][1] for t, h in consumer.mid_history.items() if h}
    gross = pnl.realized_pnl
    fees = pnl.fees_paid
    unrealized = pnl.mark_to_market(last_mids)
    net = pnl.net_total_pnl(prices = last_mids)
    contracts = sum(r["qty"] for r in consumer.fill_rows)
    open_qty = sum(abs(p.qty) for p in pnl.positions.values())

    markout_means = {}
    for h in MARKOUT_HORIZONS_S:
        vals = [(r[f"markout_{h}s"], r["qty"]) for r in consumer.fill_rows if r[f"markout_{h}s"] != ""]
        if vals:
            tot_q = sum(q for _, q in vals)
            markout_means[h] = sum(m * q for m, q in vals) / tot_q
        else:
            markout_means[h] = float("nan")

    summary = {
        "recording": Path(args.recording).name,
        "per_order_size": args.per_order_size,
        "inventory_cap": args.inventory_cap,
        "skew_threshold": args.skew_threshold,
        "alpha_name": args.alpha_name,
        "max_spread": args.max_spread,
        "improve": int(args.improve),
        "pair_risk": int(args.pair_risk),
        "n_fills": len(consumer.fill_rows),
        "contracts": contracts,
        "realized_pnl": round(gross, 4),
        "fees_paid": round(fees, 4),
        "unrealized_pnl": round(unrealized, 4),
        "net_pnl": round(net, 4),
        "open_contracts_end": round(open_qty, 2),
        "net_pair_exposure_end": round(
            sum(abs(mm._pair_exposure()) for mm in consumer.strategies.values()), 2
        ),
        "peak_deployed_dollars": round(consumer.peak_deployed, 2),
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
    print(f"\n{'=' * 64}\nPASSIVE MM SIM SUMMARY  (tag {args.tag})")
    for k, v in summary.items():
        print(f"  {k:<24} {v}")
    print("=" * 64)


if __name__ == "__main__":
    main()

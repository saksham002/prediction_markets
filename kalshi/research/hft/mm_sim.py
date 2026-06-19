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
import logging
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import PairAlphaEngine, SingleAlphaEngine
from research.hft.passive_fill import FORWARD_DELAY_S, PassiveFillEngine
from research.hft.order_router import OrderRouter
from research.hft.exchange import SimExchange
from src.utils.feps import is_pos, is_neg, is_zero, lte
from research.hft.replay import Replayer
from src.pnl import PnL

logger = logging.getLogger(__name__)

OUTPUT_BASE = Path("/data/user_data/saksham3/kalshi_hft/sims")
MARKOUT_HORIZONS_S = [5, 30, 60, 300]
STATE_LOG_INTERVAL_S = 5.0


# Kalshi write rate limit as a TOKEN BUDGET (verified live 2026-06-17 on ENGCRO):
# a place (order write) costs PLACE_TOKENS, a cancel (single == batch) CANCEL_TOKENS,
# budget WRITE_BUDGET tokens per rolling second. (NB: the real exchange throttles
# simultaneous bursts harder than the sustained 100/s — only ~5 writes clear in a
# tight burst — but per-event quoting never bursts, so the sustained budget is the
# right model here.)
PLACE_TOKENS = 10
CANCEL_TOKENS = 2
WRITE_BUDGET = 100


class WriteRateLimiter:
    """Token-budget write limiter: WRITE_BUDGET tokens per rolling 1s; a place
    costs place_cost, a cancel cancel_cost. Actions whose cost would exceed the
    budget are skipped and retried on a later event — the same backpressure a
    live trader faces."""

    def __init__(self, budget: float = WRITE_BUDGET, place_cost: float = PLACE_TOKENS,
                 cancel_cost: float = CANCEL_TOKENS):
        self.budget = budget
        self.place_cost = place_cost
        self.cancel_cost = cancel_cost
        self.events: deque = deque()        # (ts, cost) within the last 1s
        self._spent = 0.0

    def _try(self, now: float, cost: float) -> bool:
        while self.events and now - self.events[0][0] >= 1.0:
            self._spent -= self.events.popleft()[1]
        if self._spent + cost > self.budget:
            return False
        self.events.append((now, cost))
        self._spent += cost
        return True

    def try_place(self, now: float) -> bool:
        return self._try(now, self.place_cost)

    def try_cancel(self, now: float) -> bool:
        return self._try(now, self.cancel_cost)


class PairMM:
    def __init__(self, pair, consumer, params):
        self.pair = pair
        self.consumer = consumer
        self.params = params
        self.first_ticker = pair["first_ticker"]
        self.second_ticker = pair["second_ticker"]
        # share the consumer's MarketView so obi/mid are market-only live
        self.alpha_engine = PairAlphaEngine(
            pair, consumer.view, combo = getattr(params, "combo", None),
            track_agg = "agg" in params.alpha_name,
        )
        self.inventory: dict[str, float] = {self.first_ticker: 0.0, self.second_ticker: 0.0}

    def _leg_tob(self, ticker: str):
        return self.consumer.view.top(ticker)

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

    def _budget_clip(self, size: float, price: float) -> float:
        """Clip an ENTRY (cash-consuming) order to what the remaining budget can
        buy at this price: min(size, (budget - deployed) // price) whole contracts.
        No-op when no budget set. Reduce/netting orders free cash and are NOT clipped."""
        budget = getattr(self.params, "budget", None)
        if budget is None or not is_pos(price):
            return size
        affordable = max(0.0, budget - self.consumer._deployed_dollars()) // price
        return min(size, affordable)

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

        # Budget is enforced per-order by _budget_clip below (entry size clipped to
        # remaining // price), so there is no separate over-budget guard.

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

        # One quote per side at the joined best price (<=1 resting/side, enforced
        # by OrderRouter). The reducing side always joins best for the fastest
        # passive square-off.
        def shape(side: str, price: float | None, sz: float) -> list[tuple[float, float]]:
            return [] if price is None else [(price, sz)]

        # square_off: the market-maker stance that always keeps the reducing
        # side quoted so inventory mean-reverts to flat. When off, only the
        # alpha-favored side is quoted (within position limits) and signal-
        # aligned inventory is allowed to ride to the cap. A pure-reduce order
        # is capped to |inv| so a fill flattens the position, never flips it.
        square_off = getattr(p, "square_off", False)
        cap = p.inventory_cap
        desired = {}
        # add side: clip the order to BOTH the remaining budget and the remaining
        # position-limit room (cap - inv for yes, inv + cap for no) so we top up to
        # the cap instead of refusing a full-size order that would overshoot it.
        add_yes = leg_alpha > -p.skew_threshold and inv < cap
        reduce_yes = square_off and is_neg(inv)
        if yes_price is not None and (add_yes or reduce_yes):
            sz = min(self._budget_clip(size, yes_price), cap - inv) if add_yes else min(size, -inv)
            if is_pos(sz):
                desired["yes"] = shape("yes", yes_price, sz)
        add_no = leg_alpha < p.skew_threshold and inv > -cap
        reduce_no = square_off and is_pos(inv)
        if no_price is not None and (add_no or reduce_no):
            sz = min(self._budget_clip(size, no_price), inv + cap) if add_no else min(size, inv)
            if is_pos(sz):
                desired["no"] = shape("no", no_price, sz)
        return desired

    def _leg_alpha_per_leg(self, lts: float, ticker: str, leg_sign: float) -> float:
        # Per-leg mode: the leg's OWN book imbalance (no pair averaging),
        # gated by sign agreement with the leg-signed pair momentum
        obi_leg = self.consumer.view.obi(ticker)
        mom = self.alpha_engine.value_of("mom_5s", lts)
        if obi_leg is None or mom is None:
            return 0.0
        if obi_leg * (mom * leg_sign) > 0:
            return obi_leg
        return 0.0

    def _reconcile_side(self, lts: float, ticker: str, side: str,
                        want, alpha, expo, event):
        """Declare the desired resting order for (ticker, side) — `want` is (price,size)
        or None — and let the OrderRouter drive the exchange toward it. The strategy is
        UNAWARE of in-flight / pending-ack / rate-limit state: the router owns the
        in-flight lock, the write-budget gating, the passive-level invariants, and the
        order logging (see order_router.reconcile)."""
        self.consumer.router.set_target(lts, ticker, side, want,
                                        {"event": event, "alpha": alpha, "expo": expo})

    def requote(self, lts: float):
        per_leg = getattr(self.params, "per_leg_alpha", False)
        if not per_leg:
            # Single lazy alpha computation per event, mirrored across legs
            if self.consumer._timing:
                self.consumer._evt["alpha_start"] = time.time()      # #3
            base = self.alpha_engine.value_of(self.params.alpha_name, lts)
            base = 0.0 if base is None else base
            if self.consumer._timing:
                self.consumer._evt["alpha"] = base
        event = self.pair["event_ticker"]
        expo = self._pair_exposure()
        for ticker, leg_sign in ((self.first_ticker, 1.0), (self.second_ticker, -1.0)):
            leg_alpha = (self._leg_alpha_per_leg(lts, ticker, leg_sign)
                         if per_leg else base * leg_sign)
            desired = self._desired_sides(ticker, leg_alpha, lts)
            for side in ("yes", "no"):
                want = (desired.get(side) or [None])[0]
                self._reconcile_side(lts, ticker, side, want, leg_alpha, expo, event)

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
        # Resting-order cleanup (drop/re-register the reduced order) is handled by
        # OrderRouter.on_fill from MMSimConsumer._process_fills.
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
        # share the consumer's MarketView so obi/mid are market-only live
        self.alpha_engine = SingleAlphaEngine(
            ticker, consumer.view, combo = getattr(params, "combo", None),
            track_agg = "agg" in params.alpha_name,
            track_obi_ma = ("obi_ma" in params.alpha_name or "obi_dev" in params.alpha_name))
        self.inventory: dict[str, float] = {ticker: 0.0}
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
        return self.consumer.view.top(ticker)

    def _pair_exposure(self) -> float:
        return self.inventory[self.ticker]

    # Reuse PairMM quoting/booking logic with per-market settings
    _desired_sides = PairMM._desired_sides
    _budget_clip = PairMM._budget_clip
    _exit_quotes = PairMM._exit_quotes
    _liquidate_quotes = PairMM._liquidate_quotes
    _reconcile_side = PairMM._reconcile_side
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
            if (stop_long or stop_short) and self.consumer.rate_limiter.try_place(lts):
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
        if not self.consumer.rate_limiter.try_place(lts):
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
        if self.consumer._timing:
            self.consumer._evt["alpha_start"] = time.time()      # #3 alpha computation start
        alpha = self.alpha_engine.value_of(self.params.alpha_name, lts)
        if alpha is None:
            alpha = 0.0
        if self.consumer._timing:
            self.consumer._evt["alpha"] = alpha
        if getattr(self.params, "aggro_entry", None) or getattr(self.params, "aggro_neg", None):
            self._aggro_entry(lts, alpha)
        desired = self._desired_sides(self.ticker, alpha, lts)
        self._apply_desired(lts, desired, alpha)

    def _apply_desired(self, lts: float, desired: dict, alpha: float):
        """Reconcile resting orders against a desired {side: [(price, size)]}."""
        expo = self.inventory[self.ticker]
        for side in ("yes", "no"):
            want = (desired.get(side) or [None])[0]
            self._reconcile_side(lts, self.ticker, side, want, alpha, expo, self.event_ticker)


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
        self.clock = clocks_for(event_ticker)        # sim: static file cache (asserted below)
        # live-data runs (paper/live): MAIN polls ESPN every 10s into
        # consumer.live_clocks (in-memory); _phase reads from there so KO/HT/SH/FT/
        # goals appear as the game progresses. Replay/sweep reads the static clock.
        self._live_clock = getattr(params, "live_clock", False)
        # live testing before kickoff: skip phase-gating entirely (always "main")
        self._ignore_clock = getattr(params, "ignore_clock", False)
        # SIM (replay/sweep) MUST have a real game clock — otherwise WCStrategy
        # silently runs the WHOLE game in "main" phase (no no-trade/half-time/85'
        # liquidate gating), i.e. trades outside the right bounds. Fail LOUDLY here
        # instead. (Skipped for live and with --ignore-clock.)
        if not self._live_clock and not self._ignore_clock:
            assert self.clock and "ko" in self.clock, (
                f"WCStrategy: no game clock for {event_ticker}. Run espn_clock.py to "
                f"populate wc_clocks.json — refusing to sim ungated (see project_status).")

    def _phase(self, lts: float) -> str:
        if self._ignore_clock:
            return "main"                              # live test: trade regardless of clock
        c = (getattr(self.consumer, "live_clocks", {}).get(self.event_ticker)
             if self._live_clock else self.clock)      # live: in-memory (main poller); sim: static
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
        # Want no order on either side; the router cancels any resting order (rate-limit
        # gated, retried later if budget is exhausted) and logs it.
        ctx = {"event": self.event_ticker, "alpha": None, "expo": self.inventory[self.ticker]}
        for side in ("yes", "no"):
            self.consumer.router.set_target(lts, self.ticker, side, None, ctx)

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
        # single market-only book view shared by alphas/strategy/router. The book
        # contains our own orders (injected by SimExchange, like prod); reads
        # subtract the own-ledger -> market-only.
        self.view = replayer.view
        self.params = params
        # Live-only timing emitter (a live_ipc.TimingEmitter, or None in sim/sweep ->
        # ALL instrumentation below is inert and the sim path stays bit-identical).
        # `_evt` is the per-event timing context the driver (ProdExchange/LiveFeed)
        # stamps with exchange_ts/read_ts before each on_book/on_trade.
        self._timing = getattr(params, "timing_emit", None)
        self._evt: dict = {}
        # The exchange backend has the SAME API in sim and prod. SimExchange replays
        # + simulates our fills (queue model) and emits our order lifecycle as the
        # exact prod message stream; ProdExchange (params.live) drives the live WS +
        # real REST orders and raises the SAME events. Both deliver via _deliver.
        if getattr(params, "live", False):
            from research.hft.exchange import ProdExchange
            self.exchange = ProdExchange(self.view, getattr(params, "tickers", []),
                                         on_deliver = self._deliver)
            self.fill_engine = None                      # prod: no queue model (real private fills)
        else:
            # Feed delays default to 0 (equivalence test); AWS constants for realistic runs.
            self.exchange = SimExchange(
                self.view,
                fwd_delay = getattr(params, "forward_delay", FORWARD_DELAY_S),
                ack_delay = getattr(params, "ack_delay", 0.0),
                pub_delay = getattr(params, "pub_delay", 0.0),
                fill_delay = getattr(params, "fill_delay", 0.0),
                fill_pub_lag = getattr(params, "fill_pub_lag", 0.0),
                on_deliver = self._deliver,
            )
            self.fill_engine = self.exchange.fill_engine     # alias (queue reads)
        # shared write-budget limiter (also used directly by the taker/aggro path);
        # constructed before the router, which owns the passive place/cancel gating.
        self.rate_limiter = WriteRateLimiter(getattr(params, "write_budget", WRITE_BUDGET))
        # the ONLY path passive orders flow through (desired-state: the strategy sets a
        # target per side; the router owns the in-flight lock, rate-limit budget, passive
        # invariants, logging, and routes PnL/inventory off the authoritative fill).
        self.router = OrderRouter(self.exchange, self.view,
                                  rate_limiter = self.rate_limiter,
                                  max_queue = getattr(params, "max_queue_ahead", None),
                                  on_reduce = self._on_fill_reduce,
                                  on_order = self._log_order_timing if self._timing else None,
                                  log = self.log_order)
        self._cur_lts = 0.0          # lts of the recorded message being processed
        self._fill_flag = False      # a fill landed this tick -> requote
        # debug: shadow recorded-market book to assert market-only reads (book minus
        # our injected orders) == the pure recorded market, every tick.
        self._dbg = getattr(params, "debug_shadow", False)
        self._shadow: dict = {}
        self._dbg_hits = 0
        self.pnl = PnL(charge_fees = True, fee_model = "kalshi")
        self.strategies: dict[str, PairMM] = {}
        self.mm_by_ticker: dict[str, PairMM] = {}
        # Per-leg mid history (lts, mid) for markouts
        self.mid_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.fill_rows: list[dict] = []
        self.last_mid: dict[str, float] = {}
        self.peak_deployed: float = 0.0
        # Periodic per-strategy state snapshots for visualization/refit
        self.state_rows: list[dict] = []
        self._last_state_log: dict[str, float] = {}
        # Generic decision log: one row per place/cancel/fill/skip with the
        # context that explains it (works for any strategy; drives viz)
        self.order_rows: list[dict] = []

    # ---- debug shadow (per-tick market-only invariant) ----
    def _shadow_snapshot(self, ticker):
        b = self.view.books[ticker]
        self._shadow[ticker] = {"yes": dict(b.yes.levels), "no": dict(b.no.levels)}

    def _shadow_delta(self, ticker, side, price, delta):
        # recorded market-only level (clamped at 0, like the real market book)
        sh = self._shadow.setdefault(ticker, {"yes": {}, "no": {}})[side]
        q = max(0.0, sh.get(price, 0.0) + delta)
        if is_pos(q):
            sh[price] = q
        else:
            sh.pop(price, None)

    def _check_shadow(self, ticker, lts, ctx):
        if self._dbg_hits >= 15:
            return
        for side in ("yes", "no"):
            mk = self.view.market_levels(ticker, side)
            sh = self._shadow.get(ticker, {}).get(side, {})
            for p in set(mk) | set(sh):
                mv, sv = mk.get(p, 0.0), sh.get(p, 0.0)
                if abs(mv - sv) > 1e-6:
                    self._dbg_hits += 1
                    print(f"SHADOW MISMATCH [{ctx}] lts={lts:.3f} {ticker} {side} px={p}: "
                          f"view_market={mv:.4f} recorded={sv:.4f} diff={mv - sv:.4f} "
                          f"own={self.view.own_qty(ticker, side, p):.4f}", flush = True)
                    if self._dbg_hits >= 15:
                        return

    def log_order(self, lts: float, event: str, ticker: str, side: str, action: str,
                  price, size, alpha, queue_ahead = None, exposure = None):
        if self._timing:
            return                       # live: sent orders are logged via the router on_order hook
        tob = self.view.top(ticker)
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

    def _game_of(self, mm):
        return mm.pair["event_ticker"] if mm.second_ticker is not None else mm.event_ticker

    def _log_order_timing(self, ticker, side, action, price, qty, t_sent, t_done):
        """OrderRouter on_order hook, called AFTER the order is placed (off the order's
        critical path). Assembles the per-order record from the strategy-stamped event
        context (`_evt`) + emits it to the logger. Every field is strategy-sourced; the
        logger computes none of it."""
        mm = self.mm_by_ticker.get(ticker)
        e = self._evt
        self._timing.emit({
            "type": "order", "exchange_ts": e.get("exchange_ts"), "read_ts": e.get("read_ts"),
            "alpha_start": e.get("alpha_start"), "strategy_start": e.get("strategy_start"),
            "sent_to_router": t_sent, "router_done": t_done,
            "game": self._game_of(mm) if mm else None, "leg": ticker, "side": side,
            "action": action, "qty": qty, "price": price, "alpha": e.get("alpha"),
        })

    def _emit_decision(self, mm, lts: float):
        """Per market event where the strategy re-decided: log the decision alpha
        (strategy-sourced). Emitted after requote."""
        e = self._evt
        # obi is a COMPONENT of the obi-family decision alpha (obi_dev = obi - obi_ma),
        # cached from the requote's value_of -> cheap, not an extra alpha. Logged so the
        # offline check can compare the instantaneous obi EXACTLY (book-state-based,
        # immune to the obi_ma warmup/cross-clock noise that confounds obi_dev).
        obi = (mm.alpha_engine.value_of("obi", lts)
               if self.params.alpha_name.startswith("obi") else None)
        self._timing.emit({
            "type": "decision", "exchange_ts": e.get("exchange_ts"), "read_ts": e.get("read_ts"),
            "alpha_start": e.get("alpha_start"), "strategy_start": e.get("strategy_start"),
            "game": self._game_of(mm),
            "leg": mm.ticker if mm.second_ticker is None else mm.first_ticker,
            "alpha": e.get("alpha"), "obi": obi,
        })

    def _record_mid(self, lts: float, ticker: str):
        if self._timing:
            return                       # live: markouts come from the logger's feed
        tob = self.view.top(ticker)
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

    def _deliver(self, kind: str, msg: dict):
        """Dispatch a feed message from the SimExchange (own messages + the inline
        delay-0 confirmations of our own place/cancel). Mirrors the prod consumer:
        own deltas update book + ledger (no alpha, no requote); our own public trade
        is fed to the alpha which self-skips it (is_own_trade); the private fill is
        the authoritative inventory/PnL update."""
        if kind == "public_delta":
            self.view.apply_delta(msg["market_ticker"], msg["side"], msg["price_dollars"],
                                  float(msg["delta_fp"]), is_own = True)
            self.router.on_public_own_delta(msg)
        elif kind == "ack":
            self.router.on_ack(msg)
        elif kind == "reject":                  # prod: exchange rejected a place/cancel
            self.router.on_reject(msg["client_order_id"], msg["kind"])
        elif kind == "public_trade":
            mm = self.mm_by_ticker.get(msg["market_ticker"])
            if mm is not None:
                mm.alpha_engine.on_trade(self._cur_lts, msg)   # excluded via is_own_trade
        elif kind == "private_fill":
            self.router.on_private_fill(msg)

    def _on_fill_reduce(self, ticker: str, side: str, qty: float, yes_price: float,
                        action, reason, post_position_fp = None):
        """Authoritative fill (private channel) -> inventory + PnL + fill log. Replaces
        the old synchronous PairMM.on_fill; driven by OrderRouter.on_private_fill.
        The fill's `post_position_fp` (the account's position AFTER this fill, delivered
        WITH the fill in fill order) is the authoritative position; we set inventory to
        it and WARN if our incremental computation disagrees (drift / missed fill)."""
        mm = self.mm_by_ticker.get(ticker)
        if mm is None:
            return
        lts = self._cur_lts
        # Direction comes from ACTION (buy = long yes / sell = short yes), NOT the
        # book `side`. The real Kalshi fill reports side="yes" for BOTH buy-yes and
        # sell-yes(=buy-no) fills, while sim reports side="no" for no orders — so
        # `side` is not a reliable direction. `action` is the invariant across sim
        # and prod (sell == short yes), so key the sign + cost basis off it.
        if action == "buy":
            computed = mm.inventory[ticker] + qty
            own_price = round(yes_price, 6)
            self.pnl.trade(ticker, "long", qty, yes_price, is_maker = True)
        else:                                          # sell yes (= buy no) -> short
            computed = mm.inventory[ticker] - qty
            own_price = round(1.0 - yes_price, 6)
            self.pnl.trade(ticker, "short", qty, yes_price, is_maker = True)
        if post_position_fp is not None:
            post = float(post_position_fp)
            if abs(post - computed) > 1e-9:
                logger.warning("position mismatch %s: computed %+.2f vs fill post_position_fp %+.2f",
                               ticker, computed, post)
            mm.inventory[ticker] = post
        else:
            mm.inventory[ticker] = computed
        self._fill_flag = True
        self.peak_deployed = max(self.peak_deployed, self._deployed_dollars())
        # live: inventory+PnL above are essential; the fill itself is logged by the
        # separate logger off the private feed -> skip the in-process fill log.
        if self._timing:
            return
        alpha = mm.alpha_engine.value_of(self.params.alpha_name, lts)
        tob = self.view.top(ticker)
        event = mm.pair["event_ticker"] if mm.second_ticker is not None else mm.event_ticker
        self.fill_rows.append({
            "lts": lts, "event_ticker": event, "ticker": ticker, "side": side,
            "price": own_price, "yes_space_price": round(yes_price, 6), "qty": qty,
            "reason": reason, "inventory_after": mm.inventory[ticker],
            "alpha": alpha if alpha is not None else "",
            "mid": tob.mid if tob.mid is not None else "",
            "spread": tob.spread if tob.spread is not None else "",
            "realized_pnl": self.pnl.realized_pnl, "fees_paid": self.pnl.fees_paid,
        })
        self.log_order(lts, event, ticker, side, "fill", own_price, qty, alpha,
                       None, mm._pair_exposure())

    def _maybe_log_state(self, lts: float, mm):
        """Throttled per-strategy snapshot: odds, alpha, position, PnL components."""
        if self._timing:
            return                       # live: state is reconstructable from the logs
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

    def _reinject_own(self, ticker: str):
        """A recorded snapshot is market-only and wipes our orders from the book; add
        our registered resting qty back so the book = market + ours (like a prod
        aggregated/untagged snapshot) and reads subtract the ledger to market-only."""
        for side, levels in self.view.own_levels(ticker).items():
            for pk, qty in levels.items():
                if is_pos(qty):
                    self.view.apply_delta(ticker, side, pk, qty, is_own = True)

    def on_book(self, lts: float, ticker: str, delta_msg):
        mm = self.mm_by_ticker.get(ticker)
        if mm is None:
            return
        self._cur_lts = lts
        self._fill_flag = False
        self.exchange.drain(lts)                  # own messages due before this event
        if delta_msg is None:                     # snapshot (book just reloaded market-only)
            if self._dbg:
                self._shadow_snapshot(ticker)     # recorded market-only (pre re-injection)
            # sim: recorded snapshots are market-only -> re-inject our resting qty.
            # prod: the live snapshot already includes our orders -> do NOT re-inject.
            if self.exchange.simulates:
                self._reinject_own(ticker)
            self.exchange.on_recorded_snapshot(lts, ticker)
        else:                                     # market delta (book already applied by replay)
            if self._dbg:
                self._shadow_delta(ticker, delta_msg["side"], delta_msg["price_dollars"],
                                   float(delta_msg["delta_fp"]))
            mm.alpha_engine.on_delta(lts, ticker, delta_msg)   # agg flow (no-op unless tracked)
            self.exchange.on_recorded_delta(lts, ticker, delta_msg["side"],
                                            delta_msg["price_dollars"], float(delta_msg["delta_fp"]))
        self.exchange.drain(lts)                  # our fill messages from matching
        if self._dbg:
            self._check_shadow(ticker, lts, "book")
        # Every event updates the view, then the strategy re-decides from it — no
        # special-case triggers. _reconcile_side reads market-only depth, so an order
        # at a level with no remaining market backing (only our own qty) is pulled,
        # and alpha changes from deep-book deltas are acted on immediately.
        self._record_mid(lts, ticker)
        mm.alpha_engine.on_book(lts, ticker)
        if self._timing:
            self._evt["strategy_start"] = time.time()      # #4
        mm.requote(lts)
        if self._timing:
            self._emit_decision(mm, lts)
        self._maybe_log_state(lts, mm)

    def on_trade(self, lts: float, msg: dict):
        ticker = msg["market_ticker"]
        mm = self.mm_by_ticker.get(ticker)
        if mm is None:
            return
        self._cur_lts = lts
        self._fill_flag = False
        self.exchange.drain(lts)
        mm.alpha_engine.on_trade(lts, msg)            # recorded taker flow (C) -> alpha
        self.exchange.on_recorded_trade(lts, msg)     # fill engine matches -> schedule our fills
        self.exchange.drain(lts)                       # deliver our fill messages
        if self._dbg:
            self._check_shadow(ticker, lts, "trade")
        self._record_mid(lts, ticker)
        if self._timing:
            self._evt["strategy_start"] = time.time()      # #4
        mm.requote(lts)
        if self._timing:
            self._emit_decision(mm, lts)
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

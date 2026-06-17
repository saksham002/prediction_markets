"""
Alpha engine for one paired sports event, fed by the replay/live message
stream. Alphas are signed in pair space: positive favors the first-listed
team's YES price going up.

Alphas:
  tfma_{hl}     — pair TradeFillMA, raw signed aggressor qty EMA
  tfma_pw_{hl}  — pair TradeFillMA, price-weighted
  obi           — depth-weighted L1-L3 orderbook imbalance, first minus second leg
  mom_{hl}      — pair mid minus EMA(pair mid) (book-pressure momentum)
"""

import heapq
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.signals.agg_flow_ma import AggFlowMA
from research.signals.trade_fill_ma import TradeFillMA
from research.hft.passive_fill import price_key
from src.utils.feps import is_pos, lte

TFMA_HLS = {"1s": 1, "5s": 5, "10s": 10, "30s": 30, "60s": 60, "120s": 120,
            "300s": 300, "600s": 600, "900s": 900, "1800s": 1800}
MOM_HLS = {"1s": 1, "5s": 5, "10s": 10, "30s": 30, "120s": 120, "300s": 300, "600s": 600}
OBI_MA_HLS = {"1s": 1, "5s": 5, "15s": 15, "60s": 60, "300s": 300, "900s": 900}
OBI_LEVELS = 3
OBI_LEVEL_DECAY = 0.5


def _depth(book_side, levels: int = OBI_LEVELS, decay: float = OBI_LEVEL_DECAY,
           own: dict | None = None) -> float | None:
    """Geometrically weighted depth over the top `levels` price levels. `own`
    (price_key -> our resting qty) is subtracted per level so book-state alphas
    exclude our own orders (live self-loop guard); None/empty = no subtraction."""
    lv = book_side.levels
    if not lv:
        return None
    if own:
        lv = {p: q - own.get(p, 0.0) for p, q in lv.items()}
        lv = {p: q for p, q in lv.items() if is_pos(q)}
        if not lv:
            return None
    if len(lv) <= levels:
        prices = sorted(lv.keys(), key = float, reverse = True)
    else:
        prices = heapq.nlargest(levels, lv.keys(), key = float)
    total = 0.0
    w = 1.0
    for p in prices:
        total += w * lv[p]
        w *= decay
    return total


def market_obi(book, own_yes: dict | None = None, own_no: dict | None = None) -> float | None:
    """(bid_depth - ask_depth) / (bid_depth + ask_depth) for one market, in YES
    space. own_yes/own_no subtract our own resting qty from each book side."""
    bid_depth = _depth(book.yes, own = own_yes)
    ask_depth = _depth(book.no, own = own_no)
    if bid_depth is None or ask_depth is None:
        return None
    total = bid_depth + ask_depth
    if lte(total, 0):
        return None
    return (bid_depth - ask_depth) / total


def _best_bid_ex(book_side, own: dict | None = None) -> float | None:
    """Highest price whose displayed qty exceeds OUR own resting qty there (the
    market-only best bid), so our own quotes don't move the mid feeding mom.
    Falls back to the raw best price when we hold nothing on this side."""
    if not own:
        return book_side.best_bid()[0]
    best = None
    for p, q in book_side.levels.items():
        if is_pos(q - own.get(p, 0.0)) and (best is None or float(p) > best):
            best = float(p)
    return best


class _OwnOrderMixin:
    """Live self-loop guard: keep OUR own resting orders and fills out of the
    alphas we trade on, else we self-reinforce (our resting bid inflates bid
    depth -> obi favors buy -> we quote more bid -> ...). Every hook is a no-op
    until the live strategy registers state, so replay/sim (which never
    registers) is unchanged.

      register_resting(ticker, side, price, qty)  our resting qty per level;
          obi/mom are computed on the book MINUS this (qty <= 0 clears the level)
      mark_own_fill(trade_id)  a trade_id seen on the private fill channel; the
          public trade print of the same fill then skips the trade/flow alphas
    """

    def _init_own(self):
        self._own: dict[tuple[str, str], dict[str, float]] = {}
        self._own_trade_ids: set[str] = set()

    def register_resting(self, ticker: str, side: str, price: float, qty: float):
        level = self._own.setdefault((ticker, side), {})
        pk = price_key(price)
        if qty <= 0:
            level.pop(pk, None)
        else:
            level[pk] = qty

    def mark_own_fill(self, trade_id: str):
        self._own_trade_ids.add(trade_id)

    def _own_side(self, ticker: str, side: str) -> dict | None:
        return self._own.get((ticker, side)) or None

    def _is_own_delta(self, msg: dict) -> bool:
        # On the authenticated WS, Kalshi tags ONLY our own orderbook_delta with
        # client_order_id, so its presence identifies the delta as ours.
        return bool(msg.get("client_order_id"))

    def _is_own_trade(self, msg: dict) -> bool:
        return msg.get("trade_id") in self._own_trade_ids


class PairAlphaEngine(_OwnOrderMixin):
    def __init__(self, pair: dict, books: dict, combo: dict | None = None,
                 track_obi_ma: bool = False, track_agg: bool = False):
        """
        combo: optional {"weights": {name: w}, "means": {name: m}, "stds": {name: s}}
        adding a "combo" alpha = sum_i w_i * (alpha_i - m_i) / s_i  (fit_combo.py)
        track_obi_ma: maintain smoothed-OBI EMAs (loggers/studies only — the
        per-book-event OBI recompute is expensive and unused by strategies)
        """
        self.pair = pair
        self.books = books
        self.combo = combo
        # Combos referencing smoothed OBI need the EMA maintained
        self.track_obi_ma = track_obi_ma or bool(
            combo and any(k.startswith("obi_ma") for k in combo["weights"])
        )
        self.first_ticker = pair["first_ticker"]
        self.second_ticker = pair["second_ticker"]
        self.tfma = TradeFillMA(
            pair_tickers = (self.first_ticker, self.second_ticker),
            half_life_seconds = TFMA_HLS,
            time_source = "exchange",
        )
        # Aggregation alpha needs every book delta — only maintained on request
        self.track_agg = track_agg or bool(
            combo and any(k.startswith("agg") for k in combo["weights"])
        )
        self.aggflow = AggFlowMA(
            books, pair_tickers = (self.first_ticker, self.second_ticker),
            half_life_seconds = TFMA_HLS,
        ) if self.track_agg else None
        self._init_own()
        self._mid_ema: dict[str, float | None] = {label: None for label in MOM_HLS}
        self._mid_ema_last_ts: float | None = None
        self._obi_ema: dict[str, float | None] = {label: None for label in OBI_MA_HLS}
        self._obi_ema_last_ts: float | None = None

    def _leg_mid(self, ticker: str) -> float | None:
        book = self.books[ticker]
        yb = _best_bid_ex(book.yes, self._own_side(ticker, "yes"))
        nb = _best_bid_ex(book.no, self._own_side(ticker, "no"))
        if yb is None or nb is None:
            return None
        return (yb + (1.0 - nb)) / 2

    def pair_mid(self) -> float | None:
        """First-leg-space consensus mid: average of first mid and (1 - second mid)."""
        first_mid = self._leg_mid(self.first_ticker)
        second_mid = self._leg_mid(self.second_ticker)
        if first_mid is None and second_mid is None:
            return None
        if first_mid is None:
            return 1.0 - second_mid
        if second_mid is None:
            return first_mid
        return (first_mid + (1.0 - second_mid)) / 2

    def on_trade(self, lts: float, msg: dict):
        if self._is_own_trade(msg):
            return
        self.tfma.on_message("trade", msg)
        if self.aggflow is not None:
            self.aggflow.on_trade(msg)

    def on_delta(self, lts: float, ticker: str, msg: dict):
        if self.aggflow is None or self._is_own_delta(msg):
            return
        ts = float(msg["ts_ms"]) / 1000.0 if "ts_ms" in msg else lts
        self.aggflow.on_delta(ticker, msg["side"], float(msg["price_dollars"]),
                              float(msg["delta_fp"]), ts)

    def _pair_obi(self) -> float | None:
        obi_first = market_obi(self.books[self.first_ticker],
                               own_yes = self._own_side(self.first_ticker, "yes"),
                               own_no = self._own_side(self.first_ticker, "no"))
        obi_second = market_obi(self.books[self.second_ticker],
                                own_yes = self._own_side(self.second_ticker, "yes"),
                                own_no = self._own_side(self.second_ticker, "no"))
        if obi_first is None or obi_second is None:
            return None
        return (obi_first - obi_second) / 2

    def on_book(self, lts: float, ticker: str):
        mid = self.pair_mid()
        if mid is not None:
            if self._mid_ema_last_ts is None:
                for label in MOM_HLS:
                    self._mid_ema[label] = mid
            else:
                dt = max(lts - self._mid_ema_last_ts, 0.0)
                for label, hl in MOM_HLS.items():
                    alpha_w = 1.0 - math.exp(-math.log(2) * dt / hl)
                    self._mid_ema[label] += alpha_w * (mid - self._mid_ema[label])
            self._mid_ema_last_ts = lts

        if not self.track_obi_ma:
            return
        obi = self._pair_obi()
        if obi is not None:
            if self._obi_ema_last_ts is None:
                for label in OBI_MA_HLS:
                    self._obi_ema[label] = obi
            else:
                dt = max(lts - self._obi_ema_last_ts, 0.0)
                for label, hl in OBI_MA_HLS.items():
                    alpha_w = 1.0 - math.exp(-math.log(2) * dt / hl)
                    self._obi_ema[label] += alpha_w * (obi - self._obi_ema[label])
            self._obi_ema_last_ts = lts

    def values(self, now: float) -> dict[str, float | None]:
        """All alpha values. `now` in exchange-epoch seconds (lts works live and in replay)."""
        out: dict[str, float | None] = {}
        raw = self.tfma.values(now = now)
        pw = self.tfma.values_pw(now = now)
        for label in TFMA_HLS:
            out[f"tfma_{label}"] = raw[label]
            out[f"tfma_pw_{label}"] = pw[label]

        agg_lvl = self.aggflow.values_lvl(now = now) if self.aggflow is not None else None
        agg_pw = self.aggflow.values_pw(now = now) if self.aggflow is not None else None
        for label in TFMA_HLS:
            out[f"agg_{label}"] = agg_lvl[label] if agg_lvl is not None else None
            out[f"agg_pw_{label}"] = agg_pw[label] if agg_pw is not None else None

        out["obi"] = self._pair_obi()
        for label in OBI_MA_HLS:
            out[f"obi_ma_{label}"] = self._obi_ema[label]

        # Agreement gate: OBI magnitude, zeroed when dollar-flow disagrees in sign
        obi_v = out["obi"]
        tfma_v = out["tfma_pw_10s"]
        if obi_v is None or tfma_v is None:
            out["agree"] = None
        elif obi_v * tfma_v > 0:
            out["agree"] = obi_v
        else:
            out["agree"] = 0.0

        mid = self.pair_mid()
        for label in MOM_HLS:
            ema = self._mid_ema[label]
            if mid is None or ema is None:
                out[f"mom_{label}"] = None
            else:
                out[f"mom_{label}"] = mid - ema

        # OBI gated by short-term momentum agreement (MLB shows continuation:
        # mom_5s r=+0.13..0.18 on MLB-only, unlike the pooled reversal)
        mom_v = out["mom_5s"]
        if obi_v is None or mom_v is None:
            out["agree_om"] = None
        elif obi_v * mom_v > 0:
            out["agree_om"] = obi_v
        else:
            out["agree_om"] = 0.0

        if self.combo is not None:
            weights = self.combo["weights"]
            means = self.combo["means"]
            stds = self.combo["stds"]
            total = 0.0
            for name, w in weights.items():
                v = out[name]
                if v is None:
                    # Treat not-yet-warm components as at their mean (zero contribution)
                    continue
                total += w * (v - means[name]) / stds[name]
            out["combo"] = total
            # Linear combination + agreement gates: combo magnitude, zeroed
            # unless its sign agrees with momentum / book imbalance
            out["combo_om"] = total if (mom_v is not None and total * mom_v > 0) else 0.0
            out["combo_obi"] = total if (obi_v is not None and total * obi_v > 0) else 0.0
        return out

    def value_of(self, name: str, now: float) -> float | None:
        """Compute ONE alpha lazily — the strategy hot path. Semantically
        identical to values()[name] but skips every unrequested component
        (combo components with zero weight are never computed at all)."""
        if name == "obi":
            return self._pair_obi()
        if name.startswith("obi_ma_"):
            return self._obi_ema[name[7:]]
        if name.startswith("mom_"):
            mid = self.pair_mid()
            ema = self._mid_ema[name[4:]]
            return None if (mid is None or ema is None) else mid - ema
        if name == "agree_om":
            obi_v = self._pair_obi()
            mom_v = self.value_of("mom_5s", now)
            if obi_v is None or mom_v is None:
                return None
            return obi_v if obi_v * mom_v > 0 else 0.0
        if name == "agree":
            obi_v = self._pair_obi()
            tfma_v = self.tfma.values_pw(now = now)["10s"]
            if obi_v is None or tfma_v is None:
                return None
            return obi_v if obi_v * tfma_v > 0 else 0.0
        if name.startswith("tfma_pw_"):
            return self.tfma.values_pw(now = now)[name[8:]]
        if name.startswith("tfma_"):
            return self.tfma.values(now = now)[name[5:]]
        if name.startswith("agg_pw_"):
            return None if self.aggflow is None else self.aggflow.values_pw(now = now)[name[7:]]
        if name.startswith("agg_"):
            return None if self.aggflow is None else self.aggflow.values_lvl(now = now)[name[4:]]
        if name in ("combo", "combo_om", "combo_obi") and self.combo is not None:
            weights = self.combo["weights"]
            means = self.combo["means"]
            stds = self.combo["stds"]
            total = 0.0
            for cname, w in weights.items():
                if w == 0:
                    continue
                v = self.value_of(cname, now)
                if v is None:
                    continue
                total += w * (v - means[cname]) / stds[cname]
            if name == "combo":
                return total
            gate = self.value_of("mom_5s" if name == "combo_om" else "obi", now)
            if gate is None:
                return None
            return total if total * gate > 0 else 0.0
        return self.values(now).get(name)

    @staticmethod
    def alpha_names() -> list[str]:
        names = []
        for label in TFMA_HLS:
            names.append(f"tfma_{label}")
            names.append(f"tfma_pw_{label}")
        for label in TFMA_HLS:
            names.append(f"agg_{label}")
            names.append(f"agg_pw_{label}")
        names.append("obi")
        for label in OBI_MA_HLS:
            names.append(f"obi_ma_{label}")
        names.append("agree")
        names.append("agree_om")
        for label in MOM_HLS:
            names.append(f"mom_{label}")
        return names


class SingleAlphaEngine(_OwnOrderMixin):
    """Per-market alphas for N-outcome events (soccer win/draw/win), where the
    two-market pair mirror does not apply. Signals are in the market's own
    YES space: positive favors its YES price rising.

    Provides the same names used by the strategies: obi, mom_{hl},
    tfma_pw_{hl}, agree_om (obi gated by mom_5s sign agreement)."""

    def __init__(self, ticker: str, books: dict, track_obi_ma: bool = False,
                 combo: dict | None = None, track_agg: bool = False):
        self.ticker = ticker
        # Polymorphism with PairAlphaEngine for samplers/strategies: a single
        # market is its own "pair" with both legs the same ticker
        self.first_ticker = ticker
        self.second_ticker = ticker
        self.books = books
        self.combo = combo
        self.track_obi_ma = track_obi_ma or bool(
            combo and any(k.startswith("obi_ma") for k in combo["weights"])
        )
        self.tfma = TradeFillMA(ticker, half_life_seconds = TFMA_HLS, time_source = "exchange")
        self.track_agg = track_agg or bool(
            combo and any(k.startswith("agg") for k in combo["weights"])
        )
        self.aggflow = AggFlowMA(
            books, ticker, half_life_seconds = TFMA_HLS,
        ) if self.track_agg else None
        self._init_own()
        self._mid_ema: dict[str, float | None] = {label: None for label in MOM_HLS}
        self._mid_ema_last_ts: float | None = None
        self._obi_ema: dict[str, float | None] = {label: None for label in OBI_MA_HLS}
        self._obi_ema_last_ts: float | None = None

    def _mid(self) -> float | None:
        book = self.books[self.ticker]
        yb = _best_bid_ex(book.yes, self._own_side(self.ticker, "yes"))
        nb = _best_bid_ex(book.no, self._own_side(self.ticker, "no"))
        if yb is None or nb is None:
            return None
        return (yb + (1.0 - nb)) / 2

    def _obi(self) -> float | None:
        return market_obi(self.books[self.ticker],
                          own_yes = self._own_side(self.ticker, "yes"),
                          own_no = self._own_side(self.ticker, "no"))

    def pair_mid(self) -> float | None:
        return self._mid()

    def on_trade(self, lts: float, msg: dict):
        if self._is_own_trade(msg):
            return
        self.tfma.on_message("trade", msg)
        if self.aggflow is not None:
            self.aggflow.on_trade(msg)

    def on_delta(self, lts: float, ticker: str, msg: dict):
        if self.aggflow is None or self._is_own_delta(msg):
            return
        ts = float(msg["ts_ms"]) / 1000.0 if "ts_ms" in msg else lts
        self.aggflow.on_delta(ticker, msg["side"], float(msg["price_dollars"]),
                              float(msg["delta_fp"]), ts)

    def on_book(self, lts: float, ticker: str):
        mid = self._mid()
        if mid is not None:
            if self._mid_ema_last_ts is None:
                for label in MOM_HLS:
                    self._mid_ema[label] = mid
            else:
                dt = max(lts - self._mid_ema_last_ts, 0.0)
                for label, hl in MOM_HLS.items():
                    alpha_w = 1.0 - math.exp(-math.log(2) * dt / hl)
                    self._mid_ema[label] += alpha_w * (mid - self._mid_ema[label])
            self._mid_ema_last_ts = lts

        if not self.track_obi_ma:
            return
        obi = self._obi()
        if obi is not None:
            if self._obi_ema_last_ts is None:
                for label in OBI_MA_HLS:
                    self._obi_ema[label] = obi
            else:
                dt = max(lts - self._obi_ema_last_ts, 0.0)
                for label, hl in OBI_MA_HLS.items():
                    alpha_w = 1.0 - math.exp(-math.log(2) * dt / hl)
                    self._obi_ema[label] += alpha_w * (obi - self._obi_ema[label])
            self._obi_ema_last_ts = lts

    def values(self, now: float) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        raw = self.tfma.values(now = now)
        pw = self.tfma.values_pw(now = now)
        for label in TFMA_HLS:
            out[f"tfma_{label}"] = raw[label]
            out[f"tfma_pw_{label}"] = pw[label]

        agg_lvl = self.aggflow.values_lvl(now = now) if self.aggflow is not None else None
        agg_pw = self.aggflow.values_pw(now = now) if self.aggflow is not None else None
        for label in TFMA_HLS:
            out[f"agg_{label}"] = agg_lvl[label] if agg_lvl is not None else None
            out[f"agg_pw_{label}"] = agg_pw[label] if agg_pw is not None else None

        out["obi"] = self._obi()
        for label in OBI_MA_HLS:
            out[f"obi_ma_{label}"] = self._obi_ema[label]

        obi_v = out["obi"]
        tfma_v = out["tfma_pw_10s"]
        if obi_v is None or tfma_v is None:
            out["agree"] = None
        else:
            out["agree"] = obi_v if obi_v * tfma_v > 0 else 0.0

        mid = self._mid()
        for label in MOM_HLS:
            ema = self._mid_ema[label]
            out[f"mom_{label}"] = None if (mid is None or ema is None) else mid - ema

        obi_v = out["obi"]
        mom_v = out["mom_5s"]
        if obi_v is None or mom_v is None:
            out["agree_om"] = None
        elif obi_v * mom_v > 0:
            out["agree_om"] = obi_v
        else:
            out["agree_om"] = 0.0
        return out

    def value_of(self, name: str, now: float) -> float | None:
        """Single-alpha hot path (see PairAlphaEngine.value_of)."""
        if name == "obi":
            return self._obi()
        if name.startswith("obi_ma_"):
            return self._obi_ema[name[7:]]
        if name.startswith("mom_"):
            mid = self._mid()
            ema = self._mid_ema[name[4:]]
            return None if (mid is None or ema is None) else mid - ema
        if name.startswith("agree_om_"):
            # obi gated by momentum sign at a chosen HL (agree_om == mom_5s)
            obi_v = self._obi()
            mom_v = self.value_of("mom_" + name[len("agree_om_"):], now)
            if obi_v is None or mom_v is None:
                return None
            return obi_v if obi_v * mom_v > 0 else 0.0
        if name == "agree_om":
            obi_v = self._obi()
            mom_v = self.value_of("mom_5s", now)
            if obi_v is None or mom_v is None:
                return None
            return obi_v if obi_v * mom_v > 0 else 0.0
        if name == "agree_agg":
            # obi refined by the aggregation alpha: zero unless agg agrees in sign
            obi_v = self._obi()
            agg_v = self.value_of("agg_300s", now)
            if obi_v is None or agg_v is None:
                return None
            return obi_v if obi_v * agg_v > 0 else 0.0
        if name == "agree_agg_1s":
            # obi gated by SHORT-HL agg flow (1s): zero unless agg_1s agrees in sign
            obi_v = self._obi()
            agg_v = self.value_of("agg_1s", now)
            if obi_v is None or agg_v is None:
                return None
            return obi_v if obi_v * agg_v > 0 else 0.0
        if name.startswith("obi_dev_"):
            # obi minus its trailing EMA (obi_ma) at a chosen HL (deviation signal)
            obi_v = self._obi()
            ma = self._obi_ema[name[len("obi_dev_"):]]
            return None if (obi_v is None or ma is None) else obi_v - ma
        if name.startswith("tfma_pw_ratio_"):
            return self.tfma.values_pw_ratio(now = now)[name[len("tfma_pw_ratio_"):]]
        if name.startswith("tfma_ratio_"):
            return self.tfma.values_ratio(now = now)[name[len("tfma_ratio_"):]]
        if name.startswith("agg_pw_ratio_"):
            return None if self.aggflow is None else self.aggflow.values_pw_ratio(now = now)[name[len("agg_pw_ratio_"):]]
        if name.startswith("agg_ratio_"):
            return None if self.aggflow is None else self.aggflow.values_lvl_ratio(now = now)[name[len("agg_ratio_"):]]
        if name.startswith("tfma_pw_"):
            return self.tfma.values_pw(now = now)[name[8:]]
        if name.startswith("tfma_"):
            return self.tfma.values(now = now)[name[5:]]
        if name.startswith("agg_pw_"):
            return None if self.aggflow is None else self.aggflow.values_pw(now = now)[name[7:]]
        if name.startswith("agg_"):
            return None if self.aggflow is None else self.aggflow.values_lvl(now = now)[name[4:]]
        if name in ("combo", "combo_om", "combo_obi") and self.combo is not None:
            weights = self.combo["weights"]
            means = self.combo["means"]
            stds = self.combo["stds"]
            total = 0.0
            for cname, w in weights.items():
                if w == 0:
                    continue
                v = self.value_of(cname, now)
                if v is None:
                    continue
                total += w * (v - means[cname]) / stds[cname]
            if name == "combo":
                return total
            gate = self.value_of("mom_5s" if name == "combo_om" else "obi", now)
            if gate is None:
                return None
            return total if total * gate > 0 else 0.0
        return self.values(now).get(name)

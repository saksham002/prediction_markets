"""Cross-leg latency-arb backtest for 3-outcome WC events.

Detector: event E = >= L distinct price levels swept within T ms on one side of a
leg (mode 'trade' = levels consumed by trades; 'flow' = wiped by any negative
delta). That leg = LEADER, its side = direction.

Attribution (Dixon-Coles, rho=-0.13): track the true score (ESPN), calibrate
(mu1,mu2)=expected remaining goals to the pre-goal book odds with the DC tau
low-score correction, identify the scorer from leader+direction, increment the
score, recompute the DC fair odds -> each follower's target. The predicted delta
(fair - pre) is CAPPED at CAP_LEVELS ticks. We aggress each stale follower toward
its capped target at t_detect+latency (taker, fees), then liquidate passively at
the target (PassiveFillEngine), with an aggressive fallback after liq_timeout.
$1000 budget + per-event sizing cap.

Usage: arb_sim.py <game.jsonl.gz> --t-ms 2 --levels 7 [--mode trade] ...
"""
import argparse
import math
import sys
from collections import deque, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.pnl import PnL
from research.hft.passive_fill import PassiveFillEngine, FORWARD_DELAY_S
from research.hft.replay import Replayer
from research.hft.espn_clock import clocks_for

TICK = 0.01
COOLDOWN_S = 10.0
PRE_LAG_S = 0.3
RHO = -0.13
KSUM = 8
MU_GRID = [round(0.1 * i, 2) for i in range(1, 41)]   # expected remaining goals in [0.1, 4.0]


# ---- Dixon-Coles in-play model ----
def _pois(k, mu):
    return math.exp(-mu) * mu ** k / math.factorial(k)


def _tau(fh, fa, l, m, rho):
    if fh == 0 and fa == 0:
        return 1 - l * m * rho
    if fh == 0 and fa == 1:
        return 1 + l * rho
    if fh == 1 and fa == 0:
        return 1 + m * rho
    if fh == 1 and fa == 1:
        return 1 - rho
    return 1.0


def dc_probs(h, a, mu1, mu2, rho):
    p1 = [_pois(k, mu1) for k in range(KSUM + 1)]
    p2 = [_pois(k, mu2) for k in range(KSUM + 1)]
    H = D = A = 0.0
    for x in range(KSUM + 1):
        for y in range(KSUM + 1):
            fh, fa = h + x, a + y
            p = p1[x] * p2[y] * _tau(fh, fa, mu1, mu2, rho)
            if fh > fa:
                H += p
            elif fh == fa:
                D += p
            else:
                A += p
    s = H + D + A
    return H / s, D / s, A / s


def dc_calibrate(h, a, t1, t2, rho):
    best = None
    for m1 in MU_GRID:
        for m2 in MU_GRID:
            H, D, A = dc_probs(h, a, m1, m2, rho)
            err = (H - t1) ** 2 + (A - t2) ** 2
            if best is None or err < best[0]:
                best = (err, m1, m2)
    return best[1], best[2]


class ArbConsumer:
    def __init__(self, replayer, p):
        self.r = replayer
        self.p = p
        self.books = replayer.books
        self.legs = []
        self.team_legs = []
        self.tie = None
        self.goals = []            # (wc, team_abbr) actual goals (ESPN) for true score
        self.pnl = PnL(charge_fees = True, fee_model = "kalshi")
        self.fill = PassiveFillEngine(self.books, forward_delay = p.latency)
        self.midh = {}
        self.swept = defaultdict(deque)   # (leg, dir) -> deque[(lts, price)]
        self.cooldown = {}
        self.pending = []
        self.liq_side = {}
        self.fires = []

    def on_meta(self, lts, meta):
        for ev in meta.get("events", []):
            if ev["series"] != self.p.series:
                continue
            self.pnl.series_fees[ev["series"]] = (ev.get("fee_multiplier", 1.0), ev.get("fee_type", "quadratic"))
            for t in ev["tickers"]:
                self.pnl.market_to_series[t] = ev["series"]
                if t not in self.legs:
                    self.legs.append(t); self.midh.setdefault(t, deque())
            self.team_legs = [t for t in self.legs if not t.endswith("-TIE")]
            self.tie = next((t for t in self.legs if t.endswith("-TIE")), None)
            for go in (clocks_for(ev["event_ticker"]) or {}).get("events", []):
                if go["kind"] == "goal":
                    self.goals.append((go["wc"], go["team"]))

    def _abbr(self, leg):
        return leg.split("-")[-1]

    def on_trade(self, lts, msg):
        for f in self.fill.on_trade(lts, msg):
            self._on_liq_fill(lts, f)
        if self.p.mode == "trade" and msg["market_ticker"] in self.midh:
            # taker buys yes -> lifts ask (leg up); taker sells yes -> hits bid (leg down)
            if msg["taker_side"] == "yes":
                self._sweep(lts, msg["market_ticker"], "up", float(msg["no_price_dollars"]))
            else:
                self._sweep(lts, msg["market_ticker"], "down", float(msg["yes_price_dollars"]))

    def on_book(self, lts, ticker, delta):
        if ticker not in self.midh:
            return
        if delta is not None:
            m = delta.get("msg", delta)
            d = float(m.get("delta_fp", 0))
            if d < 0:
                self.fill.record_delta(lts, ticker, m["side"], m["price_dollars"], d)
                if self.p.mode == "flow" and d <= -5:
                    # yes-side wipe -> bid removed (leg down); no-side wipe -> ask removed (leg up)
                    self._sweep(lts, ticker, "down" if m["side"] == "yes" else "up", round(float(m["price_dollars"]), 2))
        tob = self.r.top(ticker)
        if tob.mid is not None:
            self.midh[ticker].append((lts, tob.mid))
            while self.midh[ticker] and self.midh[ticker][0][0] < lts - 5.0:
                self.midh[ticker].popleft()
        self._exec_pending(lts)
        for f in self.fill.on_book(lts, ticker):
            self._on_liq_fill(lts, f)
        self._liq_timeout(lts)

    # ---- detector: L levels swept within T ms ----
    def _sweep(self, lts, leg, direction, price):
        q = self.swept[(leg, direction)]
        q.append((lts, price))
        win = self.p.t_ms / 1000.0
        while q and q[0][0] < lts - win:
            q.popleft()
        if lts - self.cooldown.get(leg, -1e9) < COOLDOWN_S:
            return
        if len({pr for _, pr in q}) >= self.p.levels:
            self.cooldown[leg] = lts
            self.fires.append((lts, leg, direction))
            self.pending.append((lts + self.p.latency, leg, direction, lts))

    def _exec_pending(self, lts):
        ready = [e for e in self.pending if e[0] <= lts]
        if not ready:
            return
        self.pending = [e for e in self.pending if e[0] > lts]
        for _, leader, direction, t_detect in ready:
            self._enter(lts, leader, direction, t_detect)

    def _pre_mid(self, leg, t):
        cand = [m for ts, m in self.midh[leg] if ts <= t - PRE_LAG_S]
        return cand[-1] if cand else None

    def _score_before(self, t):
        sc = defaultdict(int)
        for wc, team in self.goals:
            if wc < t:
                sc[team] += 1
        return sc

    def _enter(self, lts, leader, direction, t_detect):
        if not self.team_legs or self.tie is None or len(self.team_legs) != 2:
            return
        t1, t2 = self.team_legs
        pre = {l: self._pre_mid(l, t_detect) for l in self.legs}
        if any(v is None for v in pre.values()):
            return
        sp = pre[t1] + pre[self.tie] + pre[t2]
        P1, P2 = pre[t1] / sp, pre[t2] / sp
        sc = self._score_before(t_detect)
        h, a = sc[self._abbr(t1)], sc[self._abbr(t2)]
        # scorer from leader + direction
        if leader == t1:
            scorer = t1 if direction == "up" else t2
        elif leader == t2:
            scorer = t2 if direction == "up" else t1
        else:   # TIE leader: 'up' = equalizer -> trailing team scored; 'down' ambiguous -> skip
            if direction != "up" or h == a:
                return
            scorer = t1 if h < a else t2
        mu1, mu2 = dc_calibrate(h, a, P1, P2, RHO)
        if scorer == t1:
            h += 1
        else:
            a += 1
        fH, fD, fA = dc_probs(h, a, mu1, mu2, RHO)
        fair = {t1: fH, self.tie: fD, t2: fA}
        cap = self.p.cap_levels * TICK
        self._event_room = min(getattr(self.p, "per_event_cap", 100.0), self.p.budget - self._deployed())
        for leg in self.legs:
            if leg == leader:
                continue
            delta = max(-cap, min(cap, fair[leg] - pre[leg]))    # cap predicted delta to 10 levels
            self._aggress(lts, leg, pre[leg] + delta)

    def _deployed(self):
        tot = 0.0
        for pos in self.pnl.positions.values():
            tot += pos.qty * (pos.avg_price if pos.side == "long" else 1.0 - pos.avg_price)
        return tot

    def _aggress(self, lts, leg, target):
        book = self.books[leg]; margin = self.p.margin
        cur = self.r.top(leg).mid
        if cur is None:
            return
        if target > cur + margin:        # follower should RISE -> buy yes (lift asks/no-bids)
            for ps in sorted(book.no.levels, key = float, reverse = True):
                yes_ask = round(1.0 - float(ps), 4)
                if yes_ask >= target - margin:
                    break
                self._take(lts, leg, "long", yes_ask, book.no.levels[ps], target)
        elif target < cur - margin:      # follower should FALL -> sell yes (hit bids)
            for ps in sorted(book.yes.levels, key = float, reverse = True):
                price = float(ps)
                if price <= target + margin:
                    break
                self._take(lts, leg, "short", price, book.yes.levels[ps], target)

    def _take(self, lts, leg, side, price, avail, target):
        unit = price if side == "long" else 1.0 - price
        if unit <= 1e-6:
            return
        room = min(self.p.budget - self._deployed(), getattr(self, "_event_room", 1e18))
        qty = min(avail, room / unit)
        if qty < 1:
            return
        self._event_room = getattr(self, "_event_room", 1e18) - qty * unit
        self.pnl.trade(leg, side, qty, price, is_maker = False)
        if side == "long":
            oid = self.fill.place(lts, leg, "no", round(1.0 - target, 4), qty)
        else:
            oid = self.fill.place(lts, leg, "yes", round(target, 4), qty)
        self.liq_side[oid] = leg

    def _on_liq_fill(self, lts, f):
        leg = f.order.ticker
        if f.order.side == "no":
            self.pnl.trade(leg, "short", f.qty, round(1.0 - f.order.price_f, 4), is_maker = True)
        else:
            self.pnl.trade(leg, "long", f.qty, f.order.price_f, is_maker = True)

    def _liq_timeout(self, lts):
        timeout = getattr(self.p, "liq_timeout", 30.0)
        for oid in list(self.liq_side):
            o = self.fill.orders.get(oid)
            if o is None:
                del self.liq_side[oid]; continue
            if lts - o.placed_lts > timeout:
                leg = self.liq_side.pop(oid); self.fill.cancel(oid); self._flatten(lts, leg)

    def _flatten(self, lts, leg):
        pos = self.pnl.positions.get(leg)
        if pos is None:
            return
        tob = self.r.top(leg)
        if pos.side == "long" and tob.yes_bid is not None:
            self.pnl.trade(leg, "short", pos.qty, tob.yes_bid, is_maker = False)
        elif pos.side == "short" and tob.yes_ask is not None:
            self.pnl.trade(leg, "long", pos.qty, tob.yes_ask, is_maker = False)


def run_one(path, p):
    r = Replayer(path); c = ArbConsumer(r, p); r.run(c)
    last_mid = {leg: r.top(leg).mid for leg in c.legs if r.top(leg).mid is not None}
    net = c.pnl.net_total_pnl(prices = last_mid)
    return {"fires": len(c.fires), "realized": c.pnl.realized_pnl, "fees": c.pnl.fees_paid,
            "net": net, "realized_net": c.pnl.realized_pnl - c.pnl.fees_paid,
            "open": sum(pos.qty for pos in c.pnl.positions.values()),
            "fire_lts": [f[0] for f in c.fires]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("game")
    ap.add_argument("--t-ms", type = float, default = 2)
    ap.add_argument("--levels", type = int, default = 7)
    ap.add_argument("--mode", default = "trade", choices = ["trade", "flow"])
    ap.add_argument("--latency", type = float, default = FORWARD_DELAY_S)
    ap.add_argument("--budget", type = float, default = 1000.0)
    ap.add_argument("--per-event-cap", dest = "per_event_cap", type = float, default = 100.0)
    ap.add_argument("--margin", type = float, default = 0.02)
    ap.add_argument("--cap-levels", dest = "cap_levels", type = int, default = 10)
    ap.add_argument("--liq-timeout", dest = "liq_timeout", type = float, default = 30.0)
    ap.add_argument("--series", default = "KXWCGAME")
    a = ap.parse_args()
    res = run_one(Path(a.game), a)
    print(f"{Path(a.game).stem.replace('.jsonl',''):<28} T{a.t_ms:g} L{a.levels} {a.mode} "
          f"fires={res['fires']:>3} realized_net={res['realized_net']:+8.1f} net={res['net']:+8.1f} "
          f"fees={res['fees']:.1f} open={res['open']:.0f}")


if __name__ == "__main__":
    main()

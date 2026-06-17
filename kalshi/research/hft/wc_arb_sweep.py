"""Cross-leg arb PnL sweep over the detection thresholds T x L (trade-mode
level-wipe detector), with the Dixon-Coles score-based attribution + 10-level
delta cap + per-event sizing ($1000 budget, 20ms latency). In = first-4 WC games,
out = last-4. Outputs, per config, BOTH the PnL (in/out net + realized-net) and
the detection confusion matrix (TP / FN / FP vs significant >=5c goals).

Usage: wc_arb_sweep.py --collect  (runs everything; writes CSV + prints table)
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from arb_sim import run_one
from research.hft.replay import Replayer
from research.hft.espn_clock import clocks_for
from research.hft.passive_fill import FORWARD_DELAY_S

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
OUT = Path("/home/saksham3/projects/personal/prediction_markets/plots/arb_pnl_sweep.csv")
TS = [2, 4, 6, 8, 10]
LS = [3, 4, 5, 6, 7]


def cfg(t, l):
    return SimpleNamespace(t_ms = t, levels = l, mode = "trade", latency = FORWARD_DELAY_S,
                           budget = 1000.0, per_event_cap = 100.0, margin = 0.02,
                           cap_levels = 10, liq_timeout = 30.0, series = "KXWCGAME")


def med(xs):
    xs = sorted(xs); n = len(xs)
    return None if n == 0 else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)


def sig_windows(g):
    """significant (>=5c stable move) goal windows [(lo,hi)] for a game."""
    ev = g.stem.replace(".jsonl", "")
    clk = clocks_for(ev) or {}
    goals = [e for e in clk.get("events", []) if e["kind"] == "goal"]
    if not goals:
        return []
    r = Replayer(g); mids = defaultdict(list); last = {}; box = {}

    class C:
        def on_meta(s, lts, m):
            for e in m.get("events", []):
                if e["series"] == "KXWCGAME":
                    box["legs"] = e["tickers"]
        def on_trade(s, lts, m): pass
        def on_book(s, lts, t, d):
            tob = r.top(t)
            if tob.mid is not None and lts - last.get(t, 0) >= 0.25:
                last[t] = lts; mids[t].append((lts, tob.mid))
    r.run(C())
    legs = box.get("legs", [])
    out = []
    for go in goals:
        wc = go["wc"]
        pre = {l: med([m for ts, m in mids[l] if wc - 90 <= ts <= wc - 45]) for l in legs}
        post = {l: med([m for ts, m in mids[l] if wc + 45 <= ts <= wc + 120]) for l in legs}
        d = [abs(post[l] - pre[l]) for l in legs if pre[l] is not None and post[l] is not None]
        if d and max(d) >= 0.05:
            out.append((wc - 12, wc + 5))
    return out


def main():
    games = sorted(DATASET.glob("KXWCGAME*.jsonl.gz"))
    inn, out = games[:4], games[4:8]
    sig = {g: sig_windows(g) for g in games}
    rows = []
    for t in TS:
        for l in LS:
            c = cfg(t, l)
            agg = {"in_net": 0.0, "out_net": 0.0, "in_rn": 0.0, "out_rn": 0.0,
                   "TP": 0, "FN": 0, "FP": 0}
            for g in games:
                res = run_one(g, c)
                tag = "in" if g in inn else "out"
                agg[f"{tag}_net"] += res["net"]
                agg[f"{tag}_rn"] += res["realized_net"]
                matched = set(); hits = 0
                for flts in res["fire_lts"]:
                    m = next((wi for wi, (lo, hi) in enumerate(sig[g]) if lo <= flts <= hi), None)
                    if m is not None:
                        matched.add(m); hits += 1
                agg["TP"] += len(matched)
                agg["FN"] += len(sig[g]) - len(matched)
                agg["FP"] += len(res["fire_lts"]) - hits
            rows.append({"t_ms": t, "levels": l, **{k: round(v, 1) if isinstance(v, float) else v
                                                     for k, v in agg.items()}})
            r = rows[-1]
            print(f"T{t:<3} L{l}  in_net={r['in_net']:+8.1f} out_net={r['out_net']:+8.1f}  "
                  f"in_rn={r['in_rn']:+8.1f} out_rn={r['out_rn']:+8.1f}  "
                  f"TP={r['TP']} FN={r['FN']} FP={r['FP']}", flush = True)
    OUT.parent.mkdir(parents = True, exist_ok = True)
    with open(OUT, "w", newline = "") as fh:
        w = csv.DictWriter(fh, fieldnames = ["t_ms", "levels", "in_net", "out_net", "in_rn", "out_rn", "TP", "FN", "FP"])
        w.writeheader(); w.writerows(rows)
    bo = max(rows, key = lambda r: r["in_net"])
    print(f"\nwrote {OUT}\nbest-in: T{bo['t_ms']} L{bo['levels']} -> in_net {bo['in_net']:+.1f} / out_net {bo['out_net']:+.1f}")


if __name__ == "__main__":
    main()

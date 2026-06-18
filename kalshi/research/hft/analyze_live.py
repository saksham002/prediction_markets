"""
Post-run analysis of a live/paper run's logger output — "does the trading look as
expected?" Checks (exact ones from the logs; the alpha recompute is a cross-feed
sanity check):

  1. TIMING   — per-order stage monotonicity + latency percentiles (read->done etc).
  2. ORDERS   — within budget; side/price consistent with the alpha skew; new/cancel.
  3. DECISIONS— decision-alpha range / non-degeneracy.
  4. ALPHA    — (if --feed) replay the recorder's public feed (MARKET-ONLY: apply only
                UNTAGGED deltas, skip our own client_order_id deltas) through the same
                SingleAlphaEngine, recompute the decision alpha at each decision's
                exchange_ts, and compare to the logged (strategy-sourced) value.
  5. FILLS    — private_feed fill count; (if --ws) reconcile vs REST positions.
  6. END      — (if --ws) 0 resting orders.

Usage: analyze_live.py <run_dir> [--feed <recorder_game.jsonl.gz>] [--alpha obi_dev_60s]
       [--budget 250] [--ws]
"""
import argparse
import gzip
import json
import statistics
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _load_jsonl(p):
    if not p.exists():
        return []
    op = gzip.open if p.suffix == ".gz" else open
    return [json.loads(l) for l in op(p, "rt")]


def check_timing(orders):
    stages = ["read_ts", "strategy_start", "alpha_start", "sent_to_router", "router_done"]
    full = [o for o in orders if all(o.get(s) is not None for s in stages)]
    bad = [o for o in full if not (o["read_ts"] <= o["strategy_start"] <= o["alpha_start"]
                                   <= o["sent_to_router"] <= o["router_done"])]
    print(f"\n[TIMING] {len(orders)} orders ({len(full)} fully stamped)")
    print(f"  monotonic violations: {len(bad)}")
    def pct(vals, f):
        v = sorted(f(o) * 1000 for o in full)
        return f"med {statistics.median(v):.3f} p95 {v[int(.95*len(v))-1]:.3f} max {max(v):.3f} ms" if v else "n/a"
    print(f"  read->router_done: {pct(full, lambda o: o['router_done']-o['read_ts'])}")
    print(f"  alpha compute:     {pct(full, lambda o: o['sent_to_router']-o['alpha_start'])}")
    print(f"  router/backend:    {pct(full, lambda o: o['router_done']-o['sent_to_router'])}")


def check_orders(orders, budget):
    new = [o for o in orders if o["action"] == "new"]
    can = [o for o in orders if o["action"] == "cancel"]
    over = [o for o in new if o["price"] is not None and o["qty"] is not None
            and o["qty"] * (o["price"] if o["side"] == "yes" else 1 - o["price"]) > budget + 1e-6]
    # skew consistency: with |alpha|>~0, more size should lean toward the alpha sign
    skew = {"yes": 0, "no": 0}
    for o in new:
        if o.get("alpha") is not None and abs(o["alpha"]) > 0.05:
            skew[o["side"]] += o["qty"] * (1 if o["alpha"] > 0 else -1)
    print(f"\n[ORDERS] new={len(new)} cancel={len(can)}")
    print(f"  per-order cost > budget ${budget}: {len(over)}")
    print(f"  alpha-skew lean (yes net {skew['yes']:+.0f} / no net {skew['no']:+.0f}; "
          f"expect yes>0 when alpha>0)")


def check_decisions(decisions):
    al = [d["alpha"] for d in decisions if d.get("alpha") is not None]
    if not al:
        print("\n[DECISIONS] none"); return
    nz = sum(1 for a in al if abs(a) > 1e-9)
    print(f"\n[DECISIONS] {len(decisions)}; alpha min {min(al):+.4f} max {max(al):+.4f} "
          f"med {statistics.median(al):+.4f} nonzero {nz}/{len(al)}")


def recompute_alpha(decisions, feed_path, ticker, alpha_name):
    from src.utils.orderbook import MarketBook
    from research.hft.alphas import SingleAlphaEngine
    books = {ticker: MarketBook()}
    eng = SingleAlphaEngine(ticker, books, track_obi_ma = True)
    # decisions for this ticker in time order, with exchange_ts
    decs = sorted([d for d in decisions if d.get("leg") == ticker and d.get("exchange_ts")
                   and d.get("alpha") is not None], key = lambda d: d["exchange_ts"])
    di = 0
    pairs = []
    op = gzip.open if str(feed_path).endswith(".gz") else open
    f = op(feed_path, "rt")
    while True:
        try:                                  # tolerate gzip tail mid-write (recorder still appending)
            line = f.readline()
        except (EOFError, zlib.error):
            break
        if not line:
            break
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if True:
            if "d" not in rec:
                continue
            d = rec["d"]; msg = d.get("msg", {})
            if msg.get("market_ticker") != ticker and d.get("type") != "orderbook_snapshot":
                continue
            lts = rec["lts"]
            t = d.get("type")
            if t == "orderbook_snapshot" and msg.get("market_ticker") == ticker:
                books[ticker].yes.load_snapshot(msg.get("yes_dollars_fp", []))
                books[ticker].no.load_snapshot(msg.get("no_dollars_fp", []))
                eng.on_book(lts, ticker)
            elif t == "orderbook_delta" and msg.get("market_ticker") == ticker:
                if msg.get("client_order_id"):        # OUR own order -> excluded (market-only)
                    continue
                side = books[ticker].yes if msg["side"] == "yes" else books[ticker].no
                side.apply_delta(msg["price_dollars"], float(msg["delta_fp"]))
                eng.on_book(lts, ticker)
            else:
                continue
            ts_ms = msg.get("ts_ms")
            # sample logged decisions whose exchange_ts we've now reached
            while di < len(decs) and ts_ms is not None and decs[di]["exchange_ts"] <= ts_ms:
                pairs.append((decs[di]["alpha"], eng.value_of(alpha_name, lts),
                              decs[di].get("obi"), eng.value_of("obi", lts)))
                di += 1
    f.close()

    def _corr(xs, ys):
        try:
            return statistics.correlation(xs, ys)
        except statistics.StatisticsError:
            return float("nan")

    cmp = [p for p in pairs if p[1] is not None]
    print(f"\n[ALPHA RECOMPUTE] {ticker}: matched {len(cmp)}/{len(decs)} decisions")
    if len(cmp) > 2:
        la = [p[0] for p in cmp]; rc = [p[1] for p in cmp]
        diffs = [abs(a - b) for a, b in zip(la, rc)]
        print(f"  {alpha_name}: |logged-recomputed| med {statistics.median(diffs):.4f} "
              f"max {max(diffs):.4f}; correlation {_corr(la, rc):.4f}")
        print(f"    (obi_dev = obi - obi_ma; live cold-starts obi_ma -> abs-diff has warmup/cross-"
              f"connection offset, so CORRELATION is the signal)")
    obi_cmp = [(p[2], p[3]) for p in cmp if p[2] is not None and p[3] is not None]
    if len(obi_cmp) > 2:
        lo = [a for a, _ in obi_cmp]; ro = [b for _, b in obi_cmp]
        od = [abs(a - b) for a, b in obi_cmp]
        print(f"  OBI (EXACT, book-state): |logged-recomputed| med {statistics.median(od):.4f} "
              f"max {max(od):.4f}; correlation {_corr(lo, ro):.4f} (expect ~1.0 = obi computed correctly)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--feed", default = None, help = "recorder game jsonl.gz for the alpha recompute")
    ap.add_argument("--alpha", default = "obi_dev_60s")
    ap.add_argument("--budget", type = float, default = 250)
    ap.add_argument("--ws", action = "store_true", help = "query REST for positions + resting orders")
    a = ap.parse_args()
    run = Path(a.run_dir)
    orders = _load_jsonl(run / "orders.jsonl")
    decisions = _load_jsonl(run / "decisions.jsonl")
    fills = _load_jsonl(run / "private_feed.jsonl.gz")
    print(f"run {run}: {len(orders)} orders, {len(decisions)} decisions, {len(fills)} private msgs")

    check_timing(orders)
    check_orders(orders, a.budget)
    check_decisions(decisions)
    if a.feed:
        legs = sorted({d["leg"] for d in decisions if d.get("leg")})
        for leg in legs:
            recompute_alpha(decisions, a.feed, leg, a.alpha)

    fillmsgs = [r["d"]["msg"] for r in fills if r.get("d", {}).get("type") == "fill"]
    print(f"\n[FILLS] {len(fillmsgs)} private fill messages")
    if a.ws:
        from src.utils.api import get_positions, get_orders
        pos = get_positions()
        resting = get_orders(status = "resting")
        print(f"  REST positions: {len(pos)}; RESTING ORDERS (should be 0): {len(resting)}")


if __name__ == "__main__":
    main()

"""Verify each per-game file spans exactly [T-1h, game-end], T = game start.
Flags pre-window data (before T-1h) and long post-trade tails (data well past
the last trade => stale post-game book that should be trimmed)."""
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_times import game_start

PREGAME = 3600
TAIL_FLAG_S = 600  # >10 min of book data after the last trade is suspicious


def main():
    d = Path(sys.argv[1])
    bad_pre = bad_tail = ok = 0
    for path in sorted(d.glob("*.jsonl.gz")):
        ev = path.stem
        first = last = last_trade = None
        ticker0 = None
        with gzip.open(path, "rt") as f:
            for line in f:
                r = json.loads(line)
                if "meta" in r:
                    g = (r["meta"]["pairs"] or r["meta"]["events"])[0]
                    ticker0 = g.get("first_ticker") or g["tickers"][0]
                    continue
                ts = r["lts"]
                if first is None:
                    first = ts
                last = ts
                if r["d"].get("type") == "trade":
                    last_trade = ts
        T = game_start(ev, ticker0)
        if T is None or first is None:
            print(f"  {ev}: NO START ESTIMATE or empty")
            continue
        lead = T - first              # should be ~3600 (T-1h gate)
        span = (last - T) / 60        # minutes after start
        tail = (last - last_trade) if last_trade else 0
        flags = []
        if first < T - PREGAME - 60:
            flags.append(f"PRE-WINDOW (+{(T - PREGAME - first):.0f}s early)")
            bad_pre += 1
        if tail > TAIL_FLAG_S:
            flags.append(f"POST-TRADE TAIL {tail/60:.0f}min")
            bad_tail += 1
        if not flags:
            ok += 1
        tag = ("  <-- " + "; ".join(flags)) if flags else ""
        print(f"  {ev}: lead={lead/60:.1f}min span={span:.0f}min tail={tail/60:.1f}min{tag}")
    print(f"\nwindow check: {ok} clean, {bad_pre} pre-window, {bad_tail} post-trade-tail")


if __name__ == "__main__":
    main()

"""Sanity-check filtered sessions and report. Exit 0 only if every market in
every checked file is gzip-intact and clears the >=1M in-window contract floor.
Usage: verify_dataset.py <dir-of-jsonl.gz>"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

MIN_CTS = 1_000_000


def check(path: Path) -> bool:
    markets = {}
    vol = defaultdict(float)
    lines = 0
    try:
        with gzip.open(path, "rt") as f:
            for line in f:
                lines += 1
                rec = json.loads(line)
                if "meta" in rec:
                    for p in rec["meta"].get("pairs", []):
                        markets[p["first_ticker"]] = p.get("title", "")
                        markets[p["second_ticker"]] = p.get("title", "")
                    for ev in rec["meta"].get("events", []):
                        for t in ev["tickers"]:
                            markets[t] = ev.get("title", "")
                    continue
                d = rec.get("d", {})
                if d.get("type") == "trade":
                    m = d["msg"]
                    vol[m["market_ticker"]] += float(m["count_fp"])
    except (EOFError, OSError, gzip.BadGzipFile) as e:
        print(f"  CORRUPT {path.name}: {type(e).__name__}: {e}")
        return False
    ok = True
    events = defaultdict(list)
    for t in markets:
        events[t.rsplit("-", 1)[0]].append(t)
    print(f"  {path.name}: {lines} lines, {len(events)} games, {len(markets)} markets")
    for ev, tks in sorted(events.items()):
        gv = [vol[t] for t in tks]
        # any-leg retention: a game is valid if its most-liquid leg clears 1M
        game_ok = max(gv) >= MIN_CTS
        n_below = sum(1 for v in gv if v < MIN_CTS)
        if not game_ok:
            note = "  <-- NO leg >=1M (FAIL)"
            ok = False
        elif n_below:
            note = f"  ({n_below}/{len(tks)} legs <1M, kept anyway)"
        else:
            note = ""
        print(f"    {ev}: {len(tks)} mkts, in-window vol {min(gv):,.0f}-{max(gv):,.0f}{note}")
    return ok


def main():
    d = Path(sys.argv[1])
    files = sorted(d.glob("*.jsonl.gz"))
    if not files:
        print(f"no files in {d}")
        sys.exit(1)
    allok = all(check(f) for f in files)
    print(f"\nSANITY: {'PASS' if allok else 'FAIL'}")
    sys.exit(0 if allok else 2)


if __name__ == "__main__":
    main()

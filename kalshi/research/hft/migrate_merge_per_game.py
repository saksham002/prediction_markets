"""Migrate to ONE FILE PER GAME by MERGING every job that recorded the game.

Each game is often split across jobs (day job has the pregame/early innings, the
evening job has the rest). We splice the sources in time order: take source i's
lines up to where source i+1 starts, then source i+1 (whose subscription snapshot
re-syncs the book in replay). Output spans [T-1h, game-end=last trade], all legs.

Sources: raws where we still have them (sub_1M Jun11, ticks Jun12); Jun10 raws
are gone so we merge the filtered dataset/ copies (day copy carries the pregame).
Non-destructive -> dataset_games/.
"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_times import game_start
try:
    from research.hft.paths import HFT_DATA
except ImportError:
    from paths import HFT_DATA

DATA = HFT_DATA
DST = DATA / "dataset_games"
MIN_CTS = 1_000_000
PREGAME = 3600

# Every source that may hold a piece of a game (raws preferred; Jun10 = filtered)
SOURCES = [
    DATA / "dataset/ticks_20260610_000718_2334504.jsonl.gz",
    DATA / "dataset/ticks_20260610_120013_3956659.jsonl.gz",
    DATA / "dataset/ticks_20260610_183014_2644094.jsonl.gz",
    DATA / "sub_1M/ticks_20260611_074145_3157453.jsonl.gz",
    DATA / "sub_1M/ticks_20260611_193554_1678643.jsonl.gz",
    DATA / "sub_1M/ticks_20260612_063040_274946.jsonl.gz",
    DATA / "ticks/ticks_20260612_183515_2488594.jsonl.gz",
]


def legs_of(g):
    return list(g["tickers"]) if "tickers" in g else [g["first_ticker"], g["second_ticker"]]


def scan(path):
    """-> {ev: {kind, meta, tickers, lines:[(lts,str)], first, last_trade}}."""
    meta_by_ev = {}
    with gzip.open(path, "rt") as f:
        for line in f:
            if '"meta"' not in line[:40]:
                continue
            for p in json.loads(line)["meta"].get("pairs", []):
                meta_by_ev[p["event_ticker"]] = ("pair", p)
            for ev in json.loads(line)["meta"].get("events", []):
                meta_by_ev[ev["event_ticker"]] = ("event", ev)
    tk2ev = {t: ev for ev, (_, g) in meta_by_ev.items() for t in legs_of(g)}
    games = {ev: {"kind": k, "meta": g, "tickers": legs_of(g), "lines": [],
                  "first": None, "last_trade": None}
             for ev, (k, g) in meta_by_ev.items()}
    with gzip.open(path, "rt") as f:
        for line in f:
            if '"meta"' in line[:40]:
                continue
            r = json.loads(line)
            d = r.get("d", {})
            ev = tk2ev.get(d.get("msg", {}).get("market_ticker"))
            if ev is None:
                continue
            ts = r["lts"]
            g = games[ev]
            g["lines"].append((ts, line if line.endswith("\n") else line + "\n"))
            if g["first"] is None:
                g["first"] = ts
            if d.get("type") == "trade":
                g["last_trade"] = ts
    return {ev: g for ev, g in games.items() if g["lines"]}


def main():
    DST.mkdir(parents = True, exist_ok = True)
    # ev -> list of source-segments
    by_game = defaultdict(list)
    meta_of = {}
    for path in SOURCES:
        if not path.exists():
            print(f"  MISSING {path.name}")
            continue
        for ev, g in scan(path).items():
            by_game[ev].append(g)
            meta_of[ev] = (g["kind"], g["meta"], g["tickers"])
        print(f"  scanned {path.name}")

    kept = dropped = 0
    for ev, segs in sorted(by_game.items()):
        kind, meta, tickers = meta_of[ev]
        ticker0 = tickers[0]
        T = game_start(ev, ticker0)
        lo = (T - PREGAME) if T else 0.0
        game_end = max((s["last_trade"] for s in segs if s["last_trade"]), default = float("inf"))
        # any-leg >=1M retention, volume summed across the merged window
        vol = defaultdict(float)
        for s in segs:
            for ts, line in s["lines"]:
                if ts < lo or ts > game_end:
                    continue
                m = json.loads(line).get("d", {})
                if m.get("type") == "trade":
                    mm = m["msg"]
                    vol[mm["market_ticker"]] += float(mm["count_fp"])
        if max((vol.get(t, 0.0) for t in tickers), default = 0.0) < MIN_CTS:
            dropped += 1
            continue

        # splice: order sources by first ts; source i contributes [first_i, first_{i+1})
        segs = sorted(segs, key = lambda s: s["first"])
        starts = [s["first"] for s in segs] + [float("inf")]
        out_lines = []
        for i, s in enumerate(segs):
            cut = starts[i + 1]
            for ts, line in s["lines"]:
                if lo <= ts <= game_end and (i == len(segs) - 1 or ts < cut):
                    out_lines.append((ts, line))
        out_lines.sort(key = lambda x: x[0])
        metablock = {"pairs": [meta], "events": []} if kind == "pair" \
            else {"pairs": [], "events": [meta]}
        with gzip.open(DST / f"{ev}.jsonl.gz", "wt") as f:
            f.write(json.dumps({"lts": lo, "meta": metablock}, separators = (",", ":")) + "\n")
            for ts, line in out_lines:
                f.write(line)
        kept += 1
        nseg = len(segs)
        lead = (T - out_lines[0][0]) / 60 if out_lines and T else 0
        print(f"  {ev}: {nseg} source(s), lead={lead:.0f}min, {len(out_lines)} lines")
    print(f"\nDONE: {kept} per-game files, {dropped} dropped (<1M)")


if __name__ == "__main__":
    main()

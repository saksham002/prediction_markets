"""Migrate the session-file dataset to ONE FILE PER GAME (all legs).

For each unique event, pick the single source session with the best coverage
(both legs present > higher min-leg volume > more lines), then write that game's
meta + all its lines to <event>.jsonl.gz. Deduplicates by construction.

Non-destructive: writes to dataset_games/, leaves dataset/ untouched. Two-pass
(score, then extract) to bound memory. Retention: keep a game iff any leg >=1M.
"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

SRC = Path("/data/user_data/saksham3/kalshi_hft/dataset")
DST = Path("/data/user_data/saksham3/kalshi_hft/dataset_games")
MIN_CTS = 1_000_000


def legs_of(g):
    return list(g["tickers"]) if "tickers" in g else [g["first_ticker"], g["second_ticker"]]


def scan(path):
    """Pass 1: per-event meta + per-leg volume + line count (no line retention)."""
    meta_by_ev, meta_lts = {}, {}
    with gzip.open(path, "rt") as f:
        for line in f:
            if '"meta"' not in line[:40]:
                continue
            r = json.loads(line)
            if "meta" not in r:
                continue
            for p in r["meta"].get("pairs", []):
                meta_by_ev[p["event_ticker"]] = ("pair", p)
                meta_lts[p["event_ticker"]] = r["lts"]
            for ev in r["meta"].get("events", []):
                meta_by_ev[ev["event_ticker"]] = ("event", ev)
                meta_lts[ev["event_ticker"]] = r["lts"]
    tk2ev = {t: ev for ev, (_, g) in meta_by_ev.items() for t in legs_of(g)}
    vol = defaultdict(lambda: defaultdict(float))
    nlines = defaultdict(int)
    last_trade = {}
    with gzip.open(path, "rt") as f:
        for line in f:
            if '"meta"' in line[:40]:
                continue
            r = json.loads(line)
            m = r.get("d", {}).get("msg", {})
            ev = tk2ev.get(m.get("market_ticker"))
            if ev is None:
                continue
            nlines[ev] += 1
            if r["d"].get("type") == "trade":
                vol[ev][m["market_ticker"]] += float(m["count_fp"])
                last_trade[ev] = r["lts"]
    return {ev: {"kind": k, "meta": g, "tickers": legs_of(g), "vol": dict(vol[ev]),
                 "n": nlines[ev], "lts": meta_lts[ev], "end": last_trade.get(ev, float("inf"))}
            for ev, (k, g) in meta_by_ev.items() if nlines[ev] > 0}


def score(c):
    vols = [c["vol"].get(t, 0.0) for t in c["tickers"]]
    return (1 if vols and all(v > 0 for v in vols) else 0, min(vols) if vols else 0, c["n"])


def main():
    DST.mkdir(parents = True, exist_ok = True)
    winner = {}  # ev -> (path, cand)
    for path in sorted(SRC.glob("ticks_*.jsonl.gz")):
        for ev, c in scan(path).items():
            if ev not in winner or score(c) > score(winner[ev][1]):
                winner[ev] = (str(path), c)
    print(f"unique games across sessions: {len(winner)}")

    by_path = defaultdict(dict)
    for ev, (path, c) in winner.items():
        by_path[path][ev] = c

    kept = dropped = 0
    for path, games in sorted(by_path.items()):
        tk2ev, files = {}, {}
        for ev, c in games.items():
            anyleg = max((c["vol"].get(t, 0.0) for t in c["tickers"]), default = 0.0)
            if anyleg < MIN_CTS:
                dropped += 1
                continue
            for t in c["tickers"]:
                tk2ev[t] = ev
            fh = gzip.open(DST / f"{ev}.jsonl.gz", "wt")
            meta = {"pairs": [c["meta"]], "events": []} if c["kind"] == "pair" \
                else {"pairs": [], "events": [c["meta"]]}
            fh.write(json.dumps({"lts": c["lts"], "meta": meta}, separators = (",", ":")) + "\n")
            files[ev] = fh
            kept += 1
        # trim each game at its last trade ("game end"): drop the post-game
        # book-only tail so files span exactly [T-1h, game-end]
        end = {ev: games[ev]["end"] for ev in files}
        with gzip.open(path, "rt") as f:
            for line in f:
                if '"meta"' in line[:40]:
                    continue
                r = json.loads(line)
                ev = tk2ev.get(r.get("d", {}).get("msg", {}).get("market_ticker"))
                if ev in files and r["lts"] <= end[ev]:
                    files[ev].write(line if line.endswith("\n") else line + "\n")
        for fh in files.values():
            fh.close()
        print(f"  {Path(path).name}: wrote {len(files)} game files")
    print(f"\nDONE: {kept} per-game files in {DST}, {dropped} dropped (<1M any leg)")


if __name__ == "__main__":
    main()

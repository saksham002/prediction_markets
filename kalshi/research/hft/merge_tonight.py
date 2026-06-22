"""Reconcile the Jun-13 overlap: the legacy session (job 8320896, ran to 18:32)
holds each early game's pregame; the new per-game recorder (8342316) holds the
rest. Merge per game by splicing sources in time order (snapshot re-syncs the
book at boundaries), trim to game-end, any-leg>=1M. Writes to a STAGING dir;
verify before swapping into dataset/."""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from migrate_merge_per_game import scan  # reuse the source scanner
from game_times import game_start
try:
    from research.hft.paths import HFT_DATA
except ImportError:
    from paths import HFT_DATA

DATA = HFT_DATA
STAGING = DATA / "merge_tonight_staging"
MIN_CTS = 1_000_000
PREGAME = 3600
TAG = "26JUN13"   # only reconcile tonight's games


def main():
    STAGING.mkdir(parents = True, exist_ok = True)
    for old in STAGING.glob("*.jsonl.gz"):
        old.unlink()
    legacy = sorted((DATA / "dataset_incoming").glob("ticks_20260613_063041_*.jsonl.gz"))
    new_files = (sorted((DATA / "dataset").glob(f"*{TAG}*.jsonl.gz"))
                 + sorted((DATA / "sub_1M").glob(f"*{TAG}*.jsonl.gz")))
    sources = legacy + new_files
    print(f"sources: {len(legacy)} legacy session(s) + {len(new_files)} new per-game files")

    by_game = defaultdict(list)
    meta_of = {}
    for path in sources:
        for ev, g in scan(path).items():
            if TAG not in ev:
                continue
            by_game[ev].append(g)
            meta_of[ev] = (g["kind"], g["meta"], g["tickers"])

    kept = dropped = 0
    for ev, segs in sorted(by_game.items()):
        kind, meta, tickers = meta_of[ev]
        T = game_start(ev, tickers[0])
        lo = (T - PREGAME) if T else 0.0
        game_end = max((s["last_trade"] for s in segs if s["last_trade"]), default = float("inf"))
        vol = defaultdict(float)
        for s in segs:
            for ts, line in s["lines"]:
                if lo <= ts <= game_end:
                    m = json.loads(line).get("d", {})
                    if m.get("type") == "trade":
                        vol[m["msg"]["market_ticker"]] += float(m["msg"]["count_fp"])
        if max((vol.get(t, 0.0) for t in tickers), default = 0.0) < MIN_CTS:
            dropped += 1
            continue
        segs = sorted(segs, key = lambda s: s["first"])
        starts = [s["first"] for s in segs] + [float("inf")]
        out_lines = []
        for i, s in enumerate(segs):
            cut = starts[i + 1]
            for ts, line in s["lines"]:
                if lo <= ts <= game_end and (i == len(segs) - 1 or ts < cut):
                    out_lines.append((ts, line))
        out_lines.sort(key = lambda x: x[0])
        block = {"pairs": [meta], "events": []} if kind == "pair" else {"pairs": [], "events": [meta]}
        with gzip.open(STAGING / f"{ev}.jsonl.gz", "wt") as f:
            f.write(json.dumps({"lts": lo, "meta": block}, separators = (",", ":")) + "\n")
            for ts, line in out_lines:
                f.write(line)
        kept += 1
        lead = (T - out_lines[0][0]) / 60 if out_lines and T else 0
        print(f"  {ev}: {len(segs)} src, lead={lead:.0f}min, {len(out_lines)} lines")
    print(f"\nDONE: {kept} merged Jun13 games -> {STAGING}, {dropped} dropped (<1M)")


if __name__ == "__main__":
    main()

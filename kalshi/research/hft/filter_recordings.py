"""
Rewrite tick recordings to the dataset liquidity rules (June 2026):

  1. Keep only markets of the target series (majors + WC + friendlies).
  2. Keep only markets with >= --min-contracts total recorded trade volume.
  3. Keep only messages from game_start - 1h onward for each market
     (game start from ticker HHMM, else API expected_expiration fallback).

Writes <name>.jsonl.gz to --out-dir with filtered content (meta lines have
pairs/events restricted to surviving markets). With --replace, the original
is DELETED after a successful rewrite.
"""

import argparse
import gzip
import json
import sys
import zlib
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.game_times import game_start

TARGET_SERIES = {"KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXNFLGAME",
                 "KXWCGAME", "KXINTLFRIENDLYGAME"}
PREGAME_WINDOW_S = 3600


def iter_lines(path: Path):
    f = gzip.open(path, "rt")
    try:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    except (EOFError, zlib.error):
        return
    finally:
        f.close()


def event_of(ticker: str) -> str:
    return "-".join(ticker.split("-")[:2])


def filter_recording(path: Path, out_dir: Path, min_contracts: float, replace: bool):
    # Pass 1: per-market trade volume WITHIN the T-1h window (volume outside
    # the window must not help a market clear the liquidity floor)
    volume: dict[str, float] = defaultdict(float)
    cutoff_cache: dict[str, float] = {}
    # Recorder re-emits meta on every re-discovery; accumulate pairs AND events
    # across ALL meta lines (the last one alone may omit earlier games).
    pairs_by_ev: dict = {}
    events_by_ev: dict = {}
    for rec in iter_lines(path):
        if "meta" in rec:
            for p in rec["meta"].get("pairs", []):
                pairs_by_ev[p["event_ticker"]] = p
            for ev in rec["meta"].get("events", []):
                events_by_ev[ev["event_ticker"]] = ev
            continue
        d = rec.get("d", {})
        if d.get("type") != "trade":
            continue
        msg = d["msg"]
        ticker = msg["market_ticker"]
        if ticker.split("-", 1)[0] not in TARGET_SERIES:
            continue
        if ticker not in cutoff_cache:
            start = game_start(event_of(ticker), ticker)
            # No start estimate -> keep all data rather than guess wrong
            cutoff_cache[ticker] = (start - PREGAME_WINDOW_S) if start else 0.0
        if rec["lts"] >= cutoff_cache[ticker]:
            volume[ticker] += float(msg["count_fp"])

    qual: set[str] = {t for t, v in volume.items() if v >= min_contracts}
    # Retention rule: keep a game if ANY of its legs clears the floor, and keep
    # ALL of that game's legs (a complete book). An illiquid leg stays in the
    # data but won't be quoted in sim — its wide spread fails the max_spread
    # gate. Generalizes across leagues (2-leg pairs, N-leg soccer events).
    keep: set[str] = set()
    for p in pairs_by_ev.values():
        legs = (p["first_ticker"], p["second_ticker"])
        if any(t in qual for t in legs):
            keep.update(legs)
    for ev in events_by_ev.values():
        legs = ev["tickers"]
        if legs and any(t in qual for t in legs):
            keep.update(legs)
    # A retained leg may have <floor (even zero) trades, so fall back to its own
    # game_start window when it never appeared in the trade-volume pass.
    start_cutoff = {}
    for t in keep:
        if t in cutoff_cache:
            start_cutoff[t] = cutoff_cache[t]
        else:
            s = game_start(event_of(t), t)
            start_cutoff[t] = (s - PREGAME_WINDOW_S) if s else 0.0

    out_path = out_dir / path.name
    tmp_path = out_dir / (path.name + ".tmp")
    kept_lines = total_lines = 0
    with gzip.open(tmp_path, "wt") as out:
        for rec in iter_lines(path):
            total_lines += 1
            if "meta" in rec:
                meta = rec["meta"]
                # All-or-nothing per game (matches the retention rule above)
                pairs = [p for p in meta.get("pairs", [])
                         if p["first_ticker"] in keep and p["second_ticker"] in keep]
                events = [ev for ev in meta.get("events", [])
                          if ev["tickers"] and all(t in keep for t in ev["tickers"])]
                rec["meta"] = {**meta, "pairs": pairs, "events": events}
                out.write(json.dumps(rec, separators = (",", ":")) + "\n")
                kept_lines += 1
                continue
            d = rec.get("d", {})
            msg = d.get("msg", {})
            ticker = msg.get("market_ticker")
            if ticker is None:
                continue  # subscribe acks etc.
            if ticker not in keep or rec["lts"] < start_cutoff[ticker]:
                continue
            out.write(json.dumps(rec, separators = (",", ":")) + "\n")
            kept_lines += 1
    tmp_path.rename(out_path)

    survivors = sorted(keep)
    print(f"{path.name}: kept {len(survivors)}/{len(volume)} markets, "
          f"{kept_lines}/{total_lines} lines -> {out_path.name}")
    for t in survivors:
        print(f"    {t}  {volume[t]:>12.0f} cts")
    if replace and out_path.resolve() != path.resolve():
        # Keep raw data: park originals in sub_1M/ instead of deleting
        park_dir = path.parent.parent / "sub_1M"
        park_dir.mkdir(parents = True, exist_ok = True)
        path.rename(park_dir / path.name)
        print(f"    moved original to {park_dir / path.name}")


def main():
    parser = argparse.ArgumentParser(description = "Filter recordings to dataset liquidity rules")
    parser.add_argument("recordings", nargs = "+")
    parser.add_argument("--out-dir", required = True)
    parser.add_argument("--min-contracts", type = float, default = 1_000_000)
    parser.add_argument("--replace", action = "store_true", help = "Delete originals after rewrite")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents = True, exist_ok = True)
    for rec_path in args.recordings:
        filter_recording(Path(rec_path), out_dir, args.min_contracts, args.replace)


if __name__ == "__main__":
    main()

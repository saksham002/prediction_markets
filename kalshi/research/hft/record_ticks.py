"""
Raw tick recorder for Kalshi paired sports markets.

Subscribes to orderbook_delta + trade channels for the top N paired sports
events by 24h volume and appends every raw WS message to a gzipped JSONL
file, one line per message:

  {"lts": <local epoch seconds>, "d": <raw ws message>}

Pair metadata (event/ticker mapping, fee types, close times) is written as a
meta line at each (re)subscription:

  {"lts": ..., "meta": {"pairs": [...]}}

Pairs are re-ranked every --rerank-interval hours (reconnect + fresh
snapshots). Recordings are replayed offline for alpha studies and sims.
"""

import argparse
import asyncio
import gzip
import json
import os
import signal
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path

import requests
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.discovery import discover_league_events
from research.hft.game_times import game_start
from src.utils.api import WS_URL, fetch_series_fee, paginate, ws_auth_headers
from src.utils.timestamp import Timestamp

from research.hft.paths import HFT_DATA as DATA_ROOT
STAGING_DIR = DATA_ROOT / "games_recording"   # in-progress per-game files
DATASET_DIR = DATA_ROOT / "dataset"           # finalized games (>=1M any leg)
SUB_DIR = DATA_ROOT / "sub_1M"                 # sub-threshold games (preserved)
FLUSH_INTERVAL_S = 30
MIN_CONTRACTS = 1_000_000


def discover_extra_events(n: int, series_csv: str) -> list[dict]:
    """Top-N open events (by 24h volume) of N-market series like soccer
    (win/draw/win), which the 2-market pair discovery skips."""
    series_set = {s.strip() for s in series_csv.split(",") if s.strip()}
    events = paginate(
        "events",
        params = {"status": "open", "with_nested_markets": True},
        key = "events",
        max_per_page = 200,
    )
    out = []
    for ev in events:
        event_ticker = ev["event_ticker"]
        series = event_ticker.split("-", 1)[0]
        if series not in series_set:
            continue
        markets = ev.get("markets", [])
        if not 2 <= len(markets) <= 8:
            continue
        volume = sum(float(m.get("volume_24h_fp", 0) or 0) for m in markets)
        out.append({
            "event_ticker": event_ticker,
            "title": ev.get("title", event_ticker),
            "series": series,
            "tickers": [m["ticker"] for m in markets],
            "volume": volume,
        })
    out.sort(key = lambda e: -e["volume"])
    top = out[:n]
    for e in top:
        print(f"  [extra] {e['event_ticker']} vol={e['volume']:.0f} {e['title'][:50]}")
    return top


def liquidity_window_gate(items: list[dict], first_ticker_of, now: float,
                          min_volume: float, lookahead_s: float) -> list[dict]:
    """Dataset rules: 24h volume >= min_volume AND game starts within
    lookahead (so the T-1h window opens before the next re-rank)."""
    kept = []
    for item in items:
        if item["volume"] < min_volume:
            continue
        start = game_start(item["event_ticker"], first_ticker_of(item))
        if start is not None and now < start - 3600 - lookahead_s:
            continue
        kept.append(item)
    return kept


class GameWriter:
    """Routes each in-window message to a per-game file <event>.jsonl.gz under
    STAGING_DIR (all legs, one file per game). Writes the game's meta (with fee
    info) once on first message. Persists across re-ranks within a job."""

    def __init__(self, out_dir: Path):
        self.dir = out_dir
        self.dir.mkdir(parents = True, exist_ok = True)
        self.files: dict[str, "gzip.GzipFile"] = {}
        self.cutoff: dict[str, float] = {}   # ticker -> T-1h
        self.tk2ev: dict[str, str] = {}      # ticker -> event
        self.meta: dict[str, tuple] = {}     # event -> (kind, meta dict)
        self._fee_cache: dict[str, tuple] = {}
        self._last_flush = time.time()
        self._n = 0

    def _annotate_fee(self, series: str, d: dict):
        if "fee_type" not in d:
            if series not in self._fee_cache:
                self._fee_cache[series] = fetch_series_fee(series)
            d["series"] = series
            d["fee_multiplier"], d["fee_type"] = self._fee_cache[series]

    def register(self, pairs: list[dict], events: list[dict]):
        """Add any newly-discovered games (T-1h cutoff, ticker map, fee meta)."""
        for p in pairs:
            ev = p["event_ticker"]
            if ev in self.meta:
                continue
            self._annotate_fee(p["first_ticker"].split("-", 1)[0], p)
            start = game_start(ev, p["first_ticker"])
            cut = (start - 3600) if start else 0.0
            for tkr in (p["first_ticker"], p["second_ticker"]):
                self.cutoff[tkr] = cut
                self.tk2ev[tkr] = ev
            self.meta[ev] = ("pair", p)
        for e in events:
            ev = e["event_ticker"]
            if ev in self.meta:
                continue
            self._annotate_fee(e["series"], e)
            start = game_start(ev, e["tickers"][0])
            cut = (start - 3600) if start else 0.0
            for tkr in e["tickers"]:
                self.cutoff[tkr] = cut
                self.tk2ev[tkr] = ev
            self.meta[ev] = ("event", e)

    @property
    def all_tickers(self) -> list[str]:
        return list(self.tk2ev.keys())

    def write(self, lts: float, data: dict):
        ticker = data.get("msg", {}).get("market_ticker")
        if ticker is None or lts < self.cutoff.get(ticker, 0.0):
            return  # not ours, or before this market's T-1h window
        ev = self.tk2ev.get(ticker)
        if ev is None:
            return
        fh = self.files.get(ev)
        if fh is None:
            fh = gzip.open(self.dir / f"{ev}.jsonl.gz", "at")
            kind, m = self.meta[ev]
            block = {"pairs": [m], "events": []} if kind == "pair" else {"pairs": [], "events": [m]}
            fh.write(json.dumps({"lts": lts, "meta": block}, separators = (",", ":")) + "\n")
            self.files[ev] = fh
        fh.write(json.dumps({"lts": lts, "d": data}, separators = (",", ":")) + "\n")
        self._n += 1
        now = time.time()
        if now - self._last_flush >= FLUSH_INTERVAL_S:
            self.flush()
            self._last_flush = now
        if self._n % 50000 == 0:
            print(f"  {self._n} msgs across {len(self.files)} game files")

    def flush(self):
        for h in self.files.values():
            h.flush()

    def close(self):
        for h in self.files.values():
            h.close()
        self.files.clear()


class TickRecorder:
    """One WS subscription cycle over the writer's current tickers."""

    def __init__(self, writer: GameWriter):
        self.writer = writer
        self.all_tickers = writer.all_tickers

    async def run(self):
        if not self.all_tickers:
            await asyncio.sleep(60)
            return
        print(f"Recording {len(self.writer.meta)} games, {len(self.all_tickers)} markets")
        while True:
            headers = ws_auth_headers()
            try:
                async with websockets.connect(WS_URL, additional_headers = headers) as ws:
                    for cid, ch in ((1, "orderbook_delta"), (2, "trade")):
                        await ws.send(json.dumps({"id": cid, "cmd": "subscribe", "params": {
                            "channels": [ch], "market_tickers": self.all_tickers}}))
                    # private fill channel (account-scoped, no market filter; the read
                    # key can subscribe) so recordings carry which trades/orders are
                    # OURS during live trading — match public trade_id to our fill's
                    # trade_id, and client_order_id tags our own orderbook_deltas.
                    await ws.send(json.dumps({"id": 3, "cmd": "subscribe",
                                              "params": {"channels": ["fill"]}}))
                    print(f"Subscribed to {len(self.all_tickers)} tickers + private fill")
                    async for raw in ws:
                        self.writer.write(time.time(), json.loads(raw))
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                print(f"Connection error ({type(e).__name__}: {e}), reconnecting in 5s...")
                self.writer.flush()
                await asyncio.sleep(5)
                continue


async def main():
    parser = argparse.ArgumentParser(description = "Raw tick recorder for top Kalshi paired sports markets")
    parser.add_argument("-n", "--top-n", type = int, default = 12, help = "Number of top sports pairs")
    parser.add_argument("-c", "--category", type = str, default = "Sports", help = "Event category")
    parser.add_argument("--series", type = str, default = None,
                        help = "Comma-separated series filter (e.g. KXMLBGAME,KXNBAGAME,KXNHLGAME,KXNFLGAME)")
    parser.add_argument("--extra-series", type = str, default = None,
                        help = "Comma-separated N-market series to record too (e.g. KXWCGAME,KXINTLFRIENDLYGAME)")
    parser.add_argument("--extra-top-n", type = int, default = 8,
                        help = "Number of extra-series events to record")
    parser.add_argument("--min-volume", type = float, default = 25_000,
                        help = "Min 24h volume (contracts) at discovery — dead-market filter; "
                               "the strict >=1M dataset rule is applied by filter_recordings.py")
    parser.add_argument("-r", "--rerank-interval", type = float, default = 2, help = "Re-rank pairs every this many hours")
    parser.add_argument("-d", "--duration", type = float, default = None, help = "Run for this many hours then exit cleanly")
    args = parser.parse_args()

    writer = GameWriter(STAGING_DIR)
    print(f"Recording per-game files to {STAGING_DIR}")

    rerank_s = args.rerank_interval * 3600
    duration_s = args.duration * 3600 if args.duration is not None else None
    start_epoch = time.time()

    try:
        while True:
            elapsed = time.time() - start_epoch
            if duration_s is not None and elapsed >= duration_s:
                break

            while True:
                try:
                    # League membership is the ONLY interest criterion; market
                    # count decides pair vs per-market handling (no title parsing)
                    all_series = ",".join(filter(None, [args.series, args.extra_series]))
                    pairs, extra_events = discover_league_events(all_series, args.top_n)
                    # Dataset rules: liquidity floor + only from game_start - 1h
                    # (lookahead = one re-rank so the window opens in time)
                    now = time.time()
                    pairs = liquidity_window_gate(
                        pairs, lambda p: p["first_ticker"], now, args.min_volume, rerank_s)
                    extra_events = liquidity_window_gate(
                        extra_events, lambda e: e["tickers"][0], now, args.min_volume, rerank_s)
                    print(f"  after liquidity/window gate: {len(pairs)} pairs, {len(extra_events)} events")
                    break
                except requests.exceptions.RequestException as e:
                    print(f"discovery failed ({type(e).__name__}: {e}), retrying in 60s...")
                    await asyncio.sleep(60)
            if not pairs and not extra_events:
                print("No matching events found, retrying in 10m...")
                await asyncio.sleep(600)
                continue

            writer.register(pairs, extra_events)
            recorder = TickRecorder(writer)
            this_iter_timeout = rerank_s
            if duration_s is not None:
                this_iter_timeout = min(rerank_s, duration_s - elapsed)
            try:
                await asyncio.wait_for(recorder.run(), timeout = this_iter_timeout)
            except asyncio.TimeoutError:
                if duration_s is not None and (time.time() - start_epoch) >= duration_s:
                    print(f"\n--- Duration {args.duration}h elapsed, exiting ---\n")
                    break
                print(f"\n--- Re-ranking top {args.top_n} pairs after {args.rerank_interval}h ---\n")
    except KeyboardInterrupt:
        print("\nStopping recorder...")
    finally:
        writer.close()
        # Finalize: trim each game at its last trade (game-end) and apply the
        # any-leg >=1M retention. Qualifying -> dataset/, sub-threshold -> sub_1M/.
        try:
            finalize_games(STAGING_DIR, DATASET_DIR, SUB_DIR)
        except Exception as e:
            print(f"finalize failed (staged files kept in {STAGING_DIR}): {e}")


def finalize_games(staging: Path, dataset: Path, sub: Path, min_cts: float = MIN_CONTRACTS):
    """Trim each staged per-game file at its last trade (game-end) and route by
    the any-leg >=1M retention rule. Sources removed only after a successful
    rewrite. Generalizes across leagues (2-leg pairs, N-leg events)."""
    dataset.mkdir(parents = True, exist_ok = True)
    sub.mkdir(parents = True, exist_ok = True)
    corrupt = DATA_ROOT / "games_corrupt"
    for path in sorted(staging.glob("*.jsonl.gz")):
        meta_line, legs, last_trade, rows = None, [], None, []
        vol: dict[str, float] = defaultdict(float)
        try:
            with gzip.open(path, "rt") as f:
                for line in f:
                    r = json.loads(line)
                    if "meta" in r:
                        meta_line = line if line.endswith("\n") else line + "\n"
                        g = (r["meta"]["pairs"] or r["meta"]["events"])[0]
                        legs = list(g["tickers"]) if "tickers" in g else [g["first_ticker"], g["second_ticker"]]
                        continue
                    rows.append((r["lts"], line if line.endswith("\n") else line + "\n"))
                    m = r["d"].get("msg", {})
                    if r["d"].get("type") == "trade":
                        vol[m["market_ticker"]] += float(m["count_fp"])
                        last_trade = r["lts"]
        except (EOFError, OSError, zlib.error, json.JSONDecodeError) as e:
            # Truncated/interleaved gzip (crashed or duplicate writer): quarantine
            # this one file and keep going. A single unreadable game must NOT abort
            # the whole finalize — it used to, stranding every game in staging.
            corrupt.mkdir(parents = True, exist_ok = True)
            dst = corrupt / path.name
            if dst.exists():
                dst = corrupt / f"{path.stem}.{int(time.time())}.gz"
            path.rename(dst)
            print(f"  CORRUPT {path.name}: {type(e).__name__}: {e} -> {corrupt.name}/")
            continue
        keep = max((vol.get(t, 0.0) for t in legs), default = 0.0) >= min_cts
        dst = (dataset if keep else sub) / path.name
        if dst.exists():
            # Game already finalized by another job (should not happen with the
            # 3am schedule). Preserve both for manual merge instead of clobbering.
            dup = DATA_ROOT / "games_dup"
            dup.mkdir(parents = True, exist_ok = True)
            dst = dup / path.name
            print(f"  WARNING: {path.name} already finalized — routing to games_dup/ (game spanned jobs)")
        with gzip.open(dst, "wt") as out:
            if meta_line:
                out.write(meta_line)
            for lts, line in rows:
                if last_trade is None or lts <= last_trade:
                    out.write(line)
        path.unlink()
        maxv = max((vol.get(t, 0.0) for t in legs), default = 0.0)
        print(f"  {path.name}: {'KEPT' if keep else 'sub-1M'} -> {dst.parent.name}/ ({maxv:.0f} max-leg cts)")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
    asyncio.run(main())

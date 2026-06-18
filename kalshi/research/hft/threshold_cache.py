"""
Percentile-threshold cache keyed by the IN-SAMPLE GAME SET.

Percentiles are ALWAYS computed on exactly the games passed in (the sweep's
in-sample) — never a hardcoded list. On (re)computation a cache entry is written to
CACHE_DIR holding (a) the per-alpha percentiles and (b) the list of games used.
`get_thresholds` first searches the cache for an entry whose game-set == the
requested in-sample set (and which already has the requested alphas/pcts); only on
a miss does it recompute. So thresholds and the sweep can never drift, and an
unchanged in-sample is never recomputed.
"""
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import SingleAlphaEngine
from research.hft.replay import Replayer

CACHE_DIR = Path("/data/user_data/saksham3/kalshi_hft/studies/threshold_cache")


def _stems(games):
    return sorted(Path(g).stem.replace(".jsonl", "") for g in games)


class _ThreshConsumer:
    """Replay one game, sampling |alpha| at a 1Hz grid for the requested alphas."""
    def __init__(self, replayer, alphas):
        self.replayer = replayer
        self.alphas = alphas
        self.engines = {}
        self.vals = defaultdict(list)
        self.last = {}

    def on_meta(self, lts, meta):
        for ev in meta.get("events", []):
            if ev["series"] == "KXWCGAME":
                for t in ev["tickers"]:
                    self.engines[t] = SingleAlphaEngine(t, self.replayer.books, track_obi_ma = True)

    def on_trade(self, lts, msg):
        e = self.engines.get(msg["market_ticker"])
        if e:
            e.on_trade(lts, msg)

    def on_book(self, lts, ticker, delta):
        e = self.engines.get(ticker)
        if not e:
            return
        if delta is not None:
            e.on_delta(lts, ticker, delta)
        e.on_book(lts, ticker)
        if lts - self.last.get(ticker, 0.0) >= 1.0:
            self.last[ticker] = lts
            for a in self.alphas:
                v = e.value_of(a, lts)
                if v is not None:
                    self.vals[a].append(abs(v))


def _compute(games, alphas, pcts):
    vals = defaultdict(list)
    for g in games:
        r = Replayer(g)
        c = _ThreshConsumer(r, alphas)
        r.run(c)
        for a in alphas:
            vals[a] += c.vals[a]
    out = {}
    for a in alphas:
        x = np.array(vals[a]) if vals[a] else np.array([0.0])
        out[a] = {str(p): round(float(np.percentile(x, p)), 6) for p in pcts}
    return out


def _find(stems, alphas, pcts):
    """Return a cache entry's path + data if its game-set matches and it already
    has every requested (alpha, pct); else (None, None)."""
    if not CACHE_DIR.exists():
        return None, None
    target = set(stems)
    for f in sorted(CACHE_DIR.glob("thr_*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if set(d.get("games", [])) != target:
            continue
        if all(a in d.get("alphas", {}) and all(str(p) in d["alphas"][a] for p in pcts)
               for a in alphas):
            return f, d
    return None, None


def get_thresholds(games, alphas, pcts):
    """Percentiles {pcts} of |alpha| for each alpha, computed on EXACTLY `games`
    (the in-sample). Cached by game-set. Returns {alpha: {str(pct): value}}."""
    stems = _stems(games)
    CACHE_DIR.mkdir(parents = True, exist_ok = True)
    path, data = _find(stems, alphas, pcts)
    if data is not None:
        print(f"threshold cache HIT ({path.name}, {len(stems)} games)")
        return {a: data["alphas"][a] for a in alphas}
    print(f"threshold cache MISS -> computing on {len(stems)} in-sample games")
    computed = _compute(games, alphas, pcts)
    key = hashlib.md5("\n".join(stems).encode()).hexdigest()[:12]
    out = CACHE_DIR / f"thr_{key}.json"
    # merge with any existing same-game-set entry (accumulate alphas across calls)
    merged = {}
    if out.exists():
        try:
            merged = json.load(open(out)).get("alphas", {})
        except Exception:
            merged = {}
    merged.update(computed)
    payload = {"games": stems, "pcts": sorted({int(p) for d in merged.values() for p in
               [int(k) for k in d]} | set(pcts)), "alphas": merged}
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent = 1))
    os.replace(tmp, out)                       # atomic -> safe under concurrent shards
    print(f"wrote {out}")
    return {a: computed[a] for a in alphas}

"""Compute |alpha| percentiles {50,75,90,95,99} on the FIRST 4 WC games
(in-sample) for obi / agg_300s / tfma_pw_300s / agree_agg, sampled at the
1Hz grid. Written to studies/wc_thresholds.json for the WC sweep."""
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import SingleAlphaEngine
from research.hft.replay import Replayer

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
OUT = Path("/data/user_data/saksham3/kalshi_hft/studies/wc_thresholds.json")
ALPHAS = ["obi", "agg_300s", "tfma_pw_300s", "agree_agg"]
PCTS = [50, 75, 90, 95, 99]


class ThreshConsumer:
    def __init__(self, replayer):
        self.replayer = replayer
        self.engines = {}
        self.vals = defaultdict(list)
        self.last = {}

    def on_meta(self, lts, meta):
        for ev in meta.get("events", []):
            if ev["series"] == "KXWCGAME":
                for t in ev["tickers"]:
                    self.engines[t] = SingleAlphaEngine(
                        t, self.replayer.books, track_obi_ma = True, track_agg = True)

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
            for a in ALPHAS:
                v = e.value_of(a, lts)
                if v is not None:
                    self.vals[a].append(abs(v))


def main():
    games = sorted(DATASET.glob("KXWCGAME*.jsonl.gz"))[:4]
    print("in-sample games:", [g.stem.replace(".jsonl", "") for g in games])
    allvals = defaultdict(list)
    for g in games:
        r = Replayer(g)
        c = ThreshConsumer(r)
        r.run(c)
        for a in ALPHAS:
            allvals[a] += c.vals[a]
    thr = {}
    for a in ALPHAS:
        x = np.array(allvals[a]) if allvals[a] else np.array([0.0])
        thr[a] = {str(p): round(float(np.percentile(x, p)), 6) for p in PCTS}
        print(f"  {a}: n={len(x)}  " + "  ".join(f"p{p}={thr[a][str(p)]:g}" for p in PCTS))
    OUT.parent.mkdir(parents = True, exist_ok = True)
    OUT.write_text(json.dumps(thr, indent = 1))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

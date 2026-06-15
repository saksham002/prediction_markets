"""
Alpha verification study on a raw tick recording.

Replays a ticks_*.jsonl.gz file, computes all PairAlphaEngine alphas on every
event (throttled to one sample per pair per --throttle seconds), and measures
correlation between alpha[t] and forward pair-mid moves at multiple horizons.

Outputs (to /data/user_data/saksham3/kalshi_hft/studies/<rec_stem>/):
  samples.csv   one row per (pair, sample time) with all alpha values
  corr.csv      pooled alpha-vs-forward-return stats per (alpha, horizon)
  corr_by_pair.csv  same but per event pair (n >= 100 only)

Note: samples overlap in time, so p-values are optimistic; use them for
ranking alphas, not as rigorous significance tests.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.alphas import PairAlphaEngine, SingleAlphaEngine
from research.hft.replay import Replayer

OUTPUT_BASE = Path("/data/user_data/saksham3/kalshi_hft/studies")
HORIZONS_S = [1, 5, 10, 30, 60, 300]


class StudyConsumer:
    def __init__(self, replayer: Replayer, throttle_s: float):
        self.replayer = replayer
        self.throttle_s = throttle_s
        self.engines: dict[str, PairAlphaEngine] = {}
        self.pair_by_ticker: dict[str, str] = {}
        # Unthrottled mid history per event (for exact forward returns)
        self.mid_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
        # Throttled samples per event
        self.samples: dict[str, list] = defaultdict(list)
        self._last_sample_ts: dict[str, float] = {}
        self.alpha_names = PairAlphaEngine.alpha_names()

    def on_meta(self, lts: float, meta: dict):
        for pair in meta.get("pairs", []) if isinstance(meta, dict) else meta:
            event_ticker = pair["event_ticker"]
            if event_ticker not in self.engines:
                self.engines[event_ticker] = PairAlphaEngine(
                    pair, self.replayer.books, track_obi_ma = True, track_agg = True)
            self.pair_by_ticker[pair["first_ticker"]] = event_ticker
            self.pair_by_ticker[pair["second_ticker"]] = event_ticker
        # Multi-market events (soccer): one single-market engine per market;
        # sample keys "EVENT:TICKER" (split assignment hashes the EVENT part)
        if isinstance(meta, dict):
            for ev in meta.get("events", []):
                for tkr in ev["tickers"]:
                    key = f"{ev['event_ticker']}:{tkr}"
                    if key not in self.engines:
                        self.engines[key] = SingleAlphaEngine(
                            tkr, self.replayer.books, track_obi_ma = True, track_agg = True)
                    self.pair_by_ticker[tkr] = key

    def _record_mid(self, lts: float, event_ticker: str):
        engine = self.engines[event_ticker]
        mid = engine.pair_mid()
        if mid is None:
            return
        hist = self.mid_history[event_ticker]
        if hist and hist[-1][1] == mid:
            return
        hist.append((lts, mid))

    def _maybe_sample(self, lts: float, event_ticker: str):
        last = self._last_sample_ts.get(event_ticker)
        if last is not None and lts - last < self.throttle_s:
            return
        engine = self.engines[event_ticker]
        mid = engine.pair_mid()
        if mid is None:
            return
        first_top = self.replayer.top(engine.first_ticker)
        second_top = self.replayer.top(engine.second_ticker)
        alphas = engine.values(now = lts)
        self._last_sample_ts[event_ticker] = lts
        self.samples[event_ticker].append(
            (lts, mid, first_top.spread, second_top.spread,
             [alphas[name] for name in self.alpha_names])
        )

    def on_book(self, lts: float, ticker: str, delta_msg):
        event_ticker = self.pair_by_ticker.get(ticker)
        if event_ticker is None:
            return
        if delta_msg is not None:
            self.engines[event_ticker].on_delta(lts, ticker, delta_msg)
        self.engines[event_ticker].on_book(lts, ticker)
        self._record_mid(lts, event_ticker)
        self._maybe_sample(lts, event_ticker)

    def on_trade(self, lts: float, msg: dict):
        event_ticker = self.pair_by_ticker.get(msg["market_ticker"])
        if event_ticker is None:
            return
        self.engines[event_ticker].on_trade(lts, msg)
        self._record_mid(lts, event_ticker)
        self._maybe_sample(lts, event_ticker)


def forward_returns(samples_ts, samples_mid, hist_ts, hist_mid, horizon_s):
    """Forward mid change (cents) at t+h using last-known mid <= t+h. NaN where data ends."""
    targets = samples_ts + horizon_s
    idx = np.searchsorted(hist_ts, targets, side = "right") - 1
    valid = (idx >= 0) & (targets <= hist_ts[-1])
    out = np.full(len(samples_ts), np.nan)
    out[valid] = (hist_mid[idx[valid]] - samples_mid[valid]) * 100.0
    return out


def main():
    parser = argparse.ArgumentParser(description = "Alpha verification study on a tick recording")
    parser.add_argument("recording", help = "Path to ticks_*.jsonl.gz")
    parser.add_argument("--throttle", type = float, default = 1.0, help = "Min seconds between samples per pair")
    args = parser.parse_args()

    rec_path = Path(args.recording)
    out_dir = OUTPUT_BASE / rec_path.name.replace(".jsonl.gz", "")
    out_dir.mkdir(parents = True, exist_ok = True)

    replayer = Replayer(rec_path)
    consumer = StudyConsumer(replayer, args.throttle)
    print(f"Replaying {rec_path}...")
    n = replayer.run(consumer)
    print(f"  {n} messages, {len(consumer.engines)} pairs, "
          f"{sum(len(s) for s in consumer.samples.values())} samples")

    alpha_names = consumer.alpha_names

    # samples.csv
    with open(out_dir / "samples.csv", "w", newline = "") as f:
        w = csv.writer(f)
        w.writerow(["lts", "event_ticker", "pair_mid", "first_spread", "second_spread"] + alpha_names)
        for event_ticker, rows in consumer.samples.items():
            for lts, mid, s1, s2, alphas in rows:
                w.writerow([f"{lts:.3f}", event_ticker, f"{mid:.4f}",
                            s1 if s1 is not None else "", s2 if s2 is not None else ""]
                           + [f"{a:.6f}" if a is not None else "" for a in alphas])

    # Pooled and per-pair forward-return stats
    pooled: dict[tuple[str, int], list[tuple[np.ndarray, np.ndarray]]] = defaultdict(list)
    by_pair_rows = []
    for event_ticker, rows in consumer.samples.items():
        hist = consumer.mid_history[event_ticker]
        if len(rows) < 10 or len(hist) < 2:
            continue
        hist_ts = np.array([h[0] for h in hist])
        hist_mid = np.array([h[1] for h in hist])
        s_ts = np.array([r[0] for r in rows])
        s_mid = np.array([r[1] for r in rows])
        alpha_matrix = np.array(
            [[r[4][i] if r[4][i] is not None else np.nan for i in range(len(alpha_names))] for r in rows]
        )
        for horizon in HORIZONS_S:
            fwd = forward_returns(s_ts, s_mid, hist_ts, hist_mid, horizon)
            for i, name in enumerate(alpha_names):
                a = alpha_matrix[:, i]
                mask = ~np.isnan(a) & ~np.isnan(fwd)
                if mask.sum() < 2:
                    continue
                pooled[(name, horizon)].append((a[mask], fwd[mask]))
                if mask.sum() >= 100:
                    r, p = pearsonr(a[mask], fwd[mask])
                    by_pair_rows.append([name, horizon, event_ticker, int(mask.sum()), f"{r:.4f}", f"{p:.3g}"])

    corr_rows = []
    for (name, horizon), chunks in sorted(pooled.items()):
        a = np.concatenate([c[0] for c in chunks])
        fwd = np.concatenate([c[1] for c in chunks])
        if len(a) < 30 or a.std() == 0 or fwd.std() == 0:
            continue
        r, p = pearsonr(a, fwd)
        lo_edge, hi_edge = np.quantile(a, [0.1, 0.9])
        top_mean = fwd[a >= hi_edge].mean() if (a >= hi_edge).any() else np.nan
        bot_mean = fwd[a <= lo_edge].mean() if (a <= lo_edge).any() else np.nan
        corr_rows.append([name, horizon, len(a), f"{r:.4f}", f"{p:.3g}",
                          f"{top_mean:.4f}", f"{bot_mean:.4f}", f"{top_mean - bot_mean:.4f}"])

    with open(out_dir / "corr.csv", "w", newline = "") as f:
        w = csv.writer(f)
        w.writerow(["alpha", "horizon_s", "n", "pearson_r", "p_value",
                    "fwd_ret_top_decile_cents", "fwd_ret_bottom_decile_cents", "decile_spread_cents"])
        w.writerows(corr_rows)

    with open(out_dir / "corr_by_pair.csv", "w", newline = "") as f:
        w = csv.writer(f)
        w.writerow(["alpha", "horizon_s", "event_ticker", "n", "pearson_r", "p_value"])
        w.writerows(by_pair_rows)

    print(f"\nWrote {out_dir}/samples.csv, corr.csv, corr_by_pair.csv")
    print("\nTop 20 (alpha, horizon) by |decile spread|:")
    print(f"{'alpha':<16} {'h(s)':>5} {'n':>8} {'r':>8} {'p':>10} {'top10%':>8} {'bot10%':>8} {'spread':>8}")
    ranked = sorted(corr_rows, key = lambda row: -abs(float(row[7])))[:20]
    for row in ranked:
        print(f"{row[0]:<16} {row[1]:>5} {row[2]:>8} {row[3]:>8} {row[4]:>10} {row[5]:>8} {row[6]:>8} {row[7]:>8}")


if __name__ == "__main__":
    main()

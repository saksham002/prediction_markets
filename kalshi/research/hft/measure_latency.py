"""
1) Verify exchange timestamp semantics from a recording: distribution of
   (local receive time - exchange ts_ms) for trade and orderbook_delta
   messages. Confirms ts_ms is Unix epoch ms and quantifies receive offset
   (one-way backward latency + clock offset).

2) Measure live round-trip latency to Kalshi from this node:
   - REST: GET /exchange/status timed via requests, N samples
   - WebSocket: protocol-level ping/pong RTT on the authenticated trade-api
     socket, N samples

forward_delay estimate = median(RTT) / 2 (one-way, clocks assumed in sync).
"""

import argparse
import asyncio
import gzip
import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import requests
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.utils.api import BASE_URL, WS_URL, ws_auth_headers


def recording_offsets(path: str):
    diffs = {"trade": [], "orderbook_delta": []}
    f = gzip.open(path, "rt")
    try:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            d = rec.get("d", {})
            t = d.get("type")
            if t not in diffs:
                continue
            msg = d["msg"]
            if "ts_ms" not in msg:
                continue
            diffs[t].append(rec["lts"] - msg["ts_ms"] / 1000.0)
    except (EOFError, zlib.error):
        pass

    print("=== exchange ts vs local receive time (lts - ts_ms/1000, seconds) ===")
    for t, vals in diffs.items():
        if not vals:
            print(f"  {t}: no samples")
            continue
        a = np.array(vals)
        print(f"  {t:<16} n={len(a):>7}  median={np.median(a):+.4f}  p5={np.percentile(a,5):+.4f}  "
              f"p95={np.percentile(a,95):+.4f}  min={a.min():+.4f}  max={a.max():+.4f}")
    print("  (positive = message received after exchange stamp; min ~ lower bound on one-way latency + clock offset)")


def rest_rtt(n: int):
    url = f"{BASE_URL}/exchange/status"
    session = requests.Session()
    session.get(url, timeout = 10)  # warm up connection (TLS handshake excluded from samples)
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        resp = session.get(url, timeout = 10)
        dt = time.perf_counter() - t0
        if resp.status_code == 200:
            samples.append(dt)
        time.sleep(0.2)
    a = np.array(samples)
    print(f"=== REST RTT (GET /exchange/status, warm conn, n={len(a)}) ===")
    print(f"  median={np.median(a)*1000:.1f}ms  p10={np.percentile(a,10)*1000:.1f}ms  "
          f"p90={np.percentile(a,90)*1000:.1f}ms  min={a.min()*1000:.1f}ms")
    return float(np.median(a))


async def ws_rtt(n: int):
    headers = ws_auth_headers()
    samples = []
    async with websockets.connect(WS_URL, additional_headers = headers) as ws:
        for _ in range(n):
            t0 = time.perf_counter()
            pong = await ws.ping()
            await pong
            samples.append(time.perf_counter() - t0)
            await asyncio.sleep(0.2)
    a = np.array(samples)
    print(f"=== WebSocket ping RTT (trade-api ws, n={len(a)}) ===")
    print(f"  median={np.median(a)*1000:.1f}ms  p10={np.percentile(a,10)*1000:.1f}ms  "
          f"p90={np.percentile(a,90)*1000:.1f}ms  min={a.min()*1000:.1f}ms")
    return float(np.median(a))


def main():
    parser = argparse.ArgumentParser(description = "Verify exchange ts semantics and measure Kalshi latency")
    parser.add_argument("--recording", default = None, help = "ticks_*.jsonl.gz for offset analysis")
    parser.add_argument("-n", type = int, default = 25, help = "latency samples per method")
    args = parser.parse_args()

    if args.recording:
        recording_offsets(args.recording)

    rest_med = rest_rtt(args.n)
    ws_med = asyncio.run(ws_rtt(args.n))
    fwd = min(rest_med, ws_med) / 2
    print(f"\nforward_delay estimate = median(WS RTT)/2 = {ws_med/2*1000:.1f}ms "
          f"(REST/2 = {rest_med/2*1000:.1f}ms; recommended FORWARD_DELAY_S = {fwd:.3f})")


if __name__ == "__main__":
    main()

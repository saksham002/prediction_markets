"""
Separate logger process for live trading (spawned by run_live.py alongside the
main trading process; common launch path). The MAIN process does NO file I/O — it
only ships strategy-stamped records over the queue. The logger:

  1. opens its OWN authenticated WS and logs the PRIVATE feed (`fill` channel) to
     private_feed.jsonl.gz. (Public feed is NOT logged here — the always-on Babel
     recorder already captures every game's public feed; analyze_live.py sources
     public data from there.)
  2. drains the IPC queue and writes the strategy's records VERBATIM to
     orders.jsonl (per-order 6-stage timing + metadata) and decisions.jsonl
     (per-actionable-event alpha). It computes nothing about them.
"""
import asyncio
import gzip
import json
import queue as _queue
import signal
import threading
import time
from pathlib import Path

import websockets

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.utils.api import WS_URL, ws_auth_headers
from research.hft.live_ipc import ORDERS_LOG, DECISIONS_LOG, PRIVATE_FEED_LOG


def logger_main(out_dir: str, q):
    """Process entry. Runs the private-feed WS logger (asyncio) + a queue-drain
    thread until SIGTERM / a None sentinel on the queue."""
    out = Path(out_dir)
    priv = gzip.open(out / PRIVATE_FEED_LOG, "wt")
    of = open(out / ORDERS_LOG, "w")
    df = open(out / DECISIONS_LOG, "w")
    stop = threading.Event()

    def drain():
        n = 0
        while not stop.is_set():
            try:
                rec = q.get(timeout = 1.0)
            except _queue.Empty:
                of.flush(); df.flush()
                continue
            if rec is None:                    # sentinel: trading finished
                break
            (of if rec.get("type") == "order" else df).write(json.dumps(rec) + "\n")
            n += 1
            if n % 50 == 0:
                of.flush(); df.flush()
        of.flush(); df.flush()

    drain_thread = threading.Thread(target = drain, daemon = True)
    drain_thread.start()

    async def ws_private():
        last_flush = time.time()
        while not stop.is_set():
            try:
                async with websockets.connect(WS_URL, additional_headers = ws_auth_headers()) as ws:
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                              "params": {"channels": ["fill"]}}))
                    print("logger: subscribed to private fill channel")
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout = 1.0)
                        except asyncio.TimeoutError:
                            if time.time() - last_flush > 5:
                                priv.flush(); last_flush = time.time()
                            continue
                        priv.write(json.dumps({"lts": time.time(), "d": json.loads(raw)}) + "\n")
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                if stop.is_set():
                    break
                print(f"logger WS reconnect ({type(e).__name__}: {e}) in 5s...")
                await asyncio.sleep(5)

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        asyncio.run(ws_private())
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        drain_thread.join(timeout = 3)
        for f in (priv, of, df):
            try:
                f.flush(); f.close()
            except Exception:
                pass
        print(f"logger: closed {out}")

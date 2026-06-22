"""
Lightweight IPC between the main trading process and the separate logger process.

Main NEVER writes files. It stamps timestamps in the hot path and, AFTER an order
is placed (off the order's critical path), pushes a small record onto a
`multiprocessing.Queue` via `TimingEmitter.emit`. The logger process drains the
queue and writes `orders.jsonl` / `acks.jsonl` / `decisions.jsonl` VERBATIM (it
computes nothing about them — all values are strategy-sourced). Kept dependency-free
so importing it in the sim/sweep path is free.

Order record fields (all main-sourced):
  type="order", exchange_ts(#1), read_ts(#2), alpha_start(#3), strategy_start(#4),
  sent_to_router(#5), router_done(#6), game, leg, side, action(new|cancel), qty,
  price, alpha, client_order_id
Ack record (the exchange's REST response to each place/cancel, joined to its order
by client_order_id):
  type="ack", result(ack|reject), read_ts, exchange_ts, op(place|cancel), leg, side,
  client_order_id, order_id, status, err
Decision record (every actionable event, no order):
  type="decision", exchange_ts, read_ts, alpha_start, strategy_start, game, leg,
  alpha, sides
"""

# log file names the logger writes (under the run's output dir)
ORDERS_LOG = "orders.jsonl"
ACKS_LOG = "acks.jsonl"
DECISIONS_LOG = "decisions.jsonl"
PRIVATE_FEED_LOG = "private_feed.jsonl.gz"


class TimingEmitter:
    """Non-blocking emit to the logger queue. Never raises into main: on a full
    queue it drops the record and counts it (main must never back-pressure on the
    logger). `q` is a multiprocessing.Queue created by the launcher."""

    __slots__ = ("q", "dropped")

    def __init__(self, q):
        self.q = q
        self.dropped = 0

    def emit(self, record: dict):
        try:
            self.q.put_nowait(record)
        except Exception:
            self.dropped += 1

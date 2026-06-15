import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collections import defaultdict

from src.utils.orderbook import MarketBook
from research.hft.passive_fill import PassiveFillEngine


def make_books():
    return defaultdict(MarketBook)


def trade_msg(ticker, taker_side, yes_price, qty, ts = 1000.0):
    return {
        "market_ticker": ticker,
        "taker_side": taker_side,
        "yes_price_dollars": f"{yes_price:.4f}",
        "no_price_dollars": f"{1.0 - yes_price:.4f}",
        "count_fp": f"{qty:.2f}",
        "ts": int(ts),
        "ts_ms": int(ts * 1000),
    }


def apply_neg_delta(eng, books, lts, ticker, side, price, qty):
    """Mimic the replay stream: record_delta, apply to book, then on_book."""
    eng.record_delta(lts, ticker, side, price, -qty)
    book_side = books[ticker].yes if side == "yes" else books[ticker].no
    book_side.apply_delta(price, -qty)
    return eng.on_book(lts, ticker)


def test_no_fill_while_queue_ahead():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "100.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books)
    oid = eng.place(1.0, "T", "yes", 0.49, 10)
    assert eng.orders[oid].queue_ahead == 100.0

    # Taker sells 60 into the yes bid: delta first (pending shields cap), then trade
    fills = apply_neg_delta(eng, books, 2.0, "T", "yes", "0.4900", 60.0)
    assert fills == []
    assert eng.orders[oid].queue_ahead == 100.0
    fills = eng.on_trade(2.1, trade_msg("T", "no", 0.49, 60))
    assert fills == []
    assert eng.orders[oid].queue_ahead == 40.0


def test_trade_overflow_partial_fill():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "30.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books)
    oid = eng.place(1.0, "T", "yes", 0.49, 10)

    # Trade of 35 consumes 30 ahead, fills 5 of ours
    assert apply_neg_delta(eng, books, 2.0, "T", "yes", "0.4900", 30.0) == []
    fills = eng.on_trade(2.1, trade_msg("T", "no", 0.49, 35))
    assert len(fills) == 1
    assert fills[0].qty == 5
    assert eng.orders[oid].remaining == 5


def test_cancel_cap_after_pending_expiry():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "100.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books)
    oid = eng.place(1.0, "T", "yes", 0.49, 10)

    # 70 cancels at our level: shielded by pending window at first...
    apply_neg_delta(eng, books, 2.0, "T", "yes", "0.4900", 70.0)
    assert eng.orders[oid].queue_ahead == 100.0
    # ...but after the window expires unexplained, cap tightens to displayed 30
    fills = eng.on_trade(3.0, trade_msg("T", "no", 0.49, 40))
    assert len(fills) == 1 and fills[0].qty == 10


def test_cross_fills_after_grace():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "50.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books)
    eng.place(1.0, "T", "yes", 0.49, 10)

    # New NO bid at 0.52 (implied yes ask 0.48) crosses us while the real
    # queue ahead empties (cancels). Fill only after the cross persists past
    # the grace window AND the pending window lets the cap drop queue_ahead.
    books["T"].no.apply_delta("0.5200", 25.0)
    eng.record_delta(2.0, "T", "yes", "0.4900", -50.0)
    books["T"].yes.apply_delta("0.4900", -50.0)
    assert eng.on_book(2.0, "T") == []
    assert eng.on_book(2.2, "T") == []
    fills = eng.on_book(3.1, "T")
    assert len(fills) == 1
    assert fills[0].qty == 10 and fills[0].reason == "cross"
    assert fills[0].order.order_id not in eng.orders


def test_cross_blocked_by_queue_ahead():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "50.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books)
    oid = eng.place(1.0, "T", "yes", 0.49, 10)

    # Crossing order (25) smaller than the displayed queue ahead of us (50):
    # its overflow never reaches us, no matter how long it rests.
    books["T"].no.apply_delta("0.5200", 25.0)
    assert eng.on_book(2.0, "T") == []
    assert eng.on_book(4.0, "T") == []
    assert eng.on_book(6.0, "T") == []
    assert oid in eng.orders


def test_transient_cross_no_fill():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "50.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books)
    oid = eng.place(1.0, "T", "yes", 0.49, 10)

    # Mid-sweep transient: NO bid at 0.51 locks the book, then is removed
    books["T"].no.apply_delta("0.5100", 5.0)
    assert eng.on_book(2.0, "T") == []
    books["T"].no.apply_delta("0.5100", -5.0)
    assert eng.on_book(2.1, "T") == []
    # Crossed state cleared: much later book events still don't fill us
    assert eng.on_book(5.0, "T") == []
    assert oid in eng.orders


def test_no_side_fill_from_yes_taker():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "50.00"]])
    books["T"].no.load_snapshot([["0.5000", "20.00"]])
    eng = PassiveFillEngine(books)
    oid = eng.place(1.0, "T", "no", 0.50, 15)
    assert eng.orders[oid].queue_ahead == 20.0

    # Aggressive YES buy at 0.50 consumes the NO bid level at 0.50
    assert apply_neg_delta(eng, books, 2.0, "T", "no", "0.5000", 20.0) == []
    fills = eng.on_trade(2.1, trade_msg("T", "yes", 0.50, 30))
    assert len(fills) == 1 and fills[0].qty == 10
    assert eng.orders[oid].remaining == 5


def test_delta_then_trade_no_double_count():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "50.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books)
    oid = eng.place(1.0, "T", "yes", 0.49, 10)

    # Fill of 20 hits the book first (delta), then its trade message arrives.
    # Pending shields the cap; the trade decrements exactly once.
    apply_neg_delta(eng, books, 2.0, "T", "yes", "0.4900", 20.0)
    assert eng.orders[oid].queue_ahead == 50.0
    fills = eng.on_trade(2.1, trade_msg("T", "no", 0.49, 20))
    assert fills == []
    assert eng.orders[oid].queue_ahead == 30.0


def test_trade_predating_placement_ignored():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "30.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books)
    oid = eng.place(100.0, "T", "yes", 0.49, 10)

    # Late trade message stamped well before our placement: no decrement, no fill
    fills = eng.on_trade(100.1, trade_msg("T", "no", 0.49, 50, ts = 50.0))
    assert fills == []
    assert eng.orders[oid].queue_ahead == 30.0


def test_forward_delay_gates_fills():
    books = make_books()
    books["T"].yes.load_snapshot([["0.4900", "30.00"]])
    books["T"].no.load_snapshot([["0.5000", "80.00"]])
    eng = PassiveFillEngine(books, forward_delay = 0.020)
    oid = eng.place(100.0, "T", "yes", 0.49, 10)

    # Trade matched before placement + forward_delay: order wasn't on the book yet
    fills = eng.on_trade(100.05, trade_msg("T", "no", 0.49, 50, ts = 100.01))
    assert fills == []
    assert eng.orders[oid].queue_ahead == 30.0

    # Trade matched after the order reached the exchange: counts (and fills overflow)
    fills = eng.on_trade(100.1, trade_msg("T", "no", 0.49, 50, ts = 100.03))
    assert len(fills) == 1 and fills[0].qty == 10


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
    print("All passive_fill tests passed")

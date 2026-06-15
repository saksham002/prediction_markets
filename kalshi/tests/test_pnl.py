import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pnl import PnL


def test_resolve_long_yes_position():
    pnl = PnL()
    pnl.trade("TEST", "long", 10, 0.40)

    pnl.resolve("TEST", 1.0)

    assert math.isclose(pnl.realized_pnl, 6.0)
    assert "TEST" not in pnl.positions


def test_resolve_short_no_position():
    pnl = PnL()
    pnl.trade("TEST", "short", 10, 0.60)

    pnl.resolve("TEST", 0.0)

    assert math.isclose(pnl.realized_pnl, 6.0)
    assert "TEST" not in pnl.positions


def test_fee_accounting_and_net_total_pnl():
    pnl = PnL(charge_fees = True, fee_model = "kalshi")
    # Seed the fee cache so the test doesn't hit the network for a fake ticker
    pnl.series_fees["TEST"] = (1.0, "quadratic")
    pnl.trade("TEST", "long", 100, 0.40)
    pnl.trade("TEST", "short", 100, 0.50)

    expected_fees = (
        PnL.kalshi_taker_fee(100, 0.40) +
        PnL.kalshi_taker_fee(100, 0.50)
    )

    assert math.isclose(pnl.realized_pnl, 10.0)
    assert math.isclose(pnl.fees_paid, expected_fees)
    assert math.isclose(pnl.net_total_pnl(), 10.0 - expected_fees)

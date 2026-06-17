"""Float-residue-robust comparison helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.feps import EPS, is_zero, is_pos, is_neg, gt, lt, gte, lte


def test_residue_treated_as_zero():
    r = 1e-13                                  # the accumulated-residue case
    assert is_zero(r) and is_zero(-r)
    assert not is_pos(r) and not is_neg(r)
    assert lte(r, 0) and gte(r, 0)             # both <=0 and >=0 hold for residue


def test_real_size_is_nonzero():
    assert is_pos(0.01) and not is_zero(0.01)  # smallest real contract size (count_fp 2dp)
    assert is_neg(-0.01)


def test_two_arg_tolerance():
    assert gt(1.0, 0.0) and lt(0.0, 1.0)
    assert not gt(1.0, 1.0 + 1e-12)            # within tolerance -> not strictly greater
    assert gte(1.0, 1.0 + 1e-12) and lte(1.0, 1.0 - 1e-12)


def test_eps_default():
    assert EPS == 1e-6

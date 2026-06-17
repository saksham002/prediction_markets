"""Float comparison helpers robust to accumulated rounding residue.

Fractional contract sizes (count_fp, queue depth, inventory) accumulate float
error under repeated add/subtract, so a value that is logically 0 can land at
e.g. 1e-13. Comparing such a value with `== 0` / `> 0` / `<= 0` then misfires:
a phantom non-zero position, a book level that looks alive, an order that never
clears. These helpers treat anything within EPS of the boundary as equal.

EPS = 1e-6: far above residue (~1e-13), far below any real contract size
(count_fp has 2 decimals, so the smallest real quantity is 0.01). Use for
quantities / inventory / depth comparisons against a threshold (usually 0) —
NOT for price ticks (those are exact 4-decimal dollars with their own
tolerances). All functions are tiny and inline-able; import the ones you need.
"""

EPS = 1e-6


def is_zero(x: float, eps: float = EPS) -> bool:
    """x == 0 within tolerance."""
    return -eps <= x <= eps


def is_pos(x: float, eps: float = EPS) -> bool:
    """x > 0 beyond rounding noise."""
    return x > eps


def is_neg(x: float, eps: float = EPS) -> bool:
    """x < 0 beyond rounding noise."""
    return x < -eps


def gt(a: float, b: float, eps: float = EPS) -> bool:
    """a > b beyond tolerance."""
    return a - b > eps


def lt(a: float, b: float, eps: float = EPS) -> bool:
    """a < b beyond tolerance."""
    return a - b < -eps


def gte(a: float, b: float, eps: float = EPS) -> bool:
    """a >= b within tolerance (i.e. not lt)."""
    return a - b >= -eps


def lte(a: float, b: float, eps: float = EPS) -> bool:
    """a <= b within tolerance (i.e. not gt)."""
    return a - b <= eps

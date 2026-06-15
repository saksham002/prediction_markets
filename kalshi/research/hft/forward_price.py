"""
Forward-price lookup for correlation studies.

Given a market's full per-trigger series (ts, yes_bid, yes_ask), attach the
touch that prevails `horizon` seconds later for each horizon: bid_<h>, ask_<h>.

The lookup is the exact searchsorted over the COMPLETE series and is applied
BEFORE any filtering — every top-of-book change is in the series, so the value
returned is the true price at t+h, and dropping rows afterwards (FilterStrategy)
never corrupts a forward value. np.nan where t+h runs past the recording.
"""

import numpy as np


def forward_fields(ts, yes_bid, yes_ask, horizons) -> dict[str, np.ndarray]:
    """{f"bid_{h}": arr, f"ask_{h}": arr} aligned to ts, one entry per horizon.

    For row i and horizon h: the last touch at or before ts[i] + h (the price
    prevailing at t+h), NaN when ts[i] + h is past the end of the series.
    """
    ts = np.asarray(ts, dtype = float)
    yb = np.asarray(yes_bid, dtype = float)
    ya = np.asarray(yes_ask, dtype = float)
    n = len(ts)
    out: dict[str, np.ndarray] = {}
    for h in horizons:
        idx = np.searchsorted(ts, ts + h, side = "right") - 1
        valid = (idx >= 0) & (ts + h <= ts[-1])
        b = np.full(n, np.nan)
        a = np.full(n, np.nan)
        b[valid] = yb[idx[valid]]
        a[valid] = ya[idx[valid]]
        out[f"bid_{h}"] = b
        out[f"ask_{h}"] = a
    return out

"""Recompute train-MLB |alpha| percentile thresholds for the size/cap sweep on
the CURRENT dataset. Writes studies/mlb_thresholds.json consumed by
sweep_size_cap.py."""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from lasso_pipeline import collect_samples, is_test
try:
    from research.hft.paths import DATASET, STUDIES
except ImportError:
    from paths import DATASET, STUDIES

ALPHAS = ["tfma_pw_300s", "agg_300s", "obi"]
PCTS = [50, 75, 90, 95]
OUT = STUDIES / "mlb_thresholds.json"


def main():
    names, by_event = collect_samples(DATASET)
    col = {n: i for i, n in enumerate(names)}
    absvals = {a: [] for a in ALPHAS}
    n_games = 0
    for event, g in by_event.items():
        if not event.startswith("KXMLBGAME") or is_test(event):
            continue
        n_games += 1
        A = np.array(g["a"], dtype = float)
        for a in ALPHAS:
            x = A[:, col[a]]
            absvals[a].append(np.abs(x[~np.isnan(x)]))
    thr = {}
    print(f"train-MLB games: {n_games}")
    for a in ALPHAS:
        x = np.concatenate(absvals[a]) if absvals[a] else np.array([0.0])
        vals = sorted({0.0, *(round(float(np.percentile(x, p)), 4) for p in PCTS)})
        thr[a] = vals
        print(f"  {a}: {vals}")
    OUT.parent.mkdir(parents = True, exist_ok = True)
    OUT.write_text(json.dumps(thr, indent = 1))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

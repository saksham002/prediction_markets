"""For each alpha, take its best-on-TRAIN config from the size/cap sweep and
render the split-aware figure set into plots/alpha_figs/<alpha>/:
  TRAIN: random (seed 86), best, worst   TEST: random (seed 86), worst
Reuses cached sims where present; sims the rest."""
import json
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_buffer import is_test_game
from sweep_size_cap import RESULTS
from viz_best import run_config

ALPHAS = ["tfma_pw_300s", "agg_300s", "obi"]
OUT_BASE = Path("/home/saksham3/projects/personal/prediction_markets/plots/alpha_figs")


def per_alpha_train_best():
    rows = []
    for p in sorted(RESULTS.glob("r_*.json")):
        with open(p) as f:
            rows.append(json.load(f))
    best = {}
    for a in ALPHAS:
        sub = [r for r in rows if r["alpha"] == a]
        best[a] = max(sub, key = lambda r: r["train_realized_net"])
    return best


def pick(games: dict, kinds: list, seed: int = 86):
    items = sorted(games.items(), key = lambda kv: kv[1]["realized_net"])
    rng = random.Random(seed)
    out = {}
    if "worst" in kinds:
        out["worst"] = items[0]
    if "best" in kinds:
        out["best"] = items[-1]
    if "random" in kinds:
        out["random"] = rng.choice(items)
    return out


def main():
    best = per_alpha_train_best()
    for alpha, cfg in best.items():
        print(f"\n=== {alpha}: best-train cfg thr={cfg['thr']:g} s={cfg['size']:g} "
              f"cap={cfg['cap']:g} (train_rn={cfg['train_realized_net']:+.2f} "
              f"test_rn={cfg['test_realized_net']:+.2f}) ===")
        out_dir = OUT_BASE / alpha
        out_dir.mkdir(parents = True, exist_ok = True)
        for old in out_dir.glob("*.png"):   # clear stale figures from prior runs
            old.unlink()
        events = run_config(alpha, cfg)
        if not events:
            print("  no traded events")
            continue
        train_g = {e: i for e, i in events.items() if not is_test_game(e)}
        test_g = {e: i for e, i in events.items() if is_test_game(e)}
        print(f"  games traded: {len(train_g)} train, {len(test_g)} test")
        plan = {"train": (train_g, ["random", "best", "worst"]),
                "test": (test_g, ["random", "worst"])}
        manifest = {"config": cfg, "picks": {}}
        for split, (games, kinds) in plan.items():
            if not games:
                continue
            for kind, (event, info) in pick(games, kinds).items():
                dst = out_dir / f"{split}_{kind}_{event.replace(':', '_')}.png"
                shutil.copy(info["png"], dst)
                manifest["picks"][f"{split}_{kind}"] = {
                    "event": event, "realized_net": info["realized_net"], "png": dst.name}
                print(f"    {split} {kind}: {event} rn={info['realized_net']:+.2f}")
        with open(out_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent = 1, default = str)


if __name__ == "__main__":
    main()

"""Train-best config only, split-aware game figures:
  TRAIN split: random (seed 86), best, worst by realized_net
  TEST split:  random (seed 86), worst
Reuses the existing bestviz_train_best_* sim dirs (no re-sim)."""
import json
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_buffer import is_test_game
from viz_best import best_configs, run_config

OUT = Path("/home/saksham3/projects/personal/prediction_markets/plots/bestviz_trainbest")


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
    OUT.mkdir(parents = True, exist_ok = True)
    cfg = best_configs()["train_best"]
    print(f"train-best config: {cfg['alpha']} thr={cfg['thr']:g} s={cfg['size']:g} "
          f"cap={cfg['cap']:g} (train_rn={cfg['train_realized_net']:+.2f} "
          f"test_rn={cfg['test_realized_net']:+.2f})")
    events = run_config("train_best", cfg)
    train_g = {e: i for e, i in events.items() if not is_test_game(e)}
    test_g = {e: i for e, i in events.items() if is_test_game(e)}
    print(f"games traded: {len(train_g)} train, {len(test_g)} test")

    plan = {"train": (train_g, ["random", "best", "worst"]),
            "test": (test_g, ["random", "worst"])}
    manifest = {"config": cfg, "picks": {}}
    for split, (games, kinds) in plan.items():
        if not games:
            print(f"  no {split} games traded")
            continue
        for kind, (event, info) in pick(games, kinds).items():
            dst = OUT / f"trainbest_{split}_{kind}_{event.replace(':', '_')}.png"
            shutil.copy(info["png"], dst)
            manifest["picks"][f"{split}_{kind}"] = {
                "event": event, "realized_net": info["realized_net"], "png": dst.name}
            print(f"  {split} {kind}: {event} realized_net={info['realized_net']:+.2f} -> {dst.name}")
    with open(OUT / "manifest.json", "w") as f:
        json.dump(manifest, f, indent = 1, default = str)


if __name__ == "__main__":
    main()

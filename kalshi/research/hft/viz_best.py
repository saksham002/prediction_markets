"""For the best-on-TRAIN and best-on-TEST configs of the size/cap sweep, rerun
mm_sim with full logging and render the 4-panel game graphs (odds/alpha/
position/PnL via viz.plot_event) for 3 games each: random (seed 86), best
realized-net, worst realized-net."""
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sweep_size_cap import COMBOS, RESULTS  # noqa: F401 (RESULTS dir reused)

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
SIMS = Path("/data/user_data/saksham3/kalshi_hft/sims")
OUT = Path("/home/saksham3/projects/personal/prediction_markets/plots/bestviz")
PY = sys.executable
HFT = Path(__file__).parent


def best_configs():
    rows = []
    for p in sorted(RESULTS.glob("r_*.json")):
        with open(p) as f:
            rows.append(json.load(f))
    train_best = max(rows, key = lambda r: r["train_realized_net"])
    test_best = max(rows, key = lambda r: r["test_realized_net"])
    return {"train_best": train_best, "test_best": test_best}


def run_config(label: str, cfg: dict) -> dict:
    """mm_sim + viz on every recording; returns event -> {realized_net, png}."""
    events = {}
    for rec in sorted(DATASET.glob("*.jsonl.gz")):
        if rec.stat().st_size <= 10000:
            continue
        tag = f"bestviz_{label}_{rec.name.split('.')[0]}"
        run_dir = SIMS / tag
        if not (run_dir / "state.csv").exists():
            subprocess.run([
                PY, str(HFT / "mm_sim.py"), str(rec),
                "-a", cfg["alpha"], "-t", str(cfg["thr"]),
                "-s", str(cfg["size"]), "-i", str(cfg["cap"]),
                "--budget", "1000", "--pair-risk", "--series", "KXMLBGAME",
                "--tag", tag,
            ], check = True, stdout = subprocess.DEVNULL)
        subprocess.run([PY, str(HFT / "viz.py"), str(run_dir)], check = True,
                       stdout = subprocess.DEVNULL)
        report_path = run_dir / "viz" / "report.json"
        if not report_path.exists():
            continue
        with open(report_path) as f:
            report = json.load(f)
        for event, info in report["events"].items():
            if info.get("realized_net") is None:
                continue
            events[event] = {"realized_net": info["realized_net"],
                             "png": run_dir / "viz" / info["png"]}
    return events


def main():
    OUT.mkdir(parents = True, exist_ok = True)
    chosen = best_configs()
    manifest = {}
    for label, cfg in chosen.items():
        print(f"{label}: {cfg['alpha']} thr={cfg['thr']:g} s={cfg['size']:g} cap={cfg['cap']:g} "
              f"(train_rn={cfg['train_realized_net']:+.2f} test_rn={cfg['test_realized_net']:+.2f})")
        events = run_config(label, cfg)
        if not events:
            print(f"  no traded events for {label}")
            continue
        items = sorted(events.items(), key = lambda kv: kv[1]["realized_net"])
        worst = items[0]
        best = items[-1]
        rng = random.Random(86)
        rnd = rng.choice(items)
        picks = {"worst": worst, "best": best, "random": rnd}
        manifest[label] = {"config": cfg, "picks": {}}
        for kind, (event, info) in picks.items():
            dst = OUT / f"{label}_{kind}_{event.replace(':', '_')}.png"
            shutil.copy(info["png"], dst)
            manifest[label]["picks"][kind] = {"event": event,
                                              "realized_net": info["realized_net"],
                                              "png": dst.name}
            print(f"  {kind}: {event} realized_net={info['realized_net']:+.2f} -> {dst.name}")
    with open(OUT / "manifest.json", "w") as f:
        json.dump(manifest, f, indent = 1, default = str)


if __name__ == "__main__":
    main()

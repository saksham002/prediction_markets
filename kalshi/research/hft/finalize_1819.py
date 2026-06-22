"""One-off: finalize ONLY the Jun 18 + Jun 19 staged games into dataset/ (the
nightly finalize has been aborting on a corrupt staged file). Moves just those
games to an isolated dir first so the live recorder writing Jun 20 into
games_recording/ is untouched, then runs the (now per-file-tolerant)
finalize_games. Corrupt files are quarantined to games_corrupt/."""
import sys
from pathlib import Path

sys.path.insert(0, "/home/saksham3/projects/personal/prediction_markets/kalshi")
from research.hft.record_ticks import finalize_games, DATASET_DIR, SUB_DIR, DATA_ROOT

STAGING = DATA_ROOT / "games_recording"
TMP = DATA_ROOT / "finalize_1819"
TMP.mkdir(parents = True, exist_ok = True)

moved = []
for pat in ("*26JUN18*.jsonl.gz", "*26JUN19*.jsonl.gz"):
    for f in sorted(STAGING.glob(pat)):
        f.rename(TMP / f.name)
        moved.append(f.name)
print(f"moved {len(moved)} Jun18/19 game files  {STAGING.name}/ -> {TMP.name}/")
for n in sorted(moved):
    print("  ", n)

print("\n=== finalize_games ===")
finalize_games(TMP, DATASET_DIR, SUB_DIR)

leftover = sorted(TMP.glob("*.jsonl.gz"))
print(f"\nleftover in {TMP.name}/ (should be 0): {len(leftover)}")
for f in leftover:
    print("  ", f.name)

print("\n=== Jun18-19 now in dataset/ ===")
ds = sorted(DATASET_DIR.glob("*26JUN18*.jsonl.gz")) + sorted(DATASET_DIR.glob("*26JUN19*.jsonl.gz"))
for f in ds:
    print("  ", f.name)
print(f"  ({len(ds)} games)")

print("\n=== Jun18-19 routed to sub_1M/ (sub-threshold) ===")
sb = sorted(SUB_DIR.glob("*26JUN18*.jsonl.gz")) + sorted(SUB_DIR.glob("*26JUN19*.jsonl.gz"))
for f in sb:
    print("  ", f.name)

print("\n=== games_corrupt/ (quarantined) ===")
for f in sorted((DATA_ROOT / "games_corrupt").glob("*")):
    print("  ", f.name)

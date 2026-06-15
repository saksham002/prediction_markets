"""Build one replay-player HTML per recording listed in
scratch/analyze_book.list, into plots/players/. Removes any existing HTMLs in
that dir first. Each game's event ticker is its dataset filename stem.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from book_player import build_player

LIST = Path("/home/saksham3/projects/personal/prediction_markets/scratch/analyze_book.list")
OUT = Path("/home/saksham3/projects/personal/prediction_markets/plots/players")


def main():
    OUT.mkdir(parents = True, exist_ok = True)
    for old in sorted(OUT.glob("*.html")):
        old.unlink()
        print(f"removed old {old.name}")
    paths = [ln.strip() for ln in LIST.read_text().splitlines() if ln.strip()]
    print(f"building {len(paths)} players")
    for p in paths:
        rec = Path(p)
        event = rec.stem.replace(".jsonl", "")
        try:
            build_player(rec, event, OUT / f"replay_{event}.html")
        except Exception as e:
            print(f"FAILED {event}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

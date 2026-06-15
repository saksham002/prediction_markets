"""Build replay players for the highest-volume MLB game and the Korea WC game."""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from book_player import build_player

DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
OUT = Path("/home/saksham3/projects/personal/prediction_markets/plots/players")


def main():
    vol = defaultdict(float)          # event -> traded contracts
    by_rec = defaultdict(lambda: defaultdict(float))  # event -> rec -> contracts
    kor_event, kor_rec = None, None
    for rec in sorted(DATASET.glob("*.jsonl.gz")):
        if rec.stat().st_size <= 10000:
            continue
        with gzip.open(rec, "rt") as f:
            for line in f:
                if '"type":"trade"' not in line:
                    continue
                m = json.loads(line)["d"]["msg"]
                event = m["market_ticker"].rsplit("-", 1)[0]
                qty = float(m["count_fp"])
                if event.startswith("KXMLBGAME"):
                    vol[event] += qty
                    by_rec[event][rec] += qty
                elif "KORCZE" in event:
                    kor_event = event
                    kor_rec = rec
    best_mlb = max(vol, key = vol.get)
    mlb_rec = max(by_rec[best_mlb], key = by_rec[best_mlb].get)
    print(f"highest-volume MLB: {best_mlb} ({vol[best_mlb]:.0f} cts) in {mlb_rec.name}")
    print(f"Korea WC: {kor_event} in {kor_rec.name}")
    build_player(mlb_rec, best_mlb, OUT / f"replay_{best_mlb}.html")
    build_player(kor_rec, kor_event, OUT / f"replay_{kor_event}.html")


if __name__ == "__main__":
    main()

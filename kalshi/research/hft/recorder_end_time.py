"""Print seconds-from-now at which the recorder job should END: the next 3am ET,
pushed later if a game is still live then (so a recorder never dies mid-game).
Capped at 23.5h so the recorder job stays within 24h (run_recorder pads +30min
for finalize -> SLURM --time <= 24h). Used by run_recorder.sh to set --time and
the recorder --duration dynamically from the actual schedule."""
import contextlib
import datetime
import sys
import zoneinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.discovery import discover_league_events
from research.hft.game_times import game_start

ET = zoneinfo.ZoneInfo("America/New_York")
MAX_GAME_S = 5 * 3600   # generous game length incl. extra innings / stoppage
CAP_S = 24 * 3600 - 1800   # 23.5h: run_recorder adds +30min finalize pad -> SLURM --time <= 24h
QUIET_BUFFER_S = 1800   # end >=30min after the last game that overlaps 3am


def main():
    series = sys.argv[1] if len(sys.argv) > 1 else "KXMLBGAME,KXNBAGAME,KXNHLGAME,KXNFLGAME,KXWCGAME,KXINTLFRIENDLYGAME"
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    now = datetime.datetime.now(ET)
    three = now.replace(hour = 3, minute = 0, second = 0, microsecond = 0)
    if now >= three:
        three += datetime.timedelta(days = 1)
    end = three.timestamp()

    # Push end past any game whose [T-1h, T+max] interval covers the candidate end
    try:
        # discovery prints progress to stdout — keep our stdout the number only
        with contextlib.redirect_stdout(sys.stderr):
            pairs, events = discover_league_events(series, top_n)
        items = [(p["event_ticker"], p["first_ticker"]) for p in pairs] + \
                [(e["event_ticker"], e["tickers"][0]) for e in events]
        for ev, tk in items:
            start = game_start(ev, tk)
            if start is None:
                continue
            if start - 3600 <= end <= start + MAX_GAME_S:
                end = max(end, start + MAX_GAME_S + QUIET_BUFFER_S)
    except Exception as e:
        print(f"# schedule lookup failed ({e}); using plain 3am ET", file = sys.stderr)

    secs = int(end - now.timestamp())
    secs = max(3600, min(secs, CAP_S))
    print(secs)


if __name__ == "__main__":
    main()

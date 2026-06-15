import datetime
import sys
import zoneinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.discovery import discover_league_events
from research.hft.game_times import game_start

ET = zoneinfo.ZoneInfo("America/New_York")
DUR = {"KXMLBGAME": 4 * 3600, "KXNBAGAME": 3 * 3600, "KXNHLGAME": 3.2 * 3600,
       "KXNFLGAME": 3.5 * 3600, "KXWCGAME": 2.5 * 3600, "KXINTLFRIENDLYGAME": 2.5 * 3600}

now = datetime.datetime.now(ET)
t515 = now.replace(hour = 17, minute = 15, second = 0, microsecond = 0)
print(f"now = {now.strftime('%H:%M')} ET ; target t = 5:15pm ET")

pairs, events = discover_league_events(
    "KXMLBGAME,KXNBAGAME,KXNHLGAME,KXNFLGAME,KXWCGAME,KXINTLFRIENDLYGAME", 30)
items = [(p["event_ticker"], p["first_ticker"]) for p in pairs] + \
        [(e["event_ticker"], e["tickers"][0]) for e in events]

now_w, t515_w = [], []
for ev, tk in items:
    s = game_start(ev, tk)
    if s is None:
        continue
    d = DUR.get(ev.split("-")[0], 3.5 * 3600)
    st = datetime.datetime.fromtimestamp(s, ET)
    end = datetime.datetime.fromtimestamp(s + d, ET)
    win_lo = st - datetime.timedelta(hours = 1)
    if win_lo <= now <= end:
        now_w.append((ev, st.strftime('%H:%M'), end.strftime('%H:%M')))
    if win_lo <= t515 <= end:
        t515_w.append((ev, st.strftime('%H:%M'), end.strftime('%H:%M')))

print(f"\n--- in recording window NOW ({len(now_w)}): ---")
for e in sorted(now_w, key = lambda x: x[1]):
    print(f"  {e[0]}  start {e[1]} ~end {e[2]}")
print(f"\n--- in recording window at 5:15pm ({len(t515_w)}): ---")
for e in sorted(t515_w, key = lambda x: x[1]):
    print(f"  {e[0]}  start {e[1]} ~end {e[2]}")

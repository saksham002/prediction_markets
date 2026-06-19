"""Fetch exact game-clock boundaries (kickoff / halftime / 2nd-half restart /
full time) for WC games from ESPN keyEvents wallclocks, so the WC strategy can
map game-minute phases (5', 85') to wall time. Cache -> studies/wc_clocks.json.

Usage: espn_clock.py <event_ticker...>   (or no args = all WC games in dataset)
Module: clocks_for(event_ticker) -> {"ko","ht","sh","ft"} epoch secs (or None).
"""
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

CACHE = Path("/data/user_data/saksham3/kalshi_hft/studies/wc_clocks.json")
DATASET = Path("/data/user_data/saksham3/kalshi_hft/dataset")
SB = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates={d}"
SUM = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={e}"
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
# ESPN abbreviation -> Kalshi ticker abbreviation (where they differ)
ALIAS = {"HAI": "HTI", "IRN": "IRI", "ALG": "DZA"}


def _get(url):
    req = urllib.request.Request(url, headers = {"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout = 30))


def _iso(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _parse(event_ticker):
    m = re.match(r"KXWCGAME-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$", event_ticker)
    if not m:
        return None, None
    yy, mon, dd, teams = m.groups()
    date = f"20{yy}{MONTHS[mon]:02d}{dd}"
    return date, teams


def fetch_clock(event_ticker, live = False):
    """Past games: keyEvent wallclocks (ko/ht/sh/ft + goals). `live=True` (live
    trading): if the match hasn't produced a Kickoff keyEvent yet, fall back to the
    scoreboard's SCHEDULED start as a provisional `ko` so pre-match gating works;
    the real KO/HT/SH/FT overwrite it as the game progresses."""
    date, teams = _parse(event_ticker)
    if not date:
        return None
    try:
        sb = _get(SB.format(d = date))
    except Exception as e:
        print(f"  {event_ticker}: scoreboard fetch failed ({e})")
        return None
    eid, id2ab, sched = None, {}, None
    for ev in sb.get("events", []):
        comp = ev["competitions"][0]["competitors"]
        abbrs = [ALIAS.get(c["team"].get("abbreviation", ""), c["team"].get("abbreviation", ""))
                 for c in comp]
        if len(abbrs) == 2 and (abbrs[0] + abbrs[1] == teams or abbrs[1] + abbrs[0] == teams):
            eid = ev["id"]
            id2ab = {c["team"]["id"]: a for c, a in zip(comp, abbrs)}
            sched = _iso(ev["date"]) if ev.get("date") else None   # scheduled KO
            break
    if eid is None:
        print(f"  {event_ticker}: no ESPN event matched (teams={teams})")
        return None
    summ = _get(SUM.format(e = eid))
    out, events = {}, []
    for ke in summ.get("keyEvents", []):
        typ = (ke.get("type") or {}).get("text", "")
        wc = ke.get("wallclock")
        if not wc:
            continue
        if typ == "Kickoff" and "ko" not in out:
            out["ko"] = _iso(wc)
        elif typ in ("Halftime", "Half Time") and "ht" not in out:
            out["ht"] = _iso(wc)
        elif typ == "Start 2nd Half" and "sh" not in out:
            out["sh"] = _iso(wc)
        elif typ in ("End Regular Time", "Full Time", "Game End"):
            out["ft"] = _iso(wc)
        # Major events to mark on plots: goals (scoringPlay covers headers,
        # penalties, own goals) and red cards.
        is_goal, is_red = bool(ke.get("scoringPlay")), "red card" in typ.lower()
        if is_goal or is_red:
            tm = (ke.get("team") or {}).get("id")
            events.append({"wc": _iso(wc), "min": (ke.get("clock") or {}).get("displayValue", ""),
                           "kind": "goal" if is_goal else "red", "team": id2ab.get(tm, "")})
    if "ko" not in out:
        if live and sched is not None:
            out["ko"] = sched                 # provisional scheduled KO (no keyEvents yet)
            out["provisional"] = True
        else:
            return None
    out["events"] = events
    return out


def _load_cache():
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def clocks_for(event_ticker):
    return _load_cache().get(event_ticker)


def events_for(event_ticker):
    return (_load_cache().get(event_ticker) or {}).get("events", [])


def main():
    events = sys.argv[1:]
    if not events:
        events = sorted(p.stem.replace(".jsonl", "") for p in DATASET.glob("KXWCGAME*.jsonl.gz"))
    cache = _load_cache()
    for ev in events:
        c = fetch_clock(ev)
        if c:
            cache[ev] = c
            fmt = lambda k: datetime.datetime.fromtimestamp(c[k]).strftime("%H:%M") if k in c else "?"
            ev_str = " ".join(f"{e['kind'][0].upper()}:{e['team']}{e['min']}" for e in c.get("events", []))
            print(f"  {ev}: KO {fmt('ko')} HT {fmt('ht')} 2H {fmt('sh')} FT {fmt('ft')}  [{ev_str}]")
    CACHE.parent.mkdir(parents = True, exist_ok = True)
    CACHE.write_text(json.dumps(cache, indent = 1))
    print(f"\ncached {len(cache)} game clocks -> {CACHE}")


if __name__ == "__main__":
    main()

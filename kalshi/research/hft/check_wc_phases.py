import csv
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.hft.espn_clock import clocks_for

ev = "KXWCGAME-26JUN11MEXRSA"
c = clocks_for(ev)
rd = Path("/data/user_data/saksham3/kalshi_hft/sims/wctest_mexrsa")
orders = list(csv.DictReader(open(rd / "orders.csv")))
fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")
ko5, liq, ft = c["ko"] + 300, c["sh"] + 2400, c["ft"]
print(f"phase boundaries: KO+5={fmt(ko5)}  85'={fmt(liq)}  FT={fmt(ft)}")
print(f"half-time no-trade: {fmt(c['ht'])} -> {fmt(c['sh'])}")

placed = [float(r["lts"]) for r in orders if r["action"] == "place"]
fills = [r for r in orders if r["action"] == "fill"]
print(f"\nplaced orders: {len(placed)}; first {fmt(min(placed))} last {fmt(max(placed))}")
# violations
pre = sum(1 for t in placed if t < ko5)
ht_v = sum(1 for t in placed if c["ht"] <= t < c["sh"])
print(f"places before KO+5: {pre}   places during half-time: {ht_v}")
# liquidation-phase places: should be reduce-side only
liq_places = [r for r in orders if r["action"] == "place" and float(r["lts"]) >= liq]
print(f"places in liquidation phase (>=85'): {len(liq_places)}")
# count main-phase places
main_places = sum(1 for t in placed if ko5 <= t < liq and not (c["ht"] <= t < c["sh"]))
print(f"places in main phase: {main_places}")

# extras/ — strategy data files

Data files read by the strategy code but produced on Babel under
`/data/user_data/saksham3/kalshi_hft/studies/` (not normally in git). Committed here so a
checkout elsewhere has them.

- **`wc_clocks.json`** — per-game KO / half-time / 2nd-half / FT wall-clock times +
  goal/card events, used by `espn_clock.clocks_for()` for WCStrategy phase gating
  (no-trade before KO+5′, half-time, liquidate at 85′). Without it WCStrategy stays
  in the "main" phase the whole game. `espn_clock.py`'s `CACHE` path points at the Babel
  copy by default; point it at this file (or copy it there) on another machine.

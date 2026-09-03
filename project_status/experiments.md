# Currently Running & Recent Experiments

_Last updated: 2026-06-22 ET_

## Jun 21 — agg_dev gate sweep (obi_dev vs obi_dev AND agg_dev), 21-6, generic self-collecting sweep
**36 configs**, $250 budget, **free_budget=true**, size 200 / cap 1000 FIXED, **21-6 chronological** split (27-game WC dataset), REALISTIC delays + in-flight lock + 20ms forward, per-game metric. Built on the new StrategyConfig/EmaSignal refactor (symmetric alpha-gate list — a side is blocked if ANY alpha crosses its own pctile threshold, no primary; new `agg_dev_<hl> = agg_<hl> − EMA_<hl>(agg_<hl>)`; config-driven half-lives; see codebase_notes 2026-06-20). Two arms:
- **A (obi_dev only)**: hl {10,60,300}s × pct {50,75,90} = 9 configs
- **B (obi_dev AND agg_dev)**: obi_hl {10,60,300}s × agg_hl {1,10,60}s × pct {50,75,90} = 27 configs

Ran on `preempt` (24-shard array `8667914` + dependent `--finalize` `8667915`, all COMPLETED); self-collecting (`best_configs(dir, score_fn=mean)` → best-in & best-out from the stored per-(config,game) PnLs; no `--collect` re-run). Output `sims/wc_sweep_aggdev216/` (36/36).

**Result — adding agg_dev does NOT help OOS; obi_dev alone wins:**

| | best per-game config | IN/g | OUT/g |
|---|---|---:|---:|
| **best IN & OUT** | **A_obi60s_p75 — obi_dev hl60, thr 0.363514** | **+91.1** | **+247.9** |

- Top-5 OUT are ALL **arm A (obi-only)**: obi60s_p75 (+247.9), obi60s_p90 (+228.2), obi300s_p90 (+185.3), obi10s_p75 (+177.7), obi10s_p90 (+176.9).
- For matched obi_hl, the AND-gate with agg_dev **consistently lowers** OUT (obi60s_p75: A +247.9 vs best-B `obi60s_agg60s_p75` +187.6; obi300s_p90: A +185.3 vs best-B +119.9). The one high-OOS B config (+187.6) has **negative IN (−16.3)** = OOS luck, not robust.
- **agg_dev IS active** (arm B PnL/fills differ from A) but mostly blocks profitable obi entries → no added edge. **agg_dev gate REJECTED** (see ideas.md).
- **SELECTED (per-game robust, #1 in BOTH samples): `obi_dev_60s` thr 0.363514 · s200 · c1000 · `free_budget:true`.** (Supersedes the prior `obi_dev_15s` thr 0.275 pick; this 21-6 run + refactor favors the 60s HL.)
- Caveats: 6-game OOS, OUT ≫ IN (favorable test slice) → trust the relative ranking (obi-only > obi+agg), not the absolute OOS level. The (separately-approved) `_budget_clip` `//`→`/` makes per-game PnL path-sensitive (canary rebaselined 229→23 fills); per-game-averaged metric used throughout.

## Jun 19 — $250 r204 sweeps re-run CLEAN + free_budget flag → robust config
**Two $250 sweeps, 72 combos each** (alphas {obi_dev_15s, obi_dev_60s, obi_dev_300s} × pctile thr {75,90,95} × size {10,50,200} × cap {50,200,1000}, s≤cap; 20 in / 4 out; REALISTIC delays + in-flight lock; per-game metric):
- **`wc_sweep_r250_r204/`** = current behavior (free_budget off). **`wc_sweep_r250_r204_fb/`** = `--free-budget` (re-enables the pre-`33fe652` over-budget reduce-only netting, gated behind the new `free_budget` flag).

**Contamination fix (why the first re-run was wrong):** `run_shard`'s `if f.exists(): continue` SILENTLY SKIPPED 48 stale `obi_dev_60s/300s` result files left in the dir from a Jun-18 sweep on older code → the collect mixed fresh `obi_dev_15s` with stale 60s/300s. Verified by bisect that `33fe652` ("clip entry to position-limit room; **drop over_budget guard**") caused a regime change (KORCZE 724→18 fills, +575→−202) — the dropped over-budget **netting** (not the clip) is the cause: with size 200/budget 250 the budget binds after ~1 fill, so old code churned reduce-only netting all game, new code goes idle. Deleted the 48 stale files + re-ran clean.

**Result — free_budget ON is robust, OFF is not:**

| | best per-game OOS config | IN/g | OUT/g |
|---|---|---:|---:|
| **free_budget ON** | **obi_dev_15s · thr 0.275094 · s200 · c1000** | **+66.1** | **+91.6** |
| free_budget OFF | obi_dev_15s · 0.275094 · s200 · c1000 | −13.3 | +348.9 |

Same config: free_budget turns an OOS-luck outlier (negative IN, inflated 4-game OUT) into a both-sided winner. OFF has NO s200/c1000 config with both IN & OUT positive (all are negative-IN OOS outliers); ON's whole top tier is both-sided positive.

- **SELECTED (free_budget): `obi_dev_15s` thr 0.275094 · s200 · c1000 · `free_budget:true`** → `studies/wc_best_config_r250_r204_fb.json` (collect now persists `free_budget`/`liquidate_no_alpha` so the `--config` loader reproduces the swept strategy — fix for the config round-trip gap).
- Note: `liquidate_no_alpha` flag added (85'+ unconditional liquidation) but NOT swept; default off.

## Jun 18 — V2 ORDER ENDPOINT MIGRATION + corrected r204 sweep
**Kalshi removed the V1 `/portfolio/orders` mutation endpoints (Jun 18–25 window)** — `POST` now returns `410 deprecated_v1_order_endpoint`. Migrated `api.create_order`/`cancel_order` to the **V2 event-order endpoints** (`POST`/`DELETE /portfolio/events/orders[/{id}]`); kept the `(side, action, price_cents)` interface, translated to V2 YES-leg `bid`/`ask` + fixed-point string price + required `time_in_force`/`self_trade_prevention_type`; `ProdExchange.place` parses the flat V2 response (`order_id`). Read endpoints (`GET /portfolio/orders|positions|fills|balance`) unaffected; we don't use amend/decrease/batch. Commit `705866b`.
- **Recorder now subscribes to the private `fill` channel** (commit `698bba0`) so recordings carry which trades/orders are the account's own.

## Jun 18 — Corrected r204 sweep — PER-GAME metric is the reporting standard
Restricted obi_dev grid, finalized at **47/48** combos (the 48th — `obi_dev_300s` t0.959703 s10 c200, a light ~2-min combo — hung on shard 2 with no output for 35 min; killed, non-competitive; its s10_c200 group tops out at OUT +102 so it can't change the winner [REVIEW: possible replay edge-case loop]). Setup: alphas {obi_dev_60s, obi_dev_300s} × percentile thresholds {75,90,95} × size {10,50,200} × cap {50,200,1000} (s≤cap), **20 in / 4 out** chronological, **$250 per-game budget**, realistic delays (ack22/pub28/fill16 + 20ms forward) + in-flight lock.

**Threshold-usage fix (why the OLD sweeps were flawed):** percentile skew-thresholds are now computed on EXACTLY the 20 in-sample games via the game-set-keyed `threshold_cache` (HIT verified, `thr_bc5b1ef49bac.json`), NOT a hardcoded/fixed first-N set. The earlier 12/8 sweep computed thresholds from only ~4 WC games — inconsistent with its own split — so its threshold grid was mis-scaled. r204 corrects this.

**Per-game averages (summed totals over-weight the tiny 4-game OOS sample — use per-game):** filtered to per-game in-sample net > $50, only 2 configs clear it:

| alpha | thr | size | cap | IN/g | OUT/g | IN rn/g | OUT rn/g | fills/g |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **obi_dev_60s** | **0.362887** | **200** | **1000** | **+59.9** | **+44.5** | +62.8 | +46.3 | 370 |
| obi_dev_300s | 0.43991 | 200 | 1000 | +59.2 | −1.8 | +61.0 | −0.8 | 382 |

- **SELECTED (per-game robust): `obi_dev_60s` thr 0.362887 · s200 · c1000** → +$59.9/game IN and +$44.5/game OUT (≈24% / 18% on $250), realized-net positive both ways — the ONLY config with a consistent per-game edge IN≈OUT.
- Raw "best OOS net" (collect's auto pick, `studies/wc_best_config_r250_r204.json` = `obi_dev_300s` thr0.43991 s50 c1000): summed OUT +413 but only **+$1.4/game IN** (20 games) vs +$103/game OUT (only 4 games) — flat in-sample, almost certainly OOS luck. NOT chosen. Supersedes the earlier (flawed-threshold) pick `obi_dev_300s` thr1.30588 s50 c1000.
- Full ranked table: `sims/wc_sweep_r250_r204/`; reproduce via `wc_sweep.py --collect --budget 250`.

## Jun 17 — Decoupled logger + timing instrumentation BUILT & VALIDATED
Architecture (`research/hft/run_live.py` launcher): ONE process discovers the universe once, spawns TWO children sharing a `multiprocessing.Queue` — **trading** (`live_mm.build_and_run`, does NO file I/O, computes only the decision alpha) + **logger** (`live_logger.logger_main`: own WS for the private `fill` feed → `private_feed.jsonl.gz`; drains the queue → `orders.jsonl`/`decisions.jsonl`). Main stamps 6 timestamps per order in a per-event context (`MMSimConsumer._evt`, gated on the emitter so sim is inert) and emits AFTER the order is placed (off the critical path). Logger writes records VERBATIM (all values strategy-sourced).
- **Paper smoke (`run_live.py --paper`, GHAPAN ~1.8 min, no orders sent)**: launcher spawned both processes cleanly; logger wrote **11,179 decisions + 132 orders** (68 new / 64 cancel); **0 timing-monotonic violations** (read_ts ≤ strategy_start ≤ alpha_start ≤ sent_to_router ≤ router_done); only snapshot events lack exchange_ts (6 orders / 3 decisions). Sample in `sims/paper_smoke_test/`.
- **Sim stays bit-identical** after all instrumentation: KORCZE obi_dev_60s net 317.4560 / 575 fills (instrumentation fully inert without an emitter).
- TODO: `analyze_live.py` (recompute alpha from the recorder's public feed + compare to the logged decision alpha; timing/fills checks).

## Jun 17 — SimExchange (sim mirrors the exchange self-feed): equivalence VALIDATED + realistic-delay re-basing
Built `research/hft/exchange.py` so the sim injects our own orders into the SAME public+private feed messages the exchange emits; consumer is feed-driven; `live` flag removed (see codebase_notes 2026-06-17). **Equivalence (delays=0) vs the pre-change baseline** (best config obi_dev_60s thr0.176139 s200 c5000, $1000, 20 WC games): **19/20 net/realized/fees/fills/contracts BIT-IDENTICAL**; HTISCO net off by 1e-4 (sub-cent unrealized-mark float-ordering; everything else identical). Per-tick market-only invariant clean on all 20. Harness: `scratch/exchange_equiv_test.py` (capture/diff); `equiv_before.json`/`equiv_after.json` in studies/.
- **Root-cause bug found+fixed during equivalence**: a recorded MARKET delta whose magnitude exceeds the market portion was draining our commingled injected order → fixed by clamping the market portion at 0 in `MarketView.apply_delta(is_own=False)` (a participant's cancel removes only their own depth). NEDJPN went from a 3-fill/−31 divergence to bit-exact.
- **Realistic-delay impact (`exchange.REALISTIC_DELAYS`: ack 22ms / pub 28ms / fill 16ms + in-flight lock binding)**: obi_dev_60s 20-game **realized-net +7498 (delays=0) → +3498 (realistic), −53%**; net +7358 → +2974; 10626 → 9138 fills. The optimistic synchronous sim materially overstates PnL (cancel-replace costs a round-trip; fills learned ~16ms late). Default delays stay 0 (reproducible); realistic is opt-in.

## Jun 17 — REALISTIC sweeps: $250 RUNNING (reprioritized), $1000 PAUSED — 12/8
`run_wc_sweep.sh <budget>`, 8 shards, 18h, general+GPU. Full 372-combo grid (blind/obi/agg_ratio/tfma_ratio/obi_dev_{5s,60s,300s}), **12 in-sample / 8 out-sample** (20-game dataset), **realistic execution**: SimExchange feed delays (ack 22 / pub 28 / fill 16 ms) + in-flight lock + 20ms forward fill + **ungated decide-from-view-every-event** requote (unsupported levels pulled via market-only `view.depth`). ~2x slower than optimistic wc_sweep_88.
- **$250 = 8519978 → sims/wc_sweep_r250_obi_tok/ — DONE 252/252 (Jun 18 07:41)** (obi-only, token rate limiter place10/cancel2/100-per-sec, 12/8 split, realistic delays + 20ms forward). **OVERALL best-in (→ `studies/wc_best_config_r250.json`): `obi_dev_300s` thr 0.457912 s200 c5000 → IN net +968.7 / OUT +439.4 (realized-net IN +976.4 / OUT +467.9 — POSITIVE OOS).** Per-strategy best-in→OOS: obi_dev_300s +439, obi_dev_60s(thr0.378689 s200 c200) +141, blind −214, obi −176, obi_dev_5s −389. CAVEAT: several **obi_dev_5s thr0.185599 s200** configs have much higher OOS (+804/+857) but lower IN — best-in selection (principled, avoids OOS overfit) picks obi_dev_300s. **Selected config (user choice, Jun 18) = `obi_dev_300s` thr 1.30588 s50 c1000 @ $250** — chosen OVER the auto best-in (s200 c5000) for tighter IN≈OUT (671/470), higher realized-net OOS (+556), and smaller size.
- **$1000 = 8510376 → sims/wc_sweep_r1000/ — PAUSED** at 9/372 (cancelled to free slots for $250; partial results saved, auto-skip on resume). Re-launch `sbatch --exclude=babel-q9-28,babel-s5-28 run_wc_sweep.sh 1000` after $250 (picks up the obi cache automatically — new processes).
- **obi cache (2026-06-17, VERIFIED bit-identical)**: `BookSide._ver` mutation counter (orderbook.py, bumped on apply_delta/load_snapshot); `alphas.py` `_obi`/`_pair_obi` memoize on the book-version tuple (obi was computed twice/event for obi_dev: on_book EMA + value_of). 5-game realistic check (GERCUW/CIVECU/HTISCO/AUSTUR/KORCZE, obi_dev_60s s50 c1000): all net/realized/fees/fills/contracts IDENTICAL (abs diff <1e-9), **189.5s → 158.2s (~16.5% faster)**; the heavy obi_dev configs (most of the sweep's wall-clock) benefit most.
These (realistic) supersede the optimistic wc_sweep_88 (overstates PnL ~2x). Also cancelled the earlier 8/8 run 8508916.

## Jun 17 — wc_sweep_88 re-run COMPLETE (refactored MarketView/OrderRouter code)
SLURM array 8463725 finished 372/372. `--collect` → best config **obi_dev_60s thr=0.176139 s200 c5000** (IN net +4675 / OUT +2638; realized-net IN +4711 / OUT +2649). Re-run captured the `--ladder`/`--depth_quote` removal + cancel-before-replace + out-of-band liquidation. Old partial pre-refactor results parked at `sims/wc_sweep_88_prerefactor_partial`. NOTE: these are the OPTIMISTIC (delays=0) numbers — see the realistic-delay re-basing above.

## Jun 16 — normalized-flow + obi_dev sweep (wc_sweep_88), 8/8 chronological, $1000 budget [SUPERSEDED by Jun 17 re-run]
`wc_sweep.py` on 16 games (train first 8 / test next 8, chronological), **$1000 budget (now standard for all sweeps)**, post unsupported-level + EPS fixes. Alphas: blind, obi, **agg_ratio / tfma_pw_ratio** (net/gross flow imbalance ∈ [−1,1], the scale-free obi-analog — new gross EMAs in AggFlowMA/TradeFillMA), **obi_dev_{5s,60s,300s}** (obi − obi_ma). 372/372 combos (first run timed out at 222 — sweep is slow ~22s/game-run with obi_ma tracking + feps; bumped to 6h, filled).

Per-alpha best-in (by IN net) → OOS:
| alpha | IN net | OUT net | IN rn | OUT rn |
|---|--:|--:|--:|--:|
| blind | +963 | +2610 | +862 | +2730 |
| obi | +588 | +102 | +561 | +406 |
| agg_ratio | +2137 | +2148 | −829 | −243 |
| tfma_ratio | +308 | +238 | −1032 | −1212 |
| obi_dev_5s | +2734 | +2126 | +2832 | +2298 |
| **obi_dev_60s** | +4310 | +3126 | +5267 | **+3164** |
| obi_dev_300s | +3530 | +2526 | +3445 | +1949 |

- **Normalization FIXED transfer (user hypothesis confirmed)**: raw agg/tfma blew up OOS (raw tfma +3998 IN/−4014 OUT); normalized agg_ratio +2137≈+2148, tfma_ratio +308≈+238 — consistent IN/OUT, no overfit. BUT realized-net negative (agg_ratio −243, tfma_ratio −1212 OUT) → normalized flow has NO usable realized edge (the +net was unrealized directional mark). Normalization was diagnostic — removed the illusion.
- **Marginal value over blind (realized-net OUT, the honest metric since blind is +2730 OOS)**: ONLY **obi_dev_60s beats blind** (+434 rn / +516 net). obi_dev_5s −432, obi_dev_300s −781, obi −2324, agg_ratio −2973, tfma_ratio −3942. So obi_dev_60s (obi−obi_ma@60s) is the only alpha adding value over no-skew quoting OOS; 60s is the sweet spot (> 5s, > 300s); obi-deviation > raw obi.
- **CAVEAT**: blind (no skew, $1000) captures ~+2730 rn OOS — most of every config's OOS PnL is the common bounded-directional/spread component on these 8 test games, not alpha. obi_dev_60s's +434 marginal over 8 games is real but modest, could be partly luck. Next: forward-test obi_dev_60s on held-out games + directional/spread decomp.

## Jun 16 — obi config forward-test on 4 NEW games (held out from sweep): NEGATIVE
Ran the full-sweep obi winner (obi thr0.440205 s200 c5000, budget off, football) on the 4 JUN15 games added to the dataset (now 16 total) — KSAURU, IRINZL, ESPCPV, BELEGY — none in the sweep's train OR test (clean forward-test). `scratch/forward_test.py`.
| game | net | realized-net | fills |
|---|--:|--:|--:|
| KSAURU | +567.8 | +1236.5 | 756 |
| IRINZL | −932.6 | −491.9 | 1510 |
| ESPCPV | −4130.5 | −1521.8 | 295 |
| BELEGY | −261.7 | −56.3 | 759 |
| SUM | **−4757.0** | **−833.5** | |
- **obi LOST on fresh data** (net −4757, realized-net −834 — not just unrealized). ESPCPV (Spain blowout) dominates the loss: the accumulated obi-directional position got run over. Strongly confirms the directional-luck thesis — the "winner" went negative on the very next 4 games. obi's edge is NOT robust game-to-game.

## Jun 16 — agg/tfma do not transfer (raw flow is volume-scaled) → normalization idea
- **Root cause (user)**: obi is a scale-free RATIO ((bid−ask)/(bid+ask) ∈ [−1,1]); agg & tfma are RAW signed flow magnitudes that scale with game interest/volume, so in-sample thresholds don't transfer (unreachable on quiet games, trigger-happy on busy ones) → explains their OOS blowup vs obi holding up.
- **Fix (causal, non-forward-looking) → ideas.md**: make agg/tfma ratios like obi — **net/gross flow ratio** = signed-flow EMA / unsigned-flow EMA (same HL) ∈ [−1,1] (recommended; one extra unsigned EMA). Alternatives: divide by a slow causal activity EMA; causal online z-score; per-standing-depth; trailing-window percentile; tanh(agg/causal-scale). Avoid whole-game/cross-game hindsight normalizers. Pending user go-ahead to implement `agg_ratio`/`tfma_ratio` + re-sweep.

## Jun 16 — full alpha sweep (wc_sweep_66), 6/6 chronological, post unsupported-level + EPS fixes
`wc_sweep.py` (budget off → position-limit binding), train=first 6 / test=next 6 WC games, alphas blind/obi/agg(300s)/tfma(pw 300s)/agree_agg_1s (obi gated by 1s-agg sign). Run AFTER: the unsupported-level guards (strategy never rests where displayed≤0) AND the EPS=1e-6 float-robustness fix (de-saturated obi — see codebase_notes). 249/252 combos (3 unfinished, not among the best). Artifacts: sims/wc_sweep_66/, studies/wc_best_config_66.json.

Per-alpha best-in (by IN net) → its OOS:
| alpha | best-in | IN net | OUT net | IN rn | OUT rn |
|---|---|--:|--:|--:|--:|
| blind | t999 s10 c10 | −21 | −26 | −21 | −24 |
| **obi** | t0.44 s200 c5000 | +476 | **+2960** | +2125 | **+1674** |
| agg | t82544 s200 c5000 | +2812 | −391 | −2539 | −4431 |
| tfma | t536400 s200 c5000 | +3998 | −4014 | −1703 | −5730 |
| agree_agg_1s | t0 s200 c1000 | +445 | −88 | +273 | +129 |

- **obi is the ONLY alpha with a real edge**: positive OOS net (+2960) AND realized-net (+1674); only alpha with positive realized-net both IN and OUT.
- **agg / tfma are mark-to-market illusions**: realized-net NEGATIVE even in-sample (agg −2539, tfma −1703) — their +IN net is open directional inventory marked favorably at FT, reverses hard OOS (tfma −5730 rn OOS). Overfit, not edges.
- **Gating obi on 1s-agg (agree_agg_1s) does NOT help**: ~breakeven OOS (rn +129) ≪ raw obi (+1674). Agg-confirmation discards good obi signal (same as the momentum-gating result).
- **blind loses** (no skew → no edge) → the skew does the work. Wins+blowups concentrated in big size/cap → still directional-leverage, not pure spread (consistent with pnl_decomp ~88% directional).
- vs the EPS-fix impact: obi OOS ≈ +2960 here vs +3104 in the pre-EPS obi-only sweep — obi conclusion robust to the fix; thresholds shifted (obi p50 0.84→0.44) from de-saturation.

## Jun 15 — passive OBI sweep, chronological 6/6 train-test (FootballStrategy, $1000)
Re-ran `wc_sweep_obi.py` (OBI-only, no gating, $1000 deployed-capital budget, WCStrategy/`football`) with a CHRONOLOGICAL split: train = first 6 WC games (KORCZE, MEXRSA, CANBIH, USAPAR, BRAMAR, HTISCO), test = next 6 (QATSUI, AUSTUR, CIVECU, GERCUW, NEDJPN, SWETUN). Uses the eval_buffer default forward latency. 36 combos thr{0.528,0.840,1.0}×size{10,50,200}×cap{10,50,200,1000,5000}. Artifacts: `sims/wc_sweep_obi_66/` + `studies/wc_obi_budget_best_config_66.json` (prior 4/4 run preserved). (Note: 6/7 split boundary falls within JUN13's 3 games — intra-day order is filename/alphabetical, the codebase chronological proxy.)
- **OOS-POSITIVE across the board** (every train-positive config is also test-positive). **best-in (select on train net): obi thr=0.84 s=200 cap=5000 → IN +992.1 / OUT +3103.8 net; realized-net (after fees) IN +2147.6 / OUT +2836.7** (in_fills 2997). Others: thr1.0 s200 c5000 OUT +3263 (highest OUT); thr0.528 s200 c5000 OUT +2306; thr0.528 s50 c1000 IN +602 / OUT +1085.
- **CAVEAT — directional, not spread.** OUT net ≈ 2–3× IN net consistently, and the biggest size/cap configs win → signature of directional inventory P&L, matching the earlier `pnl_decomp` (~88% directional / ~12% spread). 5 of 6 test games are JUN14; the +$2.8k OOS is likely favorable inventory rides on a few trending games, NOT market-making edge.
- **Best-config per-game analysis (thr0.84/s200/c5000), `scratch/best_config_volume.py`:** profit is CONCENTRATED — KORCZE +$2335 carries IN; QATSUI +$1998, NEDJPN +$1276 carry OUT; most other games small/negative.
  - **Sharpe (per-game PnL, n=6, not annualized):** train net 0.15 / realized-net 0.37; test net 0.56 / realized-net 0.83; all-12 net 0.35 / realized-net 0.54. Modest, inflated by 2–3 winners.
  - **Volume:** our sim volume is only **0.04–0.21% of market volume** every game (overall 13–43M contracts/game vs our 10–80k) → negligible participant, lots of size headroom, fills don't move price.
  - **Mean PnL/our-volume ratio:** IN net/vol **−0.0127** $/ct (realized-net **+0.0016**); OUT net/vol **+0.0358** (realized-net **+0.0258**). The IN mean ratio is NEGATIVE despite +$992 IN total — a typical game loses per contract, KORCZE saves the aggregate. Realized edge ≈ breakeven/contract IN, ~+2.6¢/contract OUT. Per-contract edge is thin + concentrated, not a stable MM edge.

## Jun 15 — latency vs the cross-leg goal arb: assessment
The per-goal stale edge decays in **~20–100ms** (median stale window ~58ms, see Jun 14). A REST order round-trip to Kalshi is ~20ms at best (mostly exchange-side gateway + matching-engine processing, which is location-independent); the market-data feed adds a further ~15ms before an event is visible. So even with a low-latency network path the assembled reaction loop (event visible → our order live) is ~30ms+ — most of the median window is gone before we can act. `arb_sim` was already OOS-negative at a 20ms latency assumption, so latency is **not the rescuable lever** for this arb on REST. The only untested lever is Kalshi's FIX gateway (gated on the Premier tier), which could trim the REST-gateway slice but not the data-feed or matching floor. Recommendation: stop pursuing latency for this arb.

## Jun 15 — cross-leg latency-arb backtest (arb_sim.py): negative so far
- **arb_sim.py**: sweep-detector (best price moves L levels in T ms) -> leader leg; aggress stale followers (other-team ~G/2, TIE ~1 tick) at t_detect+20ms (PassiveFillEngine FORWARD_DELAY); passive liquidation at the repriced fair, aggressive fallback flatten after liq_timeout; taker entry / maker exit fees from meta; $1000 budget.
- **Detector confusion (T30/L4, scratch/arb_eval.py)**: 100% RECALL of significant goals (>=5c stable move): TP 13 / FN 0; goal-precision 13/14. But **95 SPURIOUS fires** (near no goal) — precision against false alarms is the binding constraint, not recall.
- **Arb available**: ~$69k edge x size notional at t0 across 13 goals (tens of thousands of contracts/goal, edges 4-88c) BUT decays in ~20-100ms; only small quantities (<3k) persist 200-500ms. $1000 budget caps capture far below this.
- **(T,L) sweep {10..50ms}x{3..7} (wc_arb_sweep.py), in=first4/out=last4, $1000, 20ms**: NO config OOS-positive. best-in T20/L3 +306 IN / **-1049 OUT**; all OUT -86..-1318 (~full-budget blowups from spurious/wrong-direction full-budget trades, e.g. AUSTUR -960).
- **Fix #2 per-event sizing (--per-event-cap)**: bounds tail (AUSTUR -960 -> -48 at $50 cap, linear in cap) but still net-negative IN -45 / OUT -50 (T30/L5/$50). Necessary not sufficient.
- **Next**: fix #1 = trade-volume sweep detector (levels wiped BY TRADES, not best-price moves) to kill the 95 spurious fires. Fix #3 latency: FIX gated behind Premier tier / institutional@kalshi.com (FIXT.1.1/FIX50SP2, NewOrderSingle D).

## Jun 15 — arb v2 (trade detector + Dixon-Coles attribution): still OOS-negative at every (T,L)
- **Detector**: trade-based level-wipe (>=L distinct trade levels in <=T ms). Confusion (scratch/detector_sweep.py): recall 0.81-0.88, but precision caps ~0.33 (best trade L7: ~25-28 FP for 13 TP). T barely matters; L is the lever; local clock slightly beats exchange (ts_ms is ms-resolution).
- **Attribution model**: in-play **Dixon-Coles rho=-0.13** beats independent Poisson for predicting the post-goal reprice — predicted-post MAE **3.7c vs 5.5c** (indep), pre-goal calib 0.5c; literature rho (not fit) is best (generalizes). Equalizer residual is mixed: over-estimates draw on mid-game equalizers (DC fixes), under-estimates on LATE equalizers (market prices late level-score draw higher than static rho). scratch/poisson_fit.py, dc_fit.py.
- **arb_sim v2** = trade detector + DC score-based attribution (approved direction cases) + **10-level predicted-delta cap** + per-event $100 cap + $1000 budget + 20ms latency + passive liq w/ aggressive fallback.
- **PnL sweep (wc_arb_sweep.py, plots/arb_pnl_sweep.csv)**: NO config OOS-positive. T irrelevant; L3 = FP catastrophe (FP 209-217, in -1430..-1583); L7 = FP-controlled (FP ~21-27) but in +136..+141 / **out -148..-194**. best-in T8/L7 +141 / -194. **Precision-recall squeeze**: recall-friendly low-L drowns in false fires; FP-controlled high-L has capped per-goal edge (~$10-40) < cumulative FP cost. Caps + 20ms latency keep TP value below false-fire losses.
- **VERDICT**: cross-leg arb not viable at $1000 + 20ms with this detector. Levers: (1) cut latency (FIX — captures more of the 20-100ms-decaying edge per goal); (2) cross-leg confirmation gate (trade only when followers visibly stale vs leader's realized move, beyond the detector's ~33% precision).

## Jun 14 — cross-leg arb discovery + passive PnL decomposition + BRAMAR fix
- **BRAMAR data fixed**: dataset/KXWCGAME-26JUN13BRAMAR was truncated at +27' (a later merge clobbered the good per-game file with the truncated morning-session copy). Full game (−16.4'→+123.2', through resolution) recovered from `dataset_premerge_backup/`; broken copy saved to `dataset_broken_backup/`. Seamless 27' morning→evening join. (Merge pipeline bug — root cause not yet fixed.)
- **WC dataset sanity (all 8)**: END all +0.9'..+3.8' after FT (reach resolution) ✓. START all only ~12–18' before KO, NOT T−1h — recorder discovers/subscribes WC games ~15' pre-KO (confirmed in BRAMAR raw); pre-game −60'..−15' never captured, not recoverable. Future recorder should subscribe at T−1h.
- **obi+mom sweep (agree_om_{1s,5s,10s,30s}, $1000 budget, 144 combos)**: best om_30s thr0.528 s200 cap5000 → IN realized-net +1560 / OUT +204. ALL mom-HL variants far below plain obi OUT +1071 (om_5s +280, om_10s +60, om_1s −7.5). **Gating obi by momentum (even 1s) hurts OOS.** (OOS on truncated BRAMAR; refresh via Task A.)
- **Goal→odds delay (scratch/goal_delay.py)**: Kalshi book moves ~4s BEFORE ESPN goal wallclock (median onset −3.8s) — ESPN logs late; ESPN unusable as a trade signal.
- **Cross-leg latency arb (scratch/leg_arb.py)**: at a goal one leg leads; followers stale **median 55ms lag**, **~12¢ edge** (max 88¢), size median 230 (≤8.8k), stale window **median 58ms**. ~half capturable (window≥50ms). Risk-free-ish IF order RTT < window. → ideas.md [REVIEW].
- **Passive PnL decomposition (scratch/pnl_decomp.py)**: obi best @ $1000 gross PnL is **~88% DIRECTIONAL / ~12% spread** (IN 14/86, OUT 11/89). Spread small but always +ve (+$45–235/game); directional large+volatile (wins correct reads, loses MEXRSA/CANBIH). The passive "+OOS" is mostly directional luck on 2–3 games, not market-making. → ideas.md [REVIEW].

## Jun 14 — restricted OBI-only sweep under a REAL $1000 budget (budget-realistic)
- `wc_sweep_obi.py` (36 combos, SLURM array `run_wc_sweep_obi.sh` 4 shards, job 8349734): OBI only, NO gating, **budget=$1000** (global deployed-dollars cap → capital binds, not the position limit; contrast the full sweep's 1e9). Axes: thr ∈ obi |alpha| pctiles {0.528(p50), 0.840(p75), 1.0(p90+)} × size {10,50,200} × cap {10,50,200,1000,5000}, s≤cap. Separate artifacts: `sims/wc_sweep_obi/`, `studies/wc_obi_budget_best_config.json`, plots `plots/wc_obiviz/`.
- **Best-in: obi thr 0.840 (p75), s200, cap5000 → IN net +1392 / OUT net +978; realized-net IN +2119 / OUT +1071.** (cap5000 here ≈ unconstrained — the $1000 budget binds first.)
- **KEY: the obi edge survives a real $1000 capital cap.** OOS realized-net +1071 vs the budget-off full-sweep +1378 — only modestly lower. Round-trip spread capture doesn't need large deployed inventory, so the cap costs little realized PnL.
- **Budget also tames unliquidated inventory**: net (+978 OOS) ≈ realized-net (+1071 OOS), gap nearly closed — vs the budget-off run where net +2731 ≫ realized-net +1378 (huge open inventory marked at mid). $1000 cap → far less settlement risk. (Per-game open-contract magnitudes from `plots/wc_obiviz/open_positions.csv`.)
- vs full sweep selection: budget-off picked thr0.528(p50); budget-on picks thr0.840(p75) — under capital scarcity, quoting less aggressively (skew only on the larger 75th-pct obi moves) is better.

## Jun 14 — WC FootballStrategy sweep (refined, WC-percentile thresholds + agg gate)
- `wc_sweep.py` (216 combos, 3 SLURM shards, relaunched with --resume after a cancel → resumed 190→216): WCStrategy (no-trade until KO+5min → main → liquidate-only at 85min, square_off=False default). Axes: alpha ∈ {blind(obi t999), obi, agg_300s, tfma_pw_300s, agree_agg} × thr = in-sample |alpha| percentiles {50,75,90,95,99} (`wc_thresholds.json`) × size {10,50,200} × cap {10,50,200,1000,5000}, s≤cap. **Budget OFF (1e9) so the POSITION LIMIT binds, not capital.** In-sample = first-4 WC games, out-sample = last-4 (chronological).
- **OVERALL best-in = obi, thr 0.528 (p50), s200, cap5000 → IN net +3535 / OUT net +2731 (realized-net IN +2090 / OUT +1378).** First config positive OUT-of-sample on BOTH net and realized-net → confirms the WC-only passive thesis on a real train/test split. Written to `studies/wc_best_config.json`.
- Per-strategy best-in → out (net | realized-net): **blind** +243/+481 | +312/+279 (symmetric MM with limits already OOS-positive, small); **obi** +3535/+2731 | +2090/+1378 (winner, ~5x blind on realized-net); **agg** +886/+1302 net but rn −743/−1694 (positive net is m2m on huge open inventory — not real); **tfma** +697/−52 | +240/−127 (does NOT generalize); **agree_agg** +2246/+652 | +53/−1184.
- **Q2 (can agg gate/refine obi orders?): NO.** Gating obi by agg sign (`agree_agg`) is strictly worse than plain obi everywhere — net (+2246 vs +3535 in, +652 vs +2731 out) and realized-net (rn collapses to +53 in / −1184 out vs obi +2090/+1378). The sign gate discards profitable obi-driven fills; plain obi is the standalone edge. Do not gate obi on agg/tfma/mom.
- **Net vs realized-net gap** = unliquidated inventory marked at mid at game end. realized-net is the honest number; obi is positive on it both in and out. Open-position magnitudes quantified by `build_wc_bestviz.sh` (Q5, per-game `plots/wc_bestviz/open_positions.csv`).
- **Caveat for a $1000 budget**: best config's cap=5000 / s=200 is NOT capital-feasible at $1000 (budget was deliberately off to isolate the limit). Best smaller-cap obi: thr 0.528, s50, cap1000 → IN +1000 / OUT +622 net, rn +869/+408 — the realistic candidate pending a budget-on rerun.

## Jun 13 — lead-lag CCF of 60s-HL signals
- `lead_lag.py` → `plots/lead_lag_60s.csv` + `.png` (job 8344769): CCF rho_XY(tau)=corr(X[t],Y[t+tau]) between tfma_60s/agg_60s/obi_ma_60s, 1s grid per game (ff, gaps>60s dropped), per-game demeaned, pooled. tau>0 => X leads Y. Reuses collect_samples.
- **tfma~agg contemporaneous** (peak tau=0, r=0.22 ALL / 0.24 WC; both are aggressive-flow views; tiny agg-leads tilt). **agg LEADS obi_ma by ~60-120s** (ALL: neg at tau<0, broad peak ~0.085 at +60..+120 — flow precedes resting-book imbalance). **tfma vs obi_ma**: weak monotone NEGATIVE (-0.10@-300s -> -0.01@+300s), no clean peak — not lead-lag coupled (the |r| argmax at -282s is just the monotone edge / slow drift, NOT a real lead). Caveat: 60s-EMA series share slow common components, so CCFs sit on a positive long-lag baseline — interpret the near-zero asymmetry, not absolute long-lag r.

## Jun 13 — strategy-faithful corr table (v2) + analysis modules
- New reusable modules: `filter_strategy.py` (FilterStrategy = mm_sim `_desired_sides` market gate: bid>=0.05, ask<=0.95, 0.005<=spread<=0.01; mask()/allows()), `forward_price.py` (forward_fields adds bid_<h>/ask_<h> via exact searchsorted over the full touch series BEFORE filtering). Extended alpha HLs in alphas.py (tfma/agg→1800s incl 120s; mom→600s; obi_ma→900s; now 54 alphas) and horizons→[1..1800]s.
- `strat_corr_table.py` → `plots/alpha_return_corr_strat.csv` (job 8344590, 15 min, 428M peak): per-market trigger sampling (trade / top-of-book change, no 1Hz throttle), leg-signed pair alphas, exact forward price, FilterStrategy gate, RELATIVE return (p[t+h]-p[t])/p[t] (mid). Quotable rows: MLB 505k, WC 271k, ALL 777k.
- vs the flawed v1 (1Hz subsampled, approx fwd, absolute cents): signals ~2x larger and new structure visible. **obi** dominant short-horizon (ALL r=+0.126 @5s; WC strong+persistent +0.14..+0.16 across ALL horizons). **Long-horizon REVERSAL**: tfma_pw and mom strongly negative at 600-1800s (WC tfma_pw_60s r=-0.63 @1800s; ALL mom_600s -0.21 @1800s); raw tfma_1800s is instead POSITIVE in WC (+0.36) — raw vs price-weighted flow diverge at long HL. agg_pw_300s positive bump (+0.05 ALL). Caveat: 900-1800s samples heavily overlap (triggers seconds apart, 30-min windows) → SEs badly understated; descriptive only; WC rests on 4 games.

## Jun 13 — league × horizon alpha-correlation table
- `league_corr_table.py` (reuses collect_samples) → `plots/alpha_return_corr.csv`: Pearson r of each alpha (all HLs) vs cents forward pair-mid return at horizons {1,5,10,30,60,300}s, per league + ALL. Forward returns on the 1 Hz sampled mids (same as build_xy). Job 8342663, 9 min.
- Dataset = 35 MLB game-pairs + 4 WC games (12 per-market soccer legs); no NBA/NHL/NFL present. r_1s degenerate (≤ the 1s sample throttle → near-constant fwd; blank for WC where all 1s gaps exceed throttle).
- **obi is the standout** both leagues (MLB r≈0.078 @5s, WC r≈0.069 @60s). **MLB**: raw tfma/agg > price-weighted; agg HLs strengthen at longer horizons (agg_pw_300s r=0.072 @300s). **WC**: opposite — tfma_pw long-HL strengthens with horizon (tfma_pw_300s r=0.058 @300s), and **mom is negative @30-300s (reversal/flow-fade)**, consistent with the earlier soccer momentum-fade note. Cross-league sign/weighting differences argue against one pooled model.

## Jun 13 — replay player slider-rewind fix + rebuild
- **Bug (user-found)**: in book_player.py's HTML player, the scrub slider couldn't rewind while playing. `seek()` rebuilt the book at the target but left `lastSim` ahead of the new `idx`, so the next `loop()` frame's `simTarget = lastSim + dt*speed` fast-forwarded right back. Fix: `seek()` re-anchors `lastSim`/`lastReal` to the sought position. (Paused rewind already worked — the loop's idle branch resyncs lastSim each frame.)
- Rebuilt both players via `sbatch run_build_players.sh` (job 8341784) → reproduced the SAME two games: MLB KXMLBGAME-26JUN102138HOULAA (13.46M cts, ticks_20260610_183014) + WC KXWCGAME-26JUN11KORCZE (ticks_20260611_193554), now with the slider fix. Outputs in plots/players/.

## Jun 12 evening — PHANTOM-LEVEL BUG FIX + corrected WC reruns
- **Bug (user-found via replay player)**: BookSide.apply_delta kept float-residue levels (fractional count_fp doesn't cancel exactly) → phantom ~1e-13-qty best levels → optimistic passive fills (join phantom price, queue_ahead≈0), spread-gate misfires, distorted aggressive entry sizing. Fixed with 1e-9 epsilon (orderbook.py + player JS). ALL pre-fix sim numbers are suspect; tomorrow's morning routine re-baselines everything.
- **Corrected blind passive baseline**: MEX realized-net +78.6 (was +96.4, phantom-inflated), KOR **+50.1** (was −30.0 — phantom levels had been suppressing real quoting via fake crossed/narrow books; 2978 fills, $687 fees). **Positive on BOTH WC games** → passive thesis broader than "trending games only". Marked net +31.5/−19.7 with ~1000/1618 open cts at end — settlement handling matters.
- **Corrected aggressive family**: hybrid KOR uniformly worse (−178..−307); 3-leg spike KOR catastrophic (neg-only −2290, $1006 fees). Reinforces: off the main path; entry spread cap (+ ungated exits + mid-based stop) prerequisite to any further aggressive work (proposed, awaiting user go-ahead).
- MLB size/cap sweep relaunched on corrected code (4 general shards); stale old-book results deleted per user.
- **Corrected size/cap sweep (117 combos: 3 alphas × thr × s{100,500,1000} × cap{300,1k,3k})**: train-best all deeply TEST-negative — tfma_pw_300s thr31k/s100/cap1k +1078/−912; agg_300s thr49k/s100/cap3k +971/−515; obi thr1/s100/cap1k +521/−547. s100 dominates train and fails worst on test (fill-count amplifies selection bias); cap300×s≥500 structurally zero; test-positive cells anti-correlated with train (noise). Old-book "test ≈ −20" was phantom-masked; **MLB passive is decisively OOS-negative → WC-only focus hardened**.

## Jun 12 hybrid aggro-entry + passive-liquidation (tfma_pw_300s, limit 300)
- Mode: latched taker entry at alpha ±{10k, 25k}; passive quotes become exit-only (same-ticker netting at the touch); entry disarms once liquidation starts, re-arms only after alpha zero-cross.
- v1 (re-entry allowed while latched) ping-ponged: 80–146 fills, 4.4k–10.3k contracts, fees $69–166 — fixed with arm/disarm-on-peak logic.
- Fixed results (net): **MEX-RSA −59.67 (e10k, 8 fills) / −8.46 (e25k, 6 fills)**; **KOR-CZE −116.96 (e10k, 16 fills) / −131.48 (e25k, 15 fills, 600 cts stuck unliquidated, unreal −85)**.
- vs pure latched aggressive: MEX +33.6/+61.7, KOR −262/−277. Hybrid kills the trending-game win (passive exits sell back into the trend early, paying spread round-trip) but halves the choppy-game loss. Neither variant is positive on both games → aggressive/hybrid family stays off the main path; passive baseline remains default.
- **v3 TP/SL exits (user design)**: passive exit never worse than entry ±2¢ (rest above market and wait), aggressive stop at entry ∓{3¢, 7¢}. Net PnL (MEX / KOR / sum): e10k st3 −69/−44/−113; e10k st7 −80/−152/−232; e25k st3 −24/−65/−89; e25k st7 **+40**/−154/−114. Beats pure aggressive at every config (pure: e10k −228, e25k −215 summed) but still net negative on N=2 — KOR chop triggers repeated stop-outs. Aggressive/hybrid family remains a trending-game bet; off the main path pending more WC games.
- **Entry-threshold sweep {5k,10k,15k,25k,40k,60k} × stop {3¢,7¢}**: all 12 summed cells negative; best e15k/st3 −72. Threshold insensitive in 10k–40k band (KOR results identical — tfma spikes are large/discrete, same entry episodes trigger at all thresholds). e5k catastrophic on MEX (−227/−254, noise entries). MEX +40 only at {25k,60k}×st7 (same 1–2 trend entries). Confirms: game type, not threshold, decides the outcome.
- **v5 3-leg spike trade (user spec)**: own alpha > t_pos AND both siblings < −t_neg → buy YES spiking leg + NO both siblings, TP/stop exits. Full grid (t_pos {0.5k–5k} × t_neg {2k–40k}) + corner (t_pos {10k–50k} × t_neg {0.5k–2k}) + neg-only variant: ALL active cells negative (best −17.6 MEX single episode); KOR never triggers (sibling alphas never simultaneously negative — TIE alpha ~10× smaller); neg-only −394. Plots: plots/spike3_pnl.png, CSV plots/spike3_results.csv.
- **Event study (decisive)**: tfma_pw_30s signed buckets vs 120s fwd return, WC pool: |alpha|<10k mean-reverts (source of pooled r=−0.135 raw); |alpha|≥10k FOLLOWS THROUGH monotonically (+0.20/+0.31/+0.71¢ for 10–25k/25–50k/50k+; mirrored down for negative except −50k+ reverts +1.66¢ = capitulation marks bottom). BUT tail edge 0.2–0.7¢ < spread (1–2¢) + taker fee (~1.75¢) → aggressive can't monetize it; passive MM collects that same spread. t-stats inflated by overlapping 1s samples.
- **tfma_pw weighting flaw found (user-prompted /check-for-bugs)**: trade_fill_ma.py weights by TAKER-side price (p vs 1−p asymmetry, 19× at p=0.95) → biases alpha toward high-priced side. A/B at 30s/120s WC pool: taker-weight −0.053, symmetric YES-price −0.022, raw −0.135. Asymmetry is real but NOT the negativity cause (raw most negative — soccer flow genuinely contrarian in the body). Fix pending re-baseline decision.
- **v4 cross-leg condition (user spec)**: entry iff own tfma_pw > t AND all sibling legs < −t (symmetric for NO); no latch — re-entries allowed mid-liquidation; passive orders liquidation-only (TP +2¢, stop 5¢ unchanged). HL by max |corr| vs 120s return on WC pool: tfma_pw_30s (r = −0.053; ALL HLs negative — flow contrarian on soccer). Threshold sweep: t=1k −230; **t=2k +18.4 (MEX −46.8 / KOR +65.2)**; t=5k −46; t≥10k zero trades. Caveats: KOR profit is +69 unrealized on a 300-ct end-of-recording position; active t-range razor thin; in-sample tuned on N=2 → consistent with zero edge. Include in N=4 ledger (--aggro-cross flag in mm_sim.py).

## Jun 12 aggregation alpha (`research/hft/agg_flow_study.py`, user spec)
- Signed EMA over {trade, new resting order, PURE cancel} in YES space (6 cases); pure cancel = negative delta unmatched by a trade within 1s pending window (Kalshi docs confirmed: orderbook_delta has NO cancel/reason flag — inference is the only way). Weights 1/3 each; trades level-factor 1, new/cancel 1/k (k = rank from best bid).
- WC pool corr vs fwd return: agg_60s best for 2–3min (+0.022 @120s / +0.019 @180s), monotone in HL, right sign everywhere (vs tfma_raw_30s −0.024); still below OBI (+0.030).
- **Lead-lag CONFIRMED at matched HLs**: corr(agg_30s(t), X(t+lag)) asymmetric toward positive lags for both tfma_pw_30s (+10s 0.152 vs −10s 0.105) and OBI (0.105 vs 0.081) → agg leads both. (First-pass agg_300s-vs-30s tilt was a mechanical HL artifact.)
- Next: tune the 3 weights + combo regression with OBI; extend to MLB pool.
- **MLB eval (agg_mlb_eval.py)**: train-MLB corr — agg_300s +0.092 (best agg; monotone in HL), tfma_pw_300s +0.102, obi +0.234; **price-weighted agg NEGATIVE at all HLs ≥10s (agg_pw_300s −0.042)** — price weighting reintroduces the boundary bias; 1/k level weighting is correct. Trading (s500/cap1000, thr fit on train): ALL test-negative — agg_300s test_rn −25.2 (train +517), tfma_pw_300s −20.1 (+545), obi −38.4 (+579). agg is a valid leading signal but adds no OOS MLB edge; picture unchanged (WC passive baseline).

## Jun 12 MLB-vs-WC weight transfer (`research/hft/fit_transfer.py`)
- Pool fits (std. weights, 180s horizon): MLB n=209k corr 0.236 [obi_ma_1s +0.60, obi +0.33, mom_30s +0.13, tfma_pw_300s −0.05]; WC n=39k corr 0.055 [obi_ma_1s +0.13, obi +0.04, mom_30s −0.16, tfma_pw_300s −0.11]. **Cosine sim +0.49; momentum sign flips (MLB follows, WC fades)** — matches per-game cluster finding.
- WC traded with MLB weights (s500/cap1000): MEX net +25.9/+28.6 (t0.25/t0.5); KOR net +16.4/−44.5 but realized-net −64/−125 with negative markouts. Underperforms blind baseline on both games (MEX +96, KOR −30) → per-league models confirmed; no MLB→soccer transfer.
- Combos persisted: studies/transfer_{mlb,wc}_combo.json.

## Jun 12 sweep verdict (5-session dataset) — THESIS SHARPENED
- TEST realized-net median +71/max +80 across 43 budget configs, but per-league split: **WC +96.35 (MEX-RSA, N=1 test game, config-insensitive) vs MLB -16..-23**.
- Conclusion: passive MM prints on WC games regardless of exact alpha; MLB has no test edge yet. Focus = WC. Each new WC test game (USA-PAR/CAN-BIH today; 3 Sat; 5 Sun) is the validation that matters.
- Afternoon TODO: analyze_fills on MEX-RSA (+96 decomposition: markouts/spread/direction).

## Jun 12 study results (5-session dataset, ~35 games, 2 WC)
- **Pooled lasso retreats on soccer**: with KOR-CZE in validation, lambda->1.0, val corr 0.226->0.136, only obi_ma_1s survives (+0.13). Per-league models supported.
- **First meaningful TEST profit**: lasso combo thr 0.25σ -> TEST realized **+$73.02** (639 fills; train +268). Model ≈ thresholded smoothed book imbalance. LEADING CANDIDATE pending sweep comparison.
- Soccer momentum-fade: partial — 4/6 WC markets in the fade cluster (KORCZE-KOR and TIE broke pattern). USA-PAR + CAN-BIH today should settle it.

## Morning status (Jun 12)
- League-driven discovery CONFIRMED working: day recorder discovered NBA Finals G5 ($8.8M) + NHL G6 as pairs, USA-PAR + CAN-BIH ($3.7M each) as 3-market events.
- KOR-CZE fully captured overnight (in-game from 21:48); merging into dataset at ~07:35 when evening recorder auto-filters.
- Lasso (single-tfma family, MEX-RSA included): obi_ma_1s +0.62, obi +0.35, tfma_pw_300s +0.17, mom_30s +0.02; val corr 0.226.
- Per-game regimes (18 fit units): MEX-RSA's 3 markets form their own cluster — strongly momentum-CONTRARIAN (mom_30s −0.5..−0.7); MLB splits into high-vol flow-following vs calm book-imbalance regimes. League separates first, volatility second. Artifacts: studies/per_game_weights/{weights.csv,weight_regimes.png}.
- 3-leg WC arb (arb_scan.py): 6 real windows in KOR-CZE in-game (best ~$2.55), all ~0.1s — opportunistic adjunct only.
- Betfair port: RESOLVED-BLOCKED (not US-legal).

## Running now
- **Evening recorder** (SLURM 8318574, ends ~07:35 + auto-filter): **Korea–Czechia in-game** (10pm match; 63k trades/10.1M cts by 23:00) + late MLB. USA–Paraguay is Jun 12. **NHL Cup G5 missed tonight**: "Game 5: X at Y" titles broke the pair parser (fixed 23:10; effective for newly started recorders → NBA Finals G5 Sat + NHL G6 Sun covered). Writes T−1h-gated; auto-filters on exit.
- **Lasso pipeline** (srun, started 22:39): HL-per-family selection (corr vs 180s fwd return, fit games) → lasso with λ tuned on val games → threshold tuning at s500/cap1000. Output: `studies/lasso_combo.json` + threshold train/test table.
- **Scheduled recorders**: Jun 12/13/14, day (06:30) + evening (18:35) pairs, majors + KXWCGAME + KXINTLFRIENDLYGAME, top-18/top-10, 25k discovery floor.

## Dataset state
- `dataset/`: 3 filtered sessions Jun 9–10 (28 games, 45 markets, all ≥1M in-window contracts, T−1h trimmed).
- `dataset_incoming/`: Jun 11 day session (13/19 markets kept — incl. MEX–RSA + KOR–CZE, first soccer data). Merge into `dataset/` at next morning routine.
- `sub_1M/`: parked raw recordings (nothing deleted).

## Key results so far
- **88-config sweep with 80-20 train/test by game (23/5)**: NO config shows out-of-sample edge. Test realized-net: median −1.64, mean −1.21, range [−5.93, +3.28] across 55 within-budget configs. Train +400–650 figures were overfit to a few train games.
- Only doubly-positive (test realized AND test net) config: **pure tfma_pw_10s, thr 0.25σ, s500 cap1000** (test +2.91 / +115.94) — watch-list.
- t=0 sign-following gates all negative on test → thresholded combos beat t=0 OOS (reverses in-sample conclusion).
- Paper trading cancelled (logger + replay is the validation path).

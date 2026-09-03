# TODOs

_Last updated: 2026-06-22 ET_

## Sim latency model
- [ ] **`ack_delay` / `forward_delay` in `REALISTIC_DELAYS` are likely optimistic** — the modeled 22ms ack and 20ms forward are conservative one-way assumptions; a REST round-trip on the exchange-backed path can be ~2× that. Consider bumping `ack_delay` to ~0.043 and re-checking the realistic sweep ranking (a faster-than-real ack makes cancel-replace cheaper → optimistic fills).
- [ ] **Log the WC phase/clock + the desired quote in `decisions.jsonl`** (currently only the alpha + order events are logged, so a silent no-op in the desired-quote→router path — `order_router.py` place-skip returns for in-flight / want=None / can't-place / no-market-depth — leaves no trace). Also log the chrony offset when running on a remote box so cross-clock latencies are verifiable.
- [ ] **Fractional DUST**: a sub-0.05-contract remainder can leave an order RESTING indefinitely (never "fully filled"). Round/clear sub-ε remainders, or cancel-on-dust.
- [ ] **No forced flatten**: purely passive (square_off=false, liquidate_no_alpha=false, no aggressive cover) → can't exit a position the alpha/router won't passively unwind. Add an over-cap / near-settlement force-flatten.
- [ ] **Capture reject bodies** on HTTP 400 from `create_order` (err currently `None` in `acks.jsonl`); check post-only-cross / fractional-count / no-side price-band.

## DONE 2026-06-20 (StrategyConfig + base-class alpha MA/dev + agg_dev + generic sweep)
- [x] **Alpha half-lives are now CONFIG, not fixed module sets** (closes the Near-term `:141` directive) — `EmaSignal` base in `alphas.py` is the single MA/dev impl; engines take `half_lives` (None → full defaults, bit-identical; dict → lazy per-component). HL globals are defaults only. See codebase_notes 2026-06-20.
- [x] **`StrategyConfig` (symmetric alpha-gate list)** — `strategy_config.py`: list of `AlphaGate(name, threshold, AlphaConfig(half_life))` + sizing/risk knobs; NO primary/gating distinction (a side is blocked if ANY alpha crosses its threshold). `mm_sim` strategies read `cfg.X`; `_desired_sides` AND-gates all alphas (N=1 = legacy skew gate, bit-identical).
- [x] **New `agg_dev_<hl>` alpha** = `agg_<hl> − EMA_<hl>(agg_<hl>)` (self-trailing EMA via the base class).
- [x] **Generic self-collecting `wc_sweep.py`** — runs a set of configs over (in/out games), persists per-(config,game) PnLs, `best_configs(dir, score_fn=mean) → (best_in, best_out)` (pluggable score); `--finalize` (no `--collect` re-run, no sims). Removed `wc_sweep_obi*`/`wc_sweep_obi_mom*` variants.
- [x] **agg_dev sweep DONE on preempt** (24 workers, `wc_sweep_aggdev216`): obi_dev vs obi_dev+agg_dev, $250, free_budget=true, size 200/cap 1000, 21-6 → agg_dev REJECTED, `obi_dev_60s` thr 0.363514 best in both samples (see experiments.md Jun 21). **Watch:** the `_budget_clip` `/` made the MM sim path-sensitive (canary 229→23 fills); judge results on the per-game-averaged OOS metric, not single-game PnLs.
- [ ] **Confirm removal of other sweep-likes?** `sweep_size_cap.py`, `wc_arb_sweep.py` (goal-arb, different), `run_cross_sweep.sh`/`run_spike3_sweep.sh` (run mm_sim directly) were LEFT (separate-purpose) — delete if you consider them variants too.

## DONE 2026-06-20 (fractional sizes + ack logging)
- [x] **Fractional order sizes** — `ProdExchange._do_place` sends `round(qty, 2)` (was `int(qty)` → `count="0.00"` for sub-1 → reject → requote storm); `api.create_order` `count: int`→`float`. Kalshi supports fractional (min 0.01); see codebase_notes 2026-06-20 + memory [[kalshi-fractional-contracts]].
- [x] **Exchange-ack logging** — new `acks.jsonl` (ack+reject, joined to orders by `client_order_id`, which is now on the order record too). Same IPC→logger path as orders. Closes the "did we get an ack?" observability gap.
- [ ] **Run the launcher with `python -u` / `PYTHONUNBUFFERED=1`** — the trading-process stdout is block-buffered under a non-tty, so `[status]`/watchdog/reject prints are lost if the process dies.

## DONE 2026-06-19 (free_budget sweep + recorder)
- [x] **free_budget flag + $250 sweeps re-run clean** → best config `obi_dev_15s` thr 0.275094 s200 c1000 `free_budget:true` (`studies/wc_best_config_r250_r204_fb.json`; IN +66/OUT +91 per game). See experiments.md / codebase_notes.
- [x] **Config round-trip gap fixed** — `collect` persists `free_budget`/`liquidate_no_alpha`; the `--config` loader reproduces the swept strategy.
- [x] **Recorder: killed a DUPLICATE chain** (two recorders double-writing → MEXKOR/CANQAT gzip corruption), relaunched ONE clean chain; `CAP_S` 48h→23.5h so the SLURM job ≤24h (next-3am target unchanged).
- [x] **Jun-18 WC clocks backfilled** into `wc_clocks.json` (CANQAT/CZERSA/MEXKOR/SUIBIH; MEXKOR KO 21:00). NOTE: the recorder wrapper only runs `espn_clock` on DATASET (finalized) games, so staging games need a manual `espn_clock.py <tickers>` until finalized.
- [ ] **Taker/aggro path still not routed through the exchange backend** (latent; the selected config doesn't use it) — see below.
- [ ] **Recover the corrupt Jun-18 MEXKOR recording** (41MB, ~15s decodable) if its data is needed — likely unrecoverable.

## Async desired-state OrderRouter — DONE 2026-06-19 (non-blocking cancel+new)
- Strategy/alpha is now UNAWARE of in-flight/pending-ack/rate-limit (it only `router.set_target`s); the router drives the exchange async. `ProdExchange.place/cancel` are non-blocking (background `to_thread` REST; ack-completion reconciles off the latest target; ~50ms `_reconcile_timer` deferral net). Bit-identical to baseline (delays=0 + REALISTIC_DELAYS) verified. See codebase_notes (2026-06-19). This SUPERSEDES gaps A1/A4 + B5/B6/B7 framing below for the passive flow.
- [ ] **Taker/aggro path NOT routed through the exchange backend (latent)** — `SingleMM._aggro_entry`/`_take` simulate the taker fill INLINE (`pnl.trade(is_maker=False)` + local inventory) and send NO order through the backend; on ProdExchange they'd move tracked position without trading. The selected config `obi_dev_60s` never sets `aggro_entry`, so it's inert — but route them through the router (marketable orders) before running ANY aggro config on ProdExchange.
- [x] **`scratch/verify_bitident.py` expectation refreshed** (2026-06-19) — re-pinned `EXPECT = {net -61.1833, realized -14.5797, fills 229}` (the prior `317.4560/575` was a 2026-06-17 snapshot, drifted via legit commits `33fe652` size-clip / `388543a` position-sign; not a bug). Now prints MATCH. NB `scratch/` is gitignored, so this file isn't tracked.
- [x] **obi cache key completed** (2026-06-19) — the cache token now includes the own-ledger version so a standalone `register_resting` correctly drops our qty from obi (`test_register_resting_excludes_own_qty_from_obi` was failing on baseline; now passes). Bit-identical. See codebase_notes obi-cache note.

## SimExchange follow-ups (NEW 2026-06-17 — after the sim-mirrors-exchange refactor)
- **Realistic-delay sweep — RUNNING** (array 8508916 → `sims/wc_sweep_realistic/`, 8 shards 18h). Realistic execution (REALISTIC_DELAYS + in-flight lock + 20ms forward + ungated decide-every-event). On completion: `wc_sweep.py --collect` → `studies/wc_best_config_realistic.json`; compare the realistic best config to the optimistic wc_sweep_88 winner (obi_dev_60s realized-net −53% under realistic execution, so the realistic-optimal config may differ).
- **live_mm paper mode: DONE/validated** (44 paper fills on the websocket feed, no orders sent; added `--football`/`--budget`/`--realistic`).
- The market-delta clamp (never reduce a level below our own resting qty) is a ProdExchange invariant too (a participant cancel removes only their own depth).

## ProdExchange (exchange-backed path) — audit + status (2026-06-17)
**Refactor DONE (sim stays bit-identical: KORCZE net 317.4560 / 575 fills before & after):**
- **A (unified exchange API)** — `ProdExchange` is symmetric with `SimExchange` (same `place`/`cancel`/`drain`/`orders`/`own_levels`/`on_recorded_*`); `async run(consumer)` subscribes the public (`orderbook_delta`+`trade`) + private (`fill`) feeds and raises the SAME consumer events; REST orders via `api.create_order`/`cancel_order`. Consumer branches only on `exchange.simulates`. `live_mm.py --live` selects ProdExchange (default paper). See codebase_notes.
- **C (no queue model on ProdExchange)** — `fill_engine=None`, recorded hooks no-op; fills come from the authoritative private `fill` channel; `_ProdRestingOrder.queue_ahead=0`.
- **B (edge cases)** — order reject → `router.on_reject`→IDLE (no in-flight deadlock); 429 → reject/retry; cancel/fill race → fill in CANCEL_INFLIGHT frees the side, full-fill via cumulative-fill vs qty (backend-agnostic); 404-on-cancel → treat as filled; reconnect → `reconcile()` cancels ALL resting (clean slate) + resets ledger.
- **D** — own anonymous-trade ordering issue left as-is (doesn't affect the book-based obi config); kept below.
- **Watchdog + position reconciliation** — `ProdExchange._watchdog` (period 10s / stuck_after 20s: REST `get_orders` reconcile of stuck `*_INFLIGHT` + cancel orphans + log position drift); inventory set from the fill's `post_position_fp` (`logger.warning` on mismatch vs the incremental compute).

**Decoupled logger + timing infra — BUILT & VALIDATED (2026-06-17), see experiments.md:**
- `run_live.py` (launcher: discover once → spawn trading + logger sharing an mp.Queue), `live_logger.py` (private-feed WS + queue drain → orders.jsonl/decisions.jsonl), `live_ipc.py` (TimingEmitter), `mm_sim.py` timing context (`_evt`, gated → sim bit-identical), `order_router.py` `on_order` hook. Paper smoke: 11,179 decisions + 132 orders, 0 monotonic violations.
- [ ] **`analyze_live.py`** — recompute the decision alpha from the recorder's public feed (`Replayer`+`SingleAlphaEngine`) at each `decisions.jsonl` ts_ms, assert == the logged (strategy-sourced) value; timing histograms; fills reconcile vs `api.get_positions`; 0 resting at end.
- NOTE: snapshot-triggered orders/decisions have `exchange_ts=null` (orderbook_snapshot has no ts_ms) — minor; correlate those by read_ts.

Original audit (for reference) — "does the ProdExchange path behave EXACTLY like normal sim mode?" Framing: **paper mode (live_mm.py, default) IS faithful sim-on-exchange-feed** (identical `MMSimConsumer`+`SimExchange`+`PassiveFillEngine` from the WS). Items below are now mostly addressed above.

**A. Plumbing (built):** ProdExchange.place/cancel → REST (self-generated `client_order_id` so the public own-delta is matchable); authenticated `fill` channel subscribed (private fills are authoritative for inventory/PnL + resting-state); own public deltas routed by `client_order_id` → `view.apply_delta(is_own=True)` + `router.on_public_own_delta` (else our orders double-apply to the book AND feed our own obi/flow alphas); router full-fill / remaining derived from the fill's `post_position_fp`/`count` + tracked order qty (no fill-engine object on ProdExchange).

**B. Failure modes the sim never sees (handled):** order rejections → reject→IDLE path; ack/confirmation timeouts → watchdog REST reconcile; cancel/fill race → fill-in-CANCEL_INFLIGHT; reconnect resync → reconcile the ledger against the REST open-orders endpoint (skip `_reinject_own`); periodic position/PnL reconciliation vs the REST portfolio.

**C. Fill-model fidelity — the irreducible open question [REVIEW]:**
- **Queue position is MODELED, not known.** `PassiveFillEngine` estimates `queue_ahead` from observed book deltas under FIFO; real Kalshi priority + cancels ahead of us aren't fully observable, so real fill timing/rate WILL differ from the sim. This is the #1 reason exchange PnL won't match sim PnL exactly. Validation plan: diff actual fills vs what the queue model predicted on the same feed (markout + fill-rate by level).
- **Constant 20ms forward gate vs variable real latency**: sim gates fills by `placed_lts + forward_delay <= trade_ts`; real entry latency varies and there's no gate (you're at the front of the queue or not). Acknowledged modeling choice.

**D. Self-feed correctness (the original audit focus):**
- **Own anonymous public trade ordering risk [REVIEW]**: a fill emits a public anonymous `trade` (no coid) matched to our private fill by `trade_id`. Sim calls `mark_own_fill(trade_id)` at mint (deterministic) so the alpha sees flow C, not C+q. Real feed ordering of public-trade vs private-fill is **non-deterministic** (sub-ms, either order); if the public trade is processed first, the flow alpha over-counts our own q. Need to mark-by-trade_id with a short hold/buffer. **NOTE: the selected config `obi_dev_60s` is book-based (subtracted via the ledger), NOT flow-based — so this specific gap does NOT affect the current best config; it bites only if we ever skew on agg/tfma flow.**

**E. Already mitigated (don't re-solve):** MarketView market-only subtraction + the market-delta clamp (per-tick-invariant validated); `mark_own_fill`/`is_own_trade` exclusion (built; caveat D); in-flight lock (built + the B escapes); one global `WriteRateLimiter` gating both place AND cancel. Dead code to remove later: `PairMM.on_fill` (uncalled since the feed-driven refactor).

## Immediate (next morning routine, ~07:30 Jun 13)
- [ ] Verify overnight auto-filter ran (USAPAR/CANBIH volumes; merge `dataset_incoming/*` into `dataset/`).
- [ ] **Strip mom-based configs before any sweep** (user rule): regenerate/filter cfg_*.json on /data to drop `mom_*`, `agree_om`, `combo_om` (gen_combo_grid.py already updated).
- [ ] Re-run sweep + lasso-combo thresholds on enlarged dataset (5 general workers); rank by TEST realized-net; Slack table.
- [ ] WC per-game PnL ledger (N=4): passive baseline / latched-aggressive / hybrid TP-SL / 3-leg spike.
- [ ] Collect agg_mlb_eval results (b7lu80jfw) if not done: agg vs tfma_pw vs obi train/test PnL + price-weighted agg corr.
- [ ] viz.py on best config for the USAPAR game; send PNG.
- [ ] Report lasso pipeline results (HL table, λ path, weights, threshold table) if not already done.

## Near-term
- [x] **(DONE 2026-06-20 — `EmaSignal`/`StrategyConfig`, see top)** Alpha half-lives must be a CONFIG array, not a fixed module-level set (design fix — user directive 2026-06-19). Today `alphas.py` hardcodes `TFMA_HLS` / `MOM_HLS` / `OBI_MA_HLS` (module-level dicts); every HL-parameterized alpha (`tfma_{hl}`, `tfma_pw_{hl}`, `agg_{hl}`, `agg_pw_{hl}` + their `_ratio_` variants, `mom_{hl}`, `obi_ma_{hl}`, `obi_dev_{hl}`) reads its half-lives from these globals — so trying a new HL (e.g. `obi_dev_10s`) requires editing a global, and the engine wastefully maintains an EMA at EVERY HL in the set even when the strategy uses one. **There must be NO fixed set of HLs for any alpha.** Fix: each alpha (the engine, and `TradeFillMA`/`AggFlowMA` which already take `half_life_seconds=`) takes a `half_lives` array as CONFIG (flowing from strategy params), and value names are derived by the suffix convention `_<half_life>` (default; e.g. seconds → `_60s`). The engine maintains EMAs only for the configured/referenced HLs (lazy dict seeded on first use), not a hardcoded enum. Touch points: `alphas.py` HL globals (lines 25-28), both engine `__init__` (`_mid_ema`/`_obi_ema` init, the `TradeFillMA`/`AggFlowMA` constructions), `on_book` EMA loops, `value_of` suffix parsing (obi_dev/obi_ma/mom/tfma…), `values()` dict build, `alpha_names()`. CONSTRAINTS: bit-identical for the currently-used HLs; PRESERVE existing alpha-name strings (e.g. `obi_dev_60s`) so configs + the threshold cache keys (keyed by alpha-name list) still resolve. (Prompted by the obi_dev HL question + the $250 sweep adding obi_dev_15s.)
- [ ] **Self-feed: keep the strategy book MARKET-ONLY** (ProdExchange only; latent — sim & paper live_mm are market-only already, so this is inert until orders actually enter the public feed). ROOT CAUSE: the exchange's public book/feed includes OUR own orders. Invariant to enforce AT THE BOOK LAYER (not per read-site): the strategy's book excludes our orders, via BOTH (a) skip incremental orderbook_deltas tagged with our `client_order_id`, AND (b) subtract our resting-order ledger (`register_resting`) when reading depth/best-bid — (b) is MANDATORY because `orderbook_snapshot` (every reconnect) is aggregated and carries NO per-order tags, so (a) alone misses our resting qty after a snapshot. Then route ALL market-state reads through the market-only view; reads that break otherwise:
  1. supported-level keep/place guards (`is_pos(displayed)`, `lte(displayed,0)`) → our own order reads as "support" (the unsupported-level invariant is defeated);
  2. `rest_emptied` requote trigger → our order keeps the level >0 so it never fires;
  3. best-bid/ask touch + spread (`replay.top`, `_desired_sides`) → we quote against our own order / mis-measure spread (max_spread, crossed, `_liquidate_quotes` gates misfire);
  4. agg `_level_factors` `better` count → inflated by our levels.
  Already mitigated IF wired: obi/mom (register_resting), tfma/agg-flow (mark_own_fill + client_order_id). Also: call `register_resting` on every resting-qty change (qty≤0 clears) and `mark_own_fill(trade_id)` per private-fill-channel fill (subscribe to it); handle the public-trade-before-private-fill race (drain fill channel first / buffer trades). Don't reuse PassiveFillEngine queue-est off the public book (counts our own order). See codebase_notes "Forward latency + self-loop guard" + "Normalized flow alphas".
- [ ] Soccer: 3-leg lock netting (A+B+Draw = $1) analogous to pair-risk — reduces capital on locked triples.
- [ ] SingleAlphaEngine: combo components limited (no obi_ma/raw-tfma in fast path) — extend if combos are used on soccer.
- [ ] Validate soccer game-start estimates against actual kickoffs (expected_expiration − 2.2h was ~45 min late for MEX–RSA: est 15:48, kickoff ~15:00).
- [ ] viz.py: add orders.csv panels (order placements/cancels/fills timeline vs alpha).
- [ ] Effective-sample-size-aware fitting (180s overlap ⇒ ~rows/180 independent points): consider subsampling or block bootstrap for CIs.
- [ ] hist_sim.py (task #7): conservative trades-only historical backtest via GET /markets/trades (no book history exists server-side).

## Before Sept (NFL season)
- [ ] NFL ties: 2-market events but P(A)+P(B) < 1 possible — run NFL in single-market mode or add tie haircut to pair mirror.

## Watch
- [ ] WC knockout-stage market structure (2 or 3 markets?) from Jun 27.
- [ ] Kalshi fee/series changes (KXMLBGAME maker fees appeared overnight on Jun 9 — re-check fee_type per series periodically).

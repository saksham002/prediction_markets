# Kalshi Exchange Specifics — message fields & modeled latencies

_Last updated: 2026-06-18 ET_

Reference for the Kalshi feed/order message shapes and the latencies the sim
(`research/hft/exchange.py`) models. Field examples are from recorder captures
(`games_recording/*.jsonl.gz`) and the Kalshi API docs. The sim builders
(`own_delta_msg`, `public_trade_msg`, `private_fill_msg`, `ack_msg`) reproduce these
exact shapes so the consumer is byte-identical between the sim and the exchange-backed
path.

## Field-name traps (read first)
- **`*_dollars`** → price as a STRING in dollars, 4 dp (`"0.4800"` = 48¢). **`*_fp`** → fixed-point quantity STRING, 2 dp (`"15.82"` contracts). Always `float()` them.
- **`side` on book/fill = `"yes"`/`"no"`**, but **V2 ORDER `side` = `"bid"`/`"ask"`** (bid=buy yes, ask=sell yes). Different vocab, same book.
- **`position_fp`** is SIGNED (+ long yes / − short yes). On the WS the cost field is **`position_cost_dollars`**; on REST `/portfolio/positions` it's **`market_exposure_dollars`** (different name).
- A trade carries both `yes_price_dollars` and `no_price_dollars` (sum to 1).

## Public WS channels (`orderbook_delta`, `trade`) — read key, market_tickers filter

**`orderbook_snapshot`** (sent once on subscribe / each reconnect; full rebuild):
```json
{"type":"orderbook_snapshot","sid":1,"seq":17,"msg":{
  "market_ticker":"KXWCGAME-26JUN15IRINZL-IRI","market_id":"5479...",
  "yes_dollars_fp":[["0.0100","1065161.83"],["0.0200","541219.57"], ...],
  "no_dollars_fp":[ ... ]}}
```
- `yes_dollars_fp` / `no_dollars_fp`: list of `[price_dollars, size_fp]` per level. Aggregated — **no per-order tags** (so after a reconnect any own resting qty is invisible in the snapshot → must re-derive ownership from the ledger, not the feed).

**`orderbook_delta`** (incremental level change):
```json
{"type":"orderbook_delta","sid":2,"seq":481296,"msg":{
  "market_ticker":"...-TIE","market_id":"a1fe...","price_dollars":"0.7600",
  "delta_fp":"-15.82","side":"no","ts":"...Z","ts_ms":1781808480245}}
```
- `delta_fp` signed (+ add, − remove). The account's OWN deltas additionally carry **`client_order_id`** + `"subaccount":0` (account-scoped — every authenticated WS conn of the account sees the tag). Place δ>0, cancel δ<0, fill δ<0.

**`trade`** (anonymous public print):
```json
{"type":"trade","sid":1,"seq":124753,"msg":{
  "trade_id":"351e...","market_ticker":"...-NZL","yes_price_dollars":"0.2100",
  "no_price_dollars":"0.7900","count_fp":"22.56","taker_side":"yes",
  "taker_outcome_side":"yes","taker_book_side":"bid","ts":...,"ts_ms":1781570879974}}
```
- No `client_order_id`. Own fills are matchable ONLY by **`trade_id`** (== the `trade_id` on the private `fill`). `taker_side` is the aggressor's side; the maker side is the opposite.

## Private WS channel (`fill`) — read key CAN subscribe; account-scoped (no market filter)

**`fill`** (authoritative for inventory/PnL + resting-state):
- Fields (modeled in `private_fill_msg`): `trade_id`, `order_id`, `client_order_id`, `market_ticker`, `side` (`"yes"`/`"no"`), `action` (`"buy"`/`"sell"`), `count_fp`, `yes_price_dollars`, **`post_position_fp`** (account net position AFTER this fill, in fill order — AUTHORITATIVE; `_on_fill_reduce` sets inventory to it), `is_taker`, `ts`/`ts_ms`, `fee_cost`, `purchased_side`, `outcome_side`, `book_side` (bid/ask), `subaccount`. (`reason` is sim-only.)
- Whether a maker fill's book-reduction `orderbook_delta` carries `client_order_id` is **unconfirmed** (place + cancel deltas ARE coid-tagged). Replay-filtering of own orders uses `trade_id` matching (fill↔public-trade) regardless, so it does not depend on this.
- The private fill and the public trade for the same execution arrive within sub-ms on one socket, in NON-deterministic order (identical `ts_ms`). Process the fill channel before the trade, or buffer trades briefly (see codebase_notes self-loop guard).

**`market_position`** (singular type; event-driven on change, NOT periodic) — **NOT subscribed** (removed; a stale msg after a newer fill could revert position). Saved sample `studies/market_positions_sample.json`:
`user_id, market_ticker, position_fp (signed), position_cost_dollars, realized_pnl_dollars, fees_paid_dollars, position_fee_cost_dollars, volume_fp, subaccount`.

No order-lifecycle WS channel exists — order acks come ONLY from the synchronous REST response (modeled as the sim-only `ack_msg`).

## REST order responses — V2 event-order endpoints
Legacy V1 `/portfolio/orders` mutations were REMOVED Jun 2026 (`410 deprecated_v1_order_endpoint`). See codebase_notes.

**`POST /portfolio/events/orders`** (create) → `201`, FLAT body:
```json
{"client_order_id":"...","fill_count":"0.00","order_id":"e5d8...",
 "remaining_count":"1.00","ts_ms":1781821370267}
```
(+ `average_fill_price`, `average_fee_paid` if `fill_count`>0). Request body (V2):
`ticker, side("bid"/"ask"), count("1.00"), price("0.5300" yes-space), time_in_force("good_till_canceled"), self_trade_prevention_type("taker_at_cross"), client_order_id, [post_only]`.

**`DELETE /portfolio/events/orders/{order_id}`** (cancel) → `200`:
```json
{"order_id":"e5d8...","client_order_id":"...","reduced_by":"1.00","ts_ms":1781821373405}
```
(`404` if already gone/filled → treat as cancelled.)

**Position schema**: `GET /portfolio/positions` → `market_positions[].position_fp` (signed string, +long yes / −short yes) and `market_exposure_dollars`; there is NO `position` key.

## Modeled latencies (`exchange.REALISTIC_DELAYS` + `passive_fill.FORWARD_DELAY_S`)
Constant & deterministic assumptions. Default is **0** (synchronous = equivalence
baseline, in-flight lock non-binding); pass `REALISTIC_DELAYS` for the more realistic
model. Real exchange delays are variable — NOT modeled.

| param | value | leg | rationale |
|---|---|---|---|
| `forward_delay` | **20 ms** | order decided → matchable on the book (FILL gate: a resting order placed at P fills only on trades with `ts ≥ P+20ms`) | conservative one-way order-entry assumption (user directive) |
| `ack_delay` | **22 ms** | place/cancel send → private REST ack (releases the in-flight lock; makes cancel-replace cost a round-trip) | assumed REST round-trip |
| `pub_delay` | **28 ms** | place/cancel send → own `orderbook_delta` on the public feed | ack round-trip + ~6 ms private→public reflection |
| `fill_delay` | **16 ms** | exchange match → private `fill` arrival | assumed WS one-way |
| `fill_pub_lag` | **0 ms** | private fill → the same fill's public trade/delta legs | both WS legs carry the same `ts_ms` |

**Net effect** vs delays=0: the lock binds, fills are learned ~16ms late, our book footprint lags ~28ms → obi_dev_60s 20-game realized-net +7498 → +3498 (−53%).

## Event ordering & lock semantics (sim)
- **Place/cancel:** the private REST `ack` is ALWAYS delivered before the public `orderbook_delta` (`ack_delay` 22 < `pub_delay` 28; at delays=0 the ack is pushed to the scheduler first). The in-flight lock releases on the ack only (the public own-delta just updates the ledger). On the exchange the ack is the synchronous REST return, processed before any WS frame, so it's structurally first too.
- **Fill:** `priv_ts = match + fill_delay`, `pub_ts = priv_ts + fill_pub_lag` (= priv_ts since lag 0); at equal `ts` the scheduler delivers public_delta, public_trade, THEN private_fill (push order). Only the private fill drives inventory/PnL + the full-fill→IDLE transition (deduped by `trade_id`); the public legs only reduce the ledger / feed the self-skipping alpha — so the order is harmless.
- **Order actions are absolute** (each place = a whole new order with a fresh `client_order_id`; cancel removes a whole order; no amend — a requote is cancel-then-new). The book/feed REPRESENTATION is relative (`delta_fp`); position is absolute (set to `post_position_fp`).

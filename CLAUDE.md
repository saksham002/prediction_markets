# Prediction Market Research

## Project Purpose

1. **Research & Survey** — Explore prediction markets (Polymarket, Kalshi) to understand price dynamics, identify patterns, and spot arbitrage opportunities.
2. **Strategy Development** — Use research observations to design, simulate and backtest market-making and arbitrage strategies against recorded order-book data.

## User Background

The user has ~1 year of professional experience at a high-frequency trading firm, with a focus on options trading. They have deep familiarity with automated trading strategy design, pricing models, and exchange mechanics. However, they are new to prediction/betting markets like Polymarket and Kalshi, and are learning the specifics of how these markets operate, including their structure, liquidity characteristics, and resolution mechanics.

## Project Structure

- `research/` — Notebooks and scripts for market exploration and analysis
- `src/` — Core library code (client wrappers, utilities)
- `project_status/` — Living project state: `experiments.md` (running/recent experiments + results), `todos.md`, `ideas.md` (items needing review are marked [REVIEW]), `codebase_notes.md` (architecture, invariants, gotchas)
- `scratch/` — Throwaway one-off scripts for answering small ad-hoc questions (e.g. measuring goal→odds-move delay). NOT part of the main codebase/pipeline; nothing here is imported by `research/` or `src/`. Safe to delete anytime.

## Project status upkeep

Update the relevant `project_status/*.md` file whenever state changes: experiments launched or finished (experiments.md), new follow-ups discovered or completed (todos.md), new strategy/infra ideas or rejections with evidence (ideas.md), architectural changes or new invariants (codebase_notes.md). Refresh the "Last updated" stamp. When new [REVIEW] ideas accumulate in ideas.md, ping the user on Slack to review them.

## Terminology

- **Aggressive buy/sell** — Limit order priced at or worse than the counterparty's best price to guarantee immediate fill. Buy at `>= best_ask`, sell at `<= best_bid`. Equivalent to a market order.
- **Passive buy/sell** — Limit order priced to join the resting book and provide liquidity. Buy at `<= best_bid`, sell at `>= best_ask`. No guaranteed fill.

## Environments

- **Kalshi**: `/data/user_data/saksham3/uv/kalshi/.venv` (Python 3.14, managed by uv)
  - Dependencies: `kalshi-python-sync`, `python-dotenv`
  - **Important**: Must override `UV_PROJECT_ENVIRONMENT` when running uv commands, since the global env var points to `/data/user_data/saksham3/vla`. Use: `UV_PROJECT_ENVIRONMENT=/data/user_data/saksham3/uv/kalshi/.venv uv sync --project /data/user_data/saksham3/uv/kalshi`

## API keys / auth

Kalshi API credentials live in `kalshi/.env` (key IDs) and `kalshi/*.pem` (private keys) — **both gitignored; never commit them, and never read the `.pem` contents.** The key ID is the public identifier (sent in the `KALSHI-ACCESS-KEY` header); the `.pem` private key signs each request (RSA-PSS, see `kalshi/src/utils/api.py`). `api.py` reads the bare `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`, which should point to a **read-only** key so default usage is limited to market data + portfolio reads.

#!/bin/bash
#SBATCH --job-name=kalshi-wcsweep
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/sims/wcsweep-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/sims/wcsweep-slurm-%A_%a.err
#SBATCH --time=18:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=1-8

# REALISTIC WC FootballStrategy sweep -> sims/wc_sweep_r<budget>/: prod-faithful
# execution (SimExchange AWS feed delays + in-flight lock + 20ms forward fill
# latency; ungated requote = decide-from-view every event). obi-only 252 combos,
# 12/8 chronological, token rate limiter (place 10 / cancel 2 / 100-per-sec) ->
# sims/wc_sweep_r<budget>_obi_tok/. Budget is arg $1. 8 shards (auto-skips written).
BUDGET=${1:-1000}
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
SHARD=$((SLURM_ARRAY_TASK_ID - 1))
$PY -u $HFT/wc_sweep.py --shard $SHARD --num-shards 8 --budget $BUDGET

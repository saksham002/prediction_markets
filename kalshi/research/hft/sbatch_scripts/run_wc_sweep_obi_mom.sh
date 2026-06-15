#!/bin/bash
#SBATCH --job-name=kalshi-wcom
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/sims/wcom-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/sims/wcom-slurm-%A_%a.err
#SBATCH --time=03:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=1-8%4

# WC sweep: obi gated by short mom (agree_om_{1s,5s,10s,30s}), $1000 budget.
# 8 shards (max 4 concurrent) over wc_sweep_obi_mom.COMBOS; skips done files.
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
SHARD=$((SLURM_ARRAY_TASK_ID - 1))
$PY -u $HFT/wc_sweep_obi_mom.py --shard $SHARD --num-shards 8

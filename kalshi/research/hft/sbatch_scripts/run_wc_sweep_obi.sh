#!/bin/bash
#SBATCH --job-name=kalshi-wcobi
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/sims/wcobi-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/sims/wcobi-slurm-%A_%a.err
#SBATCH --time=03:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=1-4

# Restricted WC sweep: OBI only, no gating, $1000 deployed-capital budget.
# 4 shards over wc_sweep_obi.COMBOS (auto-skips already-written result files).
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
SHARD=$((SLURM_ARRAY_TASK_ID - 1))
$PY -u $HFT/wc_sweep_obi.py --shard $SHARD --num-shards 4

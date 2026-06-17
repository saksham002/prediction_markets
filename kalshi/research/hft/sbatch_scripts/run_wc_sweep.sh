#!/bin/bash
#SBATCH --job-name=kalshi-wcsweep
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/sims/wcsweep-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/sims/wcsweep-slurm-%A_%a.err
#SBATCH --time=06:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=1-8

# Full WC FootballStrategy sweep: blind / obi / agg / tfma (raw) + obi gated by
# SHORT-HL (1s) agg sign (agree_agg_1s); 6/6 chronological train-test; budget off
# (position-limit binding). 6 shards over wc_sweep.COMBOS (auto-skips written results).
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
SHARD=$((SLURM_ARRAY_TASK_ID - 1))
$PY -u $HFT/wc_sweep.py --shard $SHARD --num-shards 8

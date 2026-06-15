#!/bin/bash
#SBATCH --job-name=kalshi-sim-pass
#SBATCH --output=/data/user_data/saksham3/kalshi/signal_logs/sim-pass-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi/signal_logs/sim-pass-slurm-%A_%a.err
#SBATCH --time=08:01:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=1-4

# 2 x 2 grid: position_limit in (50, 500) x threshold in (50, 100).
# per_order_size = position_limit // 10. Index 0 unused; array starts at 1.
POSITION_LIMITS=(_ 50 50 500 500)
THRESHOLDS=(_ 50 100 50 100)
POSITION_LIMIT=${POSITION_LIMITS[$SLURM_ARRAY_TASK_ID]}
THRESHOLD=${THRESHOLDS[$SLURM_ARRAY_TASK_ID]}
PER_ORDER_SIZE=$((POSITION_LIMIT / 10))

TOP_N=${1:-5}
DURATION=${2:-8}

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/signals/sim_pass.py \
    -t "$THRESHOLD" \
    -l "$POSITION_LIMIT" \
    -s "$PER_ORDER_SIZE" \
    -n "$TOP_N" \
    -d "$DURATION"

#!/bin/bash
#SBATCH --job-name=kalshi-sim
#SBATCH --output=/data/user_data/saksham3/kalshi/signal_logs/sim-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi/signal_logs/sim-slurm-%A_%a.err
#SBATCH --time=08:01:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=1-4

# Index 0 unused; array starts at 1.
POSITION_LIMITS=(_ 10 50 200 1000)
POSITION_LIMIT=${POSITION_LIMITS[$SLURM_ARRAY_TASK_ID]}

THRESHOLD=${1:?Usage: sbatch run_sim.sh <threshold> [top_n] [duration_hours]}
TOP_N=${2:-5}
DURATION=${3:-8}

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/signals/sim.py \
    -t "$THRESHOLD" \
    -l "$POSITION_LIMIT" \
    -n "$TOP_N" \
    -d "$DURATION"

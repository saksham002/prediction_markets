#!/bin/bash
#SBATCH --job-name=kalshi-aggr-grid
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/sims/aggr-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/sims/aggr-slurm-%A_%a.err
#SBATCH --time=02:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=1-4

# Alpha thresholds for the taker strategy (list passed per alpha scale).
RECORDING=${1:?Usage: sbatch run_aggr_grid.sh <recording.jsonl.gz> [alpha_name] [position_limit] ["t1 t2 t3 t4"]}
ALPHA=${2:-tfma_pw_10s}
LIMIT=${3:-50}
read -r -a THRESHOLDS <<< "${4:-25 50 100 200}"
THRESHOLD=${THRESHOLDS[$((SLURM_ARRAY_TASK_ID - 1))]}

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/aggr_sim.py \
    "$RECORDING" \
    -t "$THRESHOLD" \
    -l "$LIMIT" \
    -a "$ALPHA"

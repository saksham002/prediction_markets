#!/bin/bash
#SBATCH --job-name=kalshi-live-mm
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/sims/live-slurm-%j.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/sims/live-slurm-%j.err
#SBATCH --time=08:10:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general

SIZE=${1:-10}
THRESHOLD=${2:-0.1}
ALPHA=${3:-obi}
DURATION=${4:-8}
TOP_N=${5:-10}
EXTRA_FLAGS=${6:-}
CAP=$(python3 -c "print(int(${SIZE} * 3))")

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/live_mm.py \
    -s "$SIZE" \
    -i "$CAP" \
    -t "$THRESHOLD" \
    -a "$ALPHA" \
    -d "$DURATION" \
    -n "$TOP_N" \
    $EXTRA_FLAGS

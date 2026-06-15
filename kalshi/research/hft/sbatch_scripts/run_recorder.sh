#!/bin/bash
#SBATCH --job-name=kalshi-tick-rec
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/ticks/rec-slurm-%j.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/ticks/rec-slurm-%j.err
#SBATCH --time=12:10:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general

TOP_N=${1:-12}
DURATION=${2:-12}
RERANK=${3:-2}
SERIES=${4:-}
EXTRA_SERIES=${5:-}
SERIES_FLAG=""
if [ -n "$SERIES" ]; then
    SERIES_FLAG="--series $SERIES"
fi
EXTRA_FLAG=""
if [ -n "$EXTRA_SERIES" ]; then
    EXTRA_FLAG="--extra-series $EXTRA_SERIES"
fi

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/record_ticks.py \
    -n "$TOP_N" \
    -d "$DURATION" \
    -r "$RERANK" \
    $SERIES_FLAG \
    $EXTRA_FLAG

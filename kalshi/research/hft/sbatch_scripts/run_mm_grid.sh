#!/bin/bash
#SBATCH --job-name=kalshi-mm-grid
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/sims/grid-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/sims/grid-slurm-%A_%a.err
#SBATCH --time=02:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=1-15

# 3 sizes x 5 skew thresholds (threshold list passed per alpha; OBI is in
# [-1,1] while TFMA is in signed contracts).
RECORDING=${1:?Usage: sbatch run_mm_grid.sh <recording.jsonl.gz> [alpha_name] ["t1 t2 t3 t4 t5"] [improve 0/1] [combo_file]}
ALPHA=${2:-tfma_pw_10s}
read -r -a THRESHOLDS <<< "${3:-0 10 25 50 100}"
IMPROVE=${4:-0}
IMPROVE_FLAG=""
if [ "$IMPROVE" = "1" ]; then
    IMPROVE_FLAG="--improve"
fi
COMBO_FLAG=""
if [ -n "$5" ]; then
    COMBO_FLAG="--combo-file $5"
fi

SIZES=(5 10 25)
IDX=$((SLURM_ARRAY_TASK_ID - 1))
SIZE=${SIZES[$((IDX / 5))]}
THRESHOLD=${THRESHOLDS[$((IDX % 5))]}
CAP=$((SIZE * 3))

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/mm_sim.py \
    "$RECORDING" \
    -s "$SIZE" \
    -i "$CAP" \
    -t "$THRESHOLD" \
    -a "$ALPHA" \
    $IMPROVE_FLAG \
    $COMBO_FLAG

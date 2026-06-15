#!/bin/bash
#SBATCH --job-name=kalshi-focused
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/combo_grid/focused-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/combo_grid/focused-slurm-%A_%a.err
#SBATCH --time=05:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=0-5

# One named config per array task from the focused_0611 dir.
CFG_DIR=${1:?Usage: sbatch run_focused.sh <cfg_dir> <ticks_dir> name1 name2 ...}
TICKS_DIR=${2:?}
shift 2
NAMES=("$@")
NAME=${NAMES[$SLURM_ARRAY_TASK_ID]}
if [ -z "$NAME" ]; then
    exit 0
fi

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/eval_buffer.py \
    --config "$CFG_DIR/$NAME.json" \
    --ticks-dir "$TICKS_DIR" \
    --out "$CFG_DIR/result_$NAME.csv"

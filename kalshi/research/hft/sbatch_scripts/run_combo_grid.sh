#!/bin/bash
#SBATCH --job-name=kalshi-combo-grid
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/combo_grid/slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/combo_grid/slurm-%A_%a.err
#SBATCH --time=03:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=0-19

# Each array task evaluates a contiguous slice of the combo-grid configs over
# the whole recording buffer. CFG_DIR must contain cfg_*.json + manifest.csv
# from gen_combo_grid.py.
CFG_DIR=${1:?Usage: sbatch run_combo_grid.sh <cfg_dir> [configs_per_task] [ticks_dir]}
PER_TASK=${2:-6}
TICKS_DIR=${3:-/data/user_data/saksham3/kalshi_hft/ticks}

START=$((SLURM_ARRAY_TASK_ID * PER_TASK))
for OFFSET in $(seq 0 $((PER_TASK - 1))); do
    IDX=$((START + OFFSET))
    CFG=$(printf "%s/cfg_%03d.json" "$CFG_DIR" "$IDX")
    if [ ! -f "$CFG" ]; then
        continue
    fi
    /data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
        /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/eval_buffer.py \
        --config "$CFG" \
        --ticks-dir "$TICKS_DIR" \
        --out "$CFG_DIR/result_$(printf %03d $IDX).csv"
done

#!/bin/bash
#SBATCH --job-name=kalshi-wide-grid
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/combo_grid/wide-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/combo_grid/wide-slurm-%A_%a.err
#SBATCH --time=02:30:00
#SBATCH --mem=16G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general
#SBATCH --array=0-4

CFG_DIR=/data/user_data/saksham3/kalshi_hft/combo_grid/wide_0611
for I in $(seq $SLURM_ARRAY_TASK_ID 5 87); do
    CFG=$(printf "%s/cfg_%03d.json" "$CFG_DIR" "$I")
    [ -f "$CFG" ] || continue
    [ -f "${CFG%.json}_result.csv" ] && continue
    /data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
        /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/eval_buffer.py \
        --config "$CFG" \
        --ticks-dir /data/user_data/saksham3/kalshi_hft/dataset \
        --out "${CFG%.json}_result.csv" 2>&1 | tail -1
done

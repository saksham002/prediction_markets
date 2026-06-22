#!/bin/bash
#SBATCH --job-name=kalshi-wcsweep
#SBATCH --output=/data/user_data/saksham3/kalshi_hft/sims/wcsweep-slurm-%A_%a.out
#SBATCH --error=/data/user_data/saksham3/kalshi_hft/sims/wcsweep-slurm-%A_%a.err
#SBATCH --time=18:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=preempt
#SBATCH --requeue
#SBATCH --array=1-24

# Generic WC sweep (default study + all params fixed in wc_sweep.py). 24-shard
# PREEMPT array; --requeue + the per-config skip make each shard resumable after a
# preemption. Two submit steps (the finalize reads the stored per-game PnLs and
# writes best-in/best-out -- it re-runs NO sims):
#   AID=$(sbatch --parsable sbatch_scripts/run_wc_sweep.sh)
#   sbatch --array=1 --dependency=afterany:$AID sbatch_scripts/run_wc_sweep.sh finalize
MODE=${1:-shard}
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
if [ "$MODE" = "finalize" ]; then
    $PY -u $HFT/wc_sweep.py --finalize
else
    SHARD=$((SLURM_ARRAY_TASK_ID - 1))
    $PY -u $HFT/wc_sweep.py --shard $SHARD --num-shards 24
fi

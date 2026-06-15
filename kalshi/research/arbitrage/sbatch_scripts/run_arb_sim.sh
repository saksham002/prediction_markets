#!/bin/bash
#SBATCH --job-name=kalshi-arb-sim
#SBATCH --output=/data/user_data/saksham3/kalshi/arb_logs/slurm-%j.out
#SBATCH --error=/data/user_data/saksham3/kalshi/arb_logs/slurm-%j.err
#SBATCH --time=16:10:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general

TOP_N=${1:-5}
CATEGORY=${2:-Sports}
DURATION=${3:-16}

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/arbitrage/sim.py \
    -n "$TOP_N" \
    -c "$CATEGORY" \
    -d "$DURATION"

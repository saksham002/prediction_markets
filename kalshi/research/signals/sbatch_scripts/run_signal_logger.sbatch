#!/bin/bash
#SBATCH --job-name=kalshi-signal-logger
#SBATCH --output=/data/user_data/saksham3/kalshi/signal_logs/slurm-%j.out
#SBATCH --error=/data/user_data/saksham3/kalshi/signal_logs/slurm-%j.err
#SBATCH --time=12:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --partition=general

TOP_N=${1:-5}

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/signals/signal_logger.py \
    -n "$TOP_N"

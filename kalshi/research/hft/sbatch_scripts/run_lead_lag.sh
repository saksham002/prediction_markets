#!/bin/bash
#SBATCH --job-name=kalshi-lead-lag
#SBATCH --output=/home/saksham3/logs/lead_lag-%j.out
#SBATCH --error=/home/saksham3/logs/lead_lag-%j.err
#SBATCH --time=02:00:00
#SBATCH --mem=48G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general

# Lead-lag CCF between tfma_60s / agg_60s / obi_ma_60s, per league + overall.
# Writes plots/lead_lag_60s.csv and plots/lead_lag_60s.png.

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/lead_lag.py

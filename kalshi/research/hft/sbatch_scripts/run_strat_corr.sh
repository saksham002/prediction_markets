#!/bin/bash
#SBATCH --job-name=kalshi-strat-corr
#SBATCH --output=/home/saksham3/logs/strat_corr-%j.out
#SBATCH --error=/home/saksham3/logs/strat_corr-%j.err
#SBATCH --time=08:00:00
#SBATCH --mem=64G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general

# Strategy-trigger-sampled alpha/return correlation table, exact forward price,
# FilterStrategy gate, relative return, extended horizons + HLs.
# Writes plots/alpha_return_corr_strat.csv.

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/strat_corr_table.py

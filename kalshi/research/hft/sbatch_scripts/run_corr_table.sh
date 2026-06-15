#!/bin/bash
#SBATCH --job-name=kalshi-corr-table
#SBATCH --output=/home/saksham3/logs/corr_table-%j.out
#SBATCH --error=/home/saksham3/logs/corr_table-%j.err
#SBATCH --time=06:00:00
#SBATCH --mem=48G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general

# Alpha-vs-forward-return correlation table over horizons, per league + overall.
# Replays the whole dataset (collect_samples), writes plots/alpha_return_corr.csv.

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/league_corr_table.py

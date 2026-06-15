#!/bin/bash
#SBATCH --job-name=kalshi-build-list
#SBATCH --output=/home/saksham3/logs/build_list-%j.out
#SBATCH --error=/home/saksham3/logs/build_list-%j.err
#SBATCH --time=02:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general

# Build replay players for every game in scratch/analyze_book.list (goal/red
# markers from espn_clock cache, 0.01x/0.1x speeds, exchange/local order times).

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/build_players_list.py

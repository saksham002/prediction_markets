#!/bin/bash
#SBATCH --job-name=kalshi-build-players
#SBATCH --output=/home/saksham3/logs/build_players-%j.out
#SBATCH --error=/home/saksham3/logs/build_players-%j.err
#SBATCH --time=01:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:1
#SBATCH --partition=general

# Rebuild the replay-player HTMLs for the same two games (highest-volume MLB +
# Korea WC) from the existing recordings, using the slider-fixed book_player
# template. Outputs to plots/players/ under the repo (visible from login node).

/data/user_data/saksham3/uv/kalshi/.venv/bin/python -u \
    /home/saksham3/projects/personal/prediction_markets/kalshi/research/hft/build_players.py

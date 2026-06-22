"""Filesystem roots for the HFT pipeline.

All default to the Babel layout (/data/user_data/saksham3/kalshi_hft) but can be
relocated by setting the KALSHI_HFT_DATA environment variable — e.g. on an EC2
deploy box: `export KALSHI_HFT_DATA=/home/ec2-user/kalshi_hft`. Import the roots
from here instead of hard-coding paths so the same code runs on any machine.
"""
import os
from pathlib import Path

HFT_DATA = Path(os.environ.get("KALSHI_HFT_DATA", "/data/user_data/saksham3/kalshi_hft"))

DATASET = HFT_DATA / "dataset"
SIMS = HFT_DATA / "sims"
STUDIES = HFT_DATA / "studies"
TICKS = HFT_DATA / "ticks"

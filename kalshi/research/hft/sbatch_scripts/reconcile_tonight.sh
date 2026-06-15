#!/bin/bash
# After the new recorder finalizes: merge legacy+new per game, verify windows,
# then swap the reconciled Jun13 games into dataset/ (copy-first: backup the
# pre-merge per-game versions). Run as an sbatch job (needs /data + venv).
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
DATA=/data/user_data/saksham3/kalshi_hft
STAGING=$DATA/merge_tonight_staging
BK=$DATA/dataset_premerge_backup

echo "=== merge legacy + new per-game (Jun13) -> staging ==="
$PY $HFT/merge_tonight.py

echo "=== verify windows on staging ==="
$PY $HFT/verify_window.py $STAGING
$PY $HFT/verify_dataset.py $STAGING || { echo "STAGING SANITY FAIL — not swapping"; exit 1; }

echo "=== swap reconciled games into dataset/ (backup pre-merge versions) ==="
mkdir -p $BK
for f in $STAGING/*.jsonl.gz; do
  [ -e "$f" ] || continue
  base=$(basename "$f")
  # back up any existing partial versions (dataset + sub_1M), then place merged
  [ -e "$DATA/dataset/$base" ] && cp "$DATA/dataset/$base" "$BK/$base"
  [ -e "$DATA/sub_1M/$base" ] && mv "$DATA/sub_1M/$base" "$BK/sub_$base"
  mv -v "$f" "$DATA/dataset/$base"
done
echo "=== final: window verify of all Jun13 games in dataset/ ==="
for g in $DATA/dataset/*26JUN13*.jsonl.gz; do echo "$(basename $g)"; done
echo "RECONCILE DONE"

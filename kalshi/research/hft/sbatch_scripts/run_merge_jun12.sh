#!/bin/bash
# Recover + merge the Jun 12 sessions into dataset/ with a sanity gate.
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
DATA=/data/user_data/saksham3/kalshi_hft
INC=$DATA/dataset_incoming
DS=$DATA/dataset
EVE_RAW=$DATA/ticks/ticks_20260612_183515_2488594.jsonl.gz

echo "=== 1. remove stale .tmp ==="
ls -la $INC/*.tmp 2>/dev/null && rm -f $INC/*.tmp && echo "removed" || echo "none"

echo "=== 2. re-filter evening raw -> dataset_incoming ==="
$PY $HFT/filter_recordings.py "$EVE_RAW" --out-dir $INC

echo "=== 3. sanity check dataset_incoming ==="
$PY $HFT/verify_dataset.py $INC
if [ $? -ne 0 ]; then echo "SANITY FAILED — not merging"; exit 1; fi

echo "=== 4. merge incoming -> dataset ==="
for f in $INC/ticks_*.jsonl.gz; do
  [ -e "$f" ] || continue
  mv -v "$f" $DS/
done

echo "=== 5. final dataset summary ==="
$PY $HFT/verify_dataset.py $DS
echo "ALL DONE"

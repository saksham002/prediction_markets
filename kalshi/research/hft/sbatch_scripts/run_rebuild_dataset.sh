#!/bin/bash
# Rebuild dataset under the any-leg>=1M + keep-all-legs filter. Re-filters the
# raws we still have (Jun 11 in sub_1M); Jun 10 raws are gone so those sessions
# are only re-verified. Re-checks sub_1M (the discarded set) for new inclusions.
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
DATA=/data/user_data/saksham3/kalshi_hft
INC=$DATA/dataset_incoming
DS=$DATA/dataset
SUB=$DATA/sub_1M
BK=$DATA/dataset_prebuild_backup

rm -f $INC/*.tmp 2>/dev/null
mkdir -p $BK

echo "=== re-filter raws still available (sub_1M Jun 11) with fixed filter ==="
for RAW in $SUB/ticks_20260611_074145_3157453.jsonl.gz \
           $SUB/ticks_20260611_193554_1678643.jsonl.gz; do
  echo "--- $RAW ---"
  $PY $HFT/filter_recordings.py "$RAW" --out-dir $INC
done

echo "=== sanity gate on re-filtered ==="
$PY $HFT/verify_dataset.py $INC || { echo "REBUILD SANITY FAIL — not swapping"; exit 1; }

echo "=== back up + replace re-filtered sessions in dataset/ ==="
for f in $INC/ticks_*.jsonl.gz; do
  base=$(basename "$f")
  [ -e "$DS/$base" ] && cp -v "$DS/$base" "$BK/$base"
  mv -v "$f" "$DS/$base"
done

echo "=== FINAL: verify full dataset (any-leg rule; notes incomplete legs) ==="
$PY $HFT/verify_dataset.py $DS
echo "REBUILD DONE"

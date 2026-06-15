#!/bin/bash
# Hybrid aggro-entry + passive-liquidation grid on the two WC games
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
DATA=/data/user_data/saksham3/kalshi_hft

cd /home/saksham3/projects/personal/prediction_markets/kalshi
$PY -m pytest tests/test_passive_fill.py -q && echo "tests OK" || { echo "TESTS FAILED"; exit 1; }

MEX=$(ls $DATA/dataset/ticks_20260611_074145_*.jsonl.gz 2>/dev/null || ls $DATA/dataset_incoming/ticks_20260611_074145_*.jsonl.gz)
KOR=$(ls $DATA/dataset/ticks_20260611_193554_*.jsonl.gz 2>/dev/null || ls $DATA/dataset_incoming/ticks_20260611_193554_*.jsonl.gz)
echo "MEX file: $MEX"
echo "KOR file: $KOR"

for ENTRY in ${ENTRIES:-10000 25000}; do
  for STOP in 0.03 0.07; do
    for GAME in MEX KOR; do
      FILE=$([ $GAME = MEX ] && echo $MEX || echo $KOR)
      echo "=== hybrid game=$GAME entry=$ENTRY limit=300 profit=0.02 stop=$STOP ==="
      $PY $HFT/mm_sim.py "$FILE" -a tfma_pw_300s -s 300 -i 1000 -t 999 \
        --aggro-entry $ENTRY --aggro-limit 300 --aggro-profit 0.02 --aggro-stop $STOP \
        --pair-risk --series KXWCGAME \
        --tag hybrid_${GAME}_e${ENTRY}_st${STOP} 2>&1 | tail -15
    done
  done
done
echo "ALL DONE"

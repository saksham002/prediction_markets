#!/bin/bash
# Cross-leg aggro entry + passive liquidation: threshold sweep on the 2 WC games
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
DATA=/data/user_data/saksham3/kalshi_hft
ALPHA=${ALPHA:?set ALPHA, e.g. tfma_pw_60s}

cd /home/saksham3/projects/personal/prediction_markets/kalshi
$PY -m pytest tests/test_passive_fill.py -q && echo "tests OK" || { echo "TESTS FAILED"; exit 1; }

MEX=$DATA/dataset/ticks_20260611_074145_3157453.jsonl.gz
KOR=$DATA/dataset/ticks_20260611_193554_1678643.jsonl.gz

for T in ${THRESHOLDS:-1000 2000 5000 10000 20000 40000}; do
  for GAME in MEX KOR; do
    FILE=$([ $GAME = MEX ] && echo $MEX || echo $KOR)
    echo "=== cross game=$GAME alpha=$ALPHA t=$T limit=300 ==="
    $PY $HFT/mm_sim.py "$FILE" -a $ALPHA -s 300 -i 1000 -t 999 \
      --aggro-entry $T --aggro-limit 300 --aggro-cross \
      --pair-risk --series KXWCGAME \
      --tag cross_${GAME}_${ALPHA}_t${T} 2>&1 | tail -15
  done
done
echo "ALL DONE"

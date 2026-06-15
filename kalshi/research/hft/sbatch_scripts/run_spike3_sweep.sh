#!/bin/bash
# 3-leg spike trade (buy YES on spike leg + NO on both siblings): sweep
# (t_pos, t_neg) and the t_neg-only variant on the 2 WC games
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
DATA=/data/user_data/saksham3/kalshi_hft
ALPHA=tfma_pw_30s
RESULTS=/home/saksham3/projects/personal/prediction_markets/plots/spike3_results.csv

cd /home/saksham3/projects/personal/prediction_markets/kalshi
$PY -m pytest tests/test_passive_fill.py -q && echo "tests OK" || { echo "TESTS FAILED"; exit 1; }

MEX=$DATA/dataset/ticks_20260611_074145_3157453.jsonl.gz
KOR=$DATA/dataset/ticks_20260611_193554_1678643.jsonl.gz
[ "${APPEND:-0}" = "1" ] || echo "variant,t_pos,t_neg,game,net_pnl,realized_pnl,fees,unrealized" > $RESULTS

run_one() {  # variant t_pos t_neg game file extra_args...
  local VARIANT=$1 TPOS=$2 TNEG=$3 GAME=$4 FILE=$5; shift 5
  local OUT=$($PY $HFT/mm_sim.py "$FILE" -a $ALPHA -s 300 -i 1000 -t 999 \
    --aggro-limit 300 --aggro-cross --aggro-neg $TNEG "$@" \
    --pair-risk --series KXWCGAME \
    --tag spike3_${VARIANT}_${GAME}_p${TPOS}_n${TNEG} 2>&1)
  echo "$OUT" | tail -14
  local NET=$(echo "$OUT" | grep -m1 "net_pnl" | awk '{print $2}')
  local REAL=$(echo "$OUT" | grep -m1 "realized_pnl" | awk '{print $2}')
  local FEES=$(echo "$OUT" | grep -m1 "fees_paid" | awk '{print $2}')
  local UNREAL=$(echo "$OUT" | grep -m1 "unrealized_pnl" | awk '{print $2}')
  echo "$VARIANT,$TPOS,$TNEG,$GAME,$NET,$REAL,$FEES,$UNREAL" >> $RESULTS
}

for TNEG in ${TNEG_LIST:-2000 5000 10000 20000 40000}; do
  for TPOS in ${TPOS_LIST:-500 2000 5000}; do
    [ "${NO_ORDER_CHECK:-0}" = "1" ] || [ $TPOS -lt $TNEG ] || continue
    for GAME in MEX KOR; do
      FILE=$([ $GAME = MEX ] && echo $MEX || echo $KOR)
      echo "=== spike3 t_pos=$TPOS t_neg=$TNEG game=$GAME ==="
      run_one both $TPOS $TNEG $GAME $FILE --aggro-entry $TPOS
    done
  done
done

if [ "${SKIP_NEGONLY:-0}" != "1" ]; then
for TNEG in ${TNEG_LIST:-2000 5000 10000 20000 40000}; do
  for GAME in MEX KOR; do
    FILE=$([ $GAME = MEX ] && echo $MEX || echo $KOR)
    echo "=== spike3 neg-only t_neg=$TNEG game=$GAME ==="
    run_one negonly none $TNEG $GAME $FILE
  done
done
fi

$PY $HFT/plot_spike3.py
echo "ALL DONE"

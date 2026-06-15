#!/bin/bash
# Corrected-book reruns of the most phantom-level-affected WC sims:
# blind passive baseline, hybrid TP/SL grid, 3-leg spike active cells
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
DATA=/data/user_data/saksham3/kalshi_hft
MEX=$DATA/dataset/ticks_20260611_074145_3157453.jsonl.gz
KOR=$DATA/dataset/ticks_20260611_193554_1678643.jsonl.gz

cd /home/saksham3/projects/personal/prediction_markets/kalshi
$PY -m pytest tests/test_passive_fill.py -q && echo "tests OK" || { echo "TESTS FAILED"; exit 1; }

run() {  # tag file extra-args...
  local TAG=$1 FILE=$2; shift 2
  echo "=== $TAG ==="
  $PY $HFT/mm_sim.py "$FILE" --pair-risk --series KXWCGAME --tag corr_$TAG "$@" 2>&1 \
    | grep -E "n_fills|realized_pnl|fees_paid|unrealized_pnl|net_pnl|open_contracts_end"
}

# 1. Blind passive baseline (always quote both sides)
for G in MEX KOR; do
  F=$([ $G = MEX ] && echo $MEX || echo $KOR)
  run base_$G "$F" -a obi -t 999 -s 500 -i 1000
done

# 2. Hybrid TP/SL grid (tfma_pw_300s, profit 2c)
for E in 10000 25000; do for ST in 0.03 0.07; do for G in MEX KOR; do
  F=$([ $G = MEX ] && echo $MEX || echo $KOR)
  run hyb_${G}_e${E}_st${ST} "$F" -a tfma_pw_300s -s 300 -i 1000 -t 999 \
    --aggro-entry $E --aggro-limit 300 --aggro-profit 0.02 --aggro-stop $ST
done; done; done

# 3. 3-leg spike active cells (tfma_pw_30s)
for G in MEX KOR; do
  F=$([ $G = MEX ] && echo $MEX || echo $KOR)
  run spk_${G}_p500_n2000 "$F" -a tfma_pw_30s -s 300 -i 1000 -t 999 \
    --aggro-entry 500 --aggro-neg 2000 --aggro-limit 300 --aggro-cross
  run spk_${G}_p2000_n5000 "$F" -a tfma_pw_30s -s 300 -i 1000 -t 999 \
    --aggro-entry 2000 --aggro-neg 5000 --aggro-limit 300 --aggro-cross
  run spk_${G}_negonly_n2000 "$F" -a tfma_pw_30s -s 300 -i 1000 -t 999 \
    --aggro-neg 2000 --aggro-limit 300 --aggro-cross
done
echo "ALL DONE"

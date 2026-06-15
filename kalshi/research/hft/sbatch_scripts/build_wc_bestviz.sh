#!/bin/bash
# Run the WC best-in-sample config (from studies/wc_best_config.json) on all 8
# WC games with --football + full logging, viz each (per-market 4-panel:
# odds+fills / alpha / position / pnl), and report the unliquidated position.
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
DATA=/data/user_data/saksham3/kalshi_hft
CFG=${1:-$DATA/studies/wc_best_config.json}
OUT=${2:-/home/saksham3/projects/personal/prediction_markets/plots/wc_bestviz}

read A T S C B < <($PY -c "import json;c=json.load(open('$CFG'));print(c['alpha'],c['thr'],c['size'],c['cap'],c.get('budget',1e9))")
echo "best config: alpha=$A thr=$T size=$S cap=$C budget=$B  (cfg=$CFG out=$OUT)"
rm -rf $OUT; mkdir -p $OUT
echo "event,n_fills,realized_pnl,fees,net_pnl,open_contracts_end,pair_exposure_end" > $OUT/open_positions.csv
for G in $(ls $DATA/dataset/KXWCGAME*.jsonl.gz | sort); do
  EV=$(basename $G .jsonl.gz)
  TAG=$(basename $OUT)_$EV
  $PY $HFT/mm_sim.py "$G" -a "$A" -t "$T" -s "$S" -i "$C" --football --budget "$B" \
     --series KXWCGAME --tag $TAG >/dev/null 2>&1
  RD=$DATA/sims/$TAG
  $PY $HFT/viz.py $RD >/dev/null 2>&1
  mkdir -p $OUT/$EV; cp $RD/viz/*.png $OUT/$EV/ 2>/dev/null
  $PY - "$RD" "$EV" >> $OUT/open_positions.csv <<'PYEOF'
import csv, sys
from pathlib import Path
s = list(csv.DictReader(open(Path(sys.argv[1]) / "summary.csv")))[0]
print(f"{sys.argv[2]},{s['n_fills']},{s['realized_pnl']},{s['fees_paid']},{s['net_pnl']},"
      f"{s['open_contracts_end']},{s['net_pair_exposure_end']}")
PYEOF
  echo "  $EV done"
done
echo "=== open positions ==="
column -t -s, $OUT/open_positions.csv
echo "BESTVIZ DONE"

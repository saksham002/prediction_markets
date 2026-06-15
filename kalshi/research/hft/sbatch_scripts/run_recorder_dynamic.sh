#!/bin/bash
# Dynamic, self-perpetuating recorder launcher. Sizes the SLURM job to END at
# the next 3am ET (pushed past any game live then), records per-game files,
# finalizes on clean exit, then resubmits itself for the next window.
#
# Start the chain once from a COMPUTE node (internet needed for the schedule
# lookup), e.g.:  srun --partition=debug --gres=gpu:1 --mem=2G --time=0:05:00 \
#                   bash .../run_recorder_dynamic.sh
set -u
PY=/data/user_data/saksham3/uv/kalshi/.venv/bin/python
HFT=/home/saksham3/projects/personal/prediction_markets/kalshi/research/hft
SELF="$HFT/sbatch_scripts/run_recorder_dynamic.sh"
SERIES="KXMLBGAME,KXNBAGAME,KXNHLGAME,KXNFLGAME"
EXTRA="KXWCGAME,KXINTLFRIENDLYGAME"

if [ "${KALSHI_REC_CHILD:-0}" != "1" ]; then
  # --- launcher: size the job and submit ---
  SECS=$($PY "$HFT/recorder_end_time.py" "$SERIES,$EXTRA" 24)
  # Recorder exits cleanly AT the target (3am); SLURM limit padded 30min after
  # so finalize + resubmit complete before any hard kill.
  DUR=$($PY -c "print(max(0.2, $SECS / 3600))")
  PAD=$((SECS + 1800))
  TLIMIT=$(printf '%02d:%02d:00' $((PAD / 3600)) $(((PAD % 3600) / 60)))
  echo "launch: SLURM --time=$TLIMIT, recorder --duration=${DUR}h (ends ~next 3am ET)"
  sbatch --partition=general --gres=gpu:1 --mem=32G --time="$TLIMIT" \
    --job-name=kalshi-rec --output="$HOME/logs/rec_%j.log" \
    --export=ALL,KALSHI_REC_CHILD=1,KREC_DUR="$DUR",KREC_SERIES="$SERIES",KREC_EXTRA="$EXTRA" \
    "$SELF"
  exit 0
fi

# --- inside the SLURM job: record, then resubmit the next window ---
$PY "$HFT/record_ticks.py" --series "$KREC_SERIES" --extra-series "$KREC_EXTRA" \
    --top-n 18 --extra-top-n 10 --duration "$KREC_DUR"
echo "recorder exited cleanly; resubmitting next window"
KALSHI_REC_CHILD=0 bash "$SELF"

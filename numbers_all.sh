#!/usr/bin/env bash
# numbers_all.sh (816, Dino 23 Nov): the numbers pass over EVERY unchecked order in the queues, whatever fed it (the day
# batch of the 03:00 feed used to be the only way in: mail-sweep and hand-fed orders never got a pass), then the
# auto-check. Runs numbers_pass_run.sh (worker paused, Ollama unloaded, 4 h a round) until nothing is left, at most
# MAX_ROUNDS rounds. Run as dinoc. Log: reports/numbers_all_<day>.log
set -uo pipefail
P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; R=/srv/ocr/scans/pipeline; DAY=$(date +%F)
exec >> "$R/reports/numbers_all_$DAY.log" 2>&1
exec 9>/tmp/numbers_all.lock; flock -n 9 || { echo "$(date) another numbers_all is running"; exit 0; }
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
say() { echo "$(date '+%F %T') $*"; }
left() { cd /srv/ml/numbers && $V $P/numbers_pass.py --day "$DAY" --count --all-unchecked 2>/dev/null | grep -oE '^[0-9]+ order' | grep -oE '[0-9]+' | head -1; }
for i in $(seq 1 "${MAX_ROUNDS:-9}"); do
  n=$(left); say "round $i: ${n:-0} order(s) to pass"
  [ "${n:-0}" = 0 ] && break
  bash $P/numbers_pass_run.sh "$DAY" --all-unchecked
  m=$(left); [ "${m:-0}" = "${n:-0}" ] && { say "no progress this round (${m:-0} left): stopping"; break; }
done
say "auto-check"
cd $P && python3 -c "import po_daily; po_daily.autocheck()"
say "Ronin JT stock codes from the capture"; python3 $P/rjt_export_fill.py --apply
say "done"

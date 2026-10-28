#!/usr/bin/env bash
# Numbers pass on the day's purchase-order batch (live, Dino 27 Oct), called by po_daily watch once the batch is read and
# before the checking file is written. Pauses the worker, waits for other GPU jobs, unloads Ollama (the 3B model needs
# ~2.9 GB; Ollama holds 13.8 of the 16 GB), runs numbers_pass.py, resumes the worker. Never fails the caller: on any
# problem the batch keeps the main reader's values and the checking file is still written.
#   numbers_pass_run.sh [YYYY-MM-DD] [extra numbers_pass.py args]
set -uo pipefail
P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; D=/srv/ml/numbers
ROOT=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["root"])')
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
DAY=${1:-$(date +%F)}; shift || true
LOG=$ROOT/reports/numbers_pass_$DAY.log
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
exec > >(tee -a "$LOG") 2>&1
say() { echo "$(date '+%F %T') $*"; }
AS=""; [ "$(id -u)" = 0 ] && AS="sudo -u dinoc -E"
[ -f "$ROOT/flags/numbers_pass_off" ] && { say "numbers pass switched off (flags/numbers_pass_off)"; exit 0; }
[ -d "$D/active/lora" ] || { say "no active numbers model ($D/active)"; exit 0; }
cd "$D"
MADE_PAUSE=0
resume() { [ $MADE_PAUSE = 1 ] && rm -f "$ROOT/PAUSE" && say "worker resumed"; }
trap resume EXIT
say "numbers pass for $DAY with $(readlink -f $D/active)"
# anything to do? (cheap check without the GPU)
N=$($AS $V $P/numbers_pass.py --day "$DAY" --count "$@" 2>/dev/null | grep -oE '^[0-9]+ order' | grep -oE '[0-9]+' | head -1)
[ "${N:-0}" = 0 ] && { say "no unchecked orders with lines in the batch: nothing to do"; exit 0; }
while systemctl is-active --quiet mps-ocr-training.service || systemctl is-active --quiet mps-numbers.service; do sleep 60; done
if [ ! -e "$ROOT/PAUSE" ]; then echo "numbers pass $DAY" > "$ROOT/PAUSE"; MADE_PAUSE=1; say "worker paused"; fi
MAX_WAIT=3600 $AS $P/gpu_wait.sh || { say "GPU busy - numbers pass skipped, main reader's values kept"; exit 0; }
sleep 60                                   # the worker finishes the page it is on
for m in $(curl -s $OLL/api/ps | python3 -c 'import json,sys;print(" ".join(x["name"] for x in json.load(sys.stdin).get("models",[])))'); do
    curl -s $OLL/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null && say "unloaded $m"
done; sleep 5
timeout 4h $AS $V $P/numbers_pass.py --day "$DAY" "$@" || say "numbers pass failed or timed out (rc $?) - orders not yet passed keep the main reader's values"
say "done"
exit 0

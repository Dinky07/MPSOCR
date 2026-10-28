#!/usr/bin/env bash
# Numbers pass shadow run on one day's purchase orders (nothing the pipeline uses is changed): wait for the GPU, note what
# Ollama holds on the GPU, pause the worker, unload Ollama, run numbers_shadow.py, resume the worker.
#   numbers_shadow_run.sh [YYYY-MM-DD]      (default: today)
set -uo pipefail
P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; D=/srv/ml/numbers
ROOT=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["root"])')
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
DAY=${1:-$(date +%F)}
RUN=$(ls -d $D/run-* 2>/dev/null | while read r; do [ -d $r/lora ] && echo $r; done | tail -1)
LOG=$ROOT/reports/numbers_shadow_$DAY.log
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
exec > >(tee -a $LOG) 2>&1
cd $D
say() { echo "$(date '+%F %T') $*"; }
MADE_PAUSE=0
resume() { [ $MADE_PAUSE = 1 ] && rm -f "$ROOT/PAUSE" && say "worker resumed"; }
trap resume EXIT
say "numbers shadow run for $DAY with $RUN"
[ -n "$RUN" ] || { say "no trained numbers model"; exit 1; }
while systemctl is-active --quiet mps-ocr-training.service || systemctl is-active --quiet mps-numbers.service; do sleep 60; done
MAX_WAIT=7200 sudo -u dinoc $P/gpu_wait.sh || { say "GPU busy - stopping"; exit 1; }
say "GPU with Ollama's models loaded:"; nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
curl -s $OLL/api/ps | python3 -c 'import json,sys;[print("  ",m["name"],round(m.get("size_vram",0)/2**30,2),"GB on the GPU") for m in json.load(sys.stdin).get("models",[])]'
if [ ! -e "$ROOT/PAUSE" ]; then echo "numbers shadow run $DAY" > "$ROOT/PAUSE"; MADE_PAUSE=1; say "worker paused"; fi
sleep 90
for m in $(curl -s $OLL/api/ps | python3 -c 'import json,sys;print(" ".join(x["name"] for x in json.load(sys.stdin).get("models",[])))'); do
    curl -s $OLL/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null && say "unloaded $m"
done; sleep 5
sudo -u dinoc -E $V $P/numbers_shadow.py --run $RUN --date $DAY || say "shadow run failed"
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
say "done"

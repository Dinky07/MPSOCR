#!/usr/bin/env bash
# Ronin JT shadow pass, nightly (Dino 10 Nov 2026 "Yes, build it"). Reads in the gap between route_sort runs, because route_sort
# does not honour PAUSE and Ollama plus this model do not fit on the card together. Skips the GPU part whenever training or
# anyone else holds it. The OCR pause ends at READ_UNTIL at the latest; the scoring afterwards needs no GPU.
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline; V=/srv/ml/train-venv/bin/python; W=$R/work/rjt_shadow
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
READ_UNTIL=$(date -d "${READ_UNTIL_AT:-22:20}" +%s)
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1 MPS_OCR_CONFIG=$P/config.json
mkdir -p "$W"
exec >> "$W/shadow.log" 2>&1
say() { echo "$(date '+%F %T') $*"; }
MADE_PAUSE=0
resume() {
    if [ $MADE_PAUSE = 1 ]; then
        grep -qs '^rjt shadow' "$R/PAUSE" && rm -f "$R/PAUSE" && say "worker resumed"
        MADE_PAUSE=0
    fi
}
trap resume EXIT
trap 'say "stopped (signal)"; exit 1' INT TERM
score() { $V $P/rjt_shadow.py --score-only | tail -40 | cut -c1-200; }
skip() { say "no reading tonight: $*"; score; exit 0; }

say "shadow pass starts"
[ -f "$R/flags/rjt_shadow_off" ] && { say "switched off (flags/rjt_shadow_off)"; exit 0; }
[ -e "$R/PAUSE" ] && skip "PAUSE is set ($(head -c 80 "$R/PAUSE"))"
for s in mps-after-train mps-ab-finish mps-weekend-train mps-ocr-training mps-numbers mps-numbers-weekly; do
    systemctl is-active --quiet $s.service && skip "$s is running"
done
w=0
while systemctl is-active --quiet mps-route-sort.service; do
    [ $w -ge 300 ] && skip "route_sort still running after 5 min"
    sleep 30; w=$((w + 30))
done
N=$($V $P/rjt_shadow.py --count 2>/dev/null | grep -oE "'to read': [0-9]+" | grep -oE '[0-9]+$')
[ "${N:-0}" = 0 ] && skip "no JTs left to read"
[ $(( READ_UNTIL - $(date +%s) )) -lt 420 ] && skip "too late to start before $(date -d @$READ_UNTIL '+%H:%M')"
MAX_WAIT=120 $P/gpu_wait.sh || skip "another GPU job is running"

echo "rjt shadow (ends by itself by $(date -d @$READ_UNTIL '+%H:%M'))" > "$R/PAUSE"; MADE_PAUSE=1; say "worker paused, $N JTs to read"
sleep 60       # the worker finishes the page it is on: an Ollama client is never cut off mid-request
free=0
for i in 1 2 3 4 5 6; do
    for m in $(curl -s -m 20 $OLL/api/ps | python3 -c 'import json,sys;print(" ".join(x["name"] for x in json.load(sys.stdin).get("models",[])))' 2>/dev/null); do
        curl -s -m 60 $OLL/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null && say "unloaded $m"
    done
    sleep 5
    used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' ')
    [ "${used:-99999}" -lt 2500 ] && { free=1; say "GPU free (${used} MiB in use)"; break; }
    sleep 20
done
[ $free = 1 ] || { resume; skip "GPU not free (${used} MiB in use)"; }
left=$(( READ_UNTIL - $(date +%s) ))
[ $left -lt 240 ] && { resume; skip "too late to start"; }
timeout $(( left + 150 ))s $V $P/rjt_shadow.py --max-seconds $(( left - 30 )) | tail -45 | cut -c1-200
say "reading finished (rc ${PIPESTATUS[0]})"
resume
say "done"

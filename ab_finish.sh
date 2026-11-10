#!/usr/bin/env bash
# 616 (Dino 10 Nov "Yes, finish them"): the A/B service hit its 84 h limit on Tue 10 Nov 05:56 with three checkpoints unscored.
# Waits for the training chain to end (its resume step skips once on training/chain_restart, so OCR stays held), scores those
# three with the A/B's own command on the same test orders, writes the A/B report, then brings all OCR back on.
# A checkpoint is skipped rather than part-scored when a full pass will not fit before LATEST: a partial score is on other orders.
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline; V=/srv/ml/train-venv/bin/python; D=/srv/ml/numbers; DS=$D/data-sheet
AB=$D/ab-20261106-2135; REP=$R/training/reports/numbers-ab-20261106-2135.json
QA=unsloth/Qwen2.5-VL-3B-Instruct-bnb-4bit; QB=unsloth/Qwen3-VL-4B-Instruct-unsloth-bnb-4bit
ACTIVE=$(readlink -f $D/active)
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
LATEST=$(date -d "2026-11-12 16:45" +%s)
JOBS="A/epoch-4:14700 B/epoch-3:15900 B/epoch-4:15900"       # measured 9-10 Nov: A 4 h 04, B 4 h 24 a pass
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
say() { echo "$(date '+%F %T') $*"; }
left() { echo $(( LATEST - $(date +%s) )); }
if [ "${DRY:-0}" = 1 ]; then
    for x in $JOBS; do j=${x%:*}; [ -d $AB/$j/lora ] && [ ! -f $AB/$j/eval.json ] && say "would score $j" || say "NOT scorable: $j"; done
    say "active ${ACTIVE}; $(( $(left) / 3600 )) h to the latest end"; exit 0
fi
exec > >(tee -a $AB/run.log) 2>&1
unload() {
    for m in $(curl -s -m 20 $OLL/api/ps | python3 -c 'import json,sys;print(" ".join(x["name"] for x in json.load(sys.stdin).get("models",[])))' 2>/dev/null); do
        curl -s -m 60 $OLL/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null && say "unloaded $m"
    done; sleep 5
}
gpu_free() {
    local i used
    for i in $(seq 1 20); do
        unload
        used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' ')
        [ "${used:-99999}" -lt 2500 ] && { say "GPU free (${used} MiB in use)"; return 0; }
        sleep 30
    done
    say "GPU not free after 10 min (${used} MiB in use)"; return 1
}
report() { python3 $P/numbers_ab_report.py "$AB" "$REP" "$QA" "$QB" "$ACTIVE" | cut -c1-2500; }
finish() {
    report
    rm -f $R/training/chain_restart
    # stopped while the chain still runs: the note is gone, so the chain's own end brings OCR back
    if systemctl is-active --quiet mps-after-train.service; then say "the chain is still running: it brings OCR back itself"; return; fi
    say "all OCR back on"; FROM_CHAIN=1 /bin/bash $P/weekend_resume.sh
}
trap finish EXIT
trap 'say "stopped (signal)"; exit 1' INT TERM

say "finishing the epoch scoring after the chain: $JOBS"
w=0
while systemctl is-active --quiet mps-after-train.service; do
    [ $((w % 3600)) -eq 0 ] && say "waiting for the training chain ($(( $(left) / 3600 )) h to the latest end)"
    sleep 60; w=$((w + 60))
done
rm -f $R/training/chain_restart
say "the training chain has ended"
for x in $JOBS; do
    j=${x%:*}; est=${x#*:}; RUN=$AB/$j
    [ -f $RUN/eval.json ] && { say "$j is already scored"; continue; }
    lim=$(( $(left) - 300 )); [ $lim -gt $(( est * 13 / 10 + 1800 )) ] && lim=$(( est * 13 / 10 + 1800 ))
    [ $lim -lt $(( est * 105 / 100 )) ] && { say "no time for a full score of $j: skipped ($(( $(left) / 60 )) min left)"; continue; }
    gpu_free || { say "scoring $j skipped: GPU not free"; continue; }
    say "scoring $j (time limit ${lim}s)"
    timeout $(( lim + 900 ))s $V $P/numbers_eval.py --data $DS --run $RUN --max-seconds $lim || say "scoring $j failed"
done
say "epoch scoring finished"

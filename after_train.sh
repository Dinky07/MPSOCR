#!/usr/bin/env bash
# 573 (Dino 6 Nov: "delay all OCR until all planned training is done and training is maximised ... even thursday" and
# "and the Job Transfer training?"): what runs after the numbers A/B (mps-weekend-train), one after the other, the GPU
# kept free and Ollama unloaded before each, every one scored only (nothing goes live) and skipped when it would not end
# before the latest resume (Thursday 12 Nov 03:00):
#   1. the Ronin JT numbers model (rjt_numbers_run.sh: Qwen2.5-VL-3B, 417 train / 89 test JTs, 5 epochs)
#   2. the JT reader fine-tune, Qwen3-VL-8B + LoRA on the 469 reviewed job transfers, 3 epochs (the 16 Oct recipe)
#   3. the same with 5 epochs
# then weekend_resume.sh: all OCR back on. Started now; waits while the A/B runs.
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline; V=/srv/ml/train-venv/bin/python
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
LATEST=$(date -d "2026-11-12 02:30" +%s)
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1 IN_QUEUE=1 MPS_OCR_CONFIG=$P/config.json
say() { echo "$(date '+%F %T') $*"; }
left() { echo $(( LATEST - $(date +%s) )); }
hrs() { echo $(( $1 / 3600 )); }
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
        [ $i = 1 ] && say "GPU has ${used} MiB in use: $(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader | tr '\n' ' ')"
        sleep 30
    done
    say "GPU not free after 10 min (${used} MiB in use)"; return 1
}
finish() { say "all OCR back on"; FROM_CHAIN=1 /bin/bash $P/weekend_resume.sh; }
trap finish EXIT
trap 'say "stopped (signal)"; exit 1' INT TERM

say "after the A/B: Ronin JT numbers, JT LoRA 3 and 5 epochs; everything must end by $(date -d @$LATEST '+%a %H:%M')"
w=0
while systemctl is-active --quiet mps-weekend-train.service; do
    [ $((w % 3600)) -eq 0 ] && say "waiting for the numbers A/B ($(hrs $(left)) h left)"
    sleep 120; w=$((w + 120))
done
say "the numbers A/B is done: $(ls -t $R/training/reports/numbers-ab-*.json 2>/dev/null | head -1 | xargs -r basename)"
# the A/B stopped these timers and started them again at its end: off again until the chain is done
for t in mps-route-sort.timer mps-tc-copies.timer mps-po-check.timer; do sudo -n /usr/bin/systemctl stop $t; done

# 1. Ronin JT numbers (its own weekday 01:00-12:00 rule and 3 h cap do not apply here)
if [ $(left) -gt $((3 * 3600)) ]; then
    gpu_free && {
        sed -e 's/^\[ "\$(date +%H%M)" -ge 0100 \].*$/: # 573: any time/' $P/rjt_numbers_run.sh > /tmp/rjt573.sh
        grep -q '573: any time' /tmp/rjt573.sh && bash -n /tmp/rjt573.sh || cp $P/rjt_numbers_run.sh /tmp/rjt573.sh
        say "1. Ronin JT numbers model, 5 epochs"
        EPOCHS=5 TRAIN_MAX=$(( $(left) - 3 * 3600 ))s /bin/bash /tmp/rjt573.sh || say "Ronin JT numbers: failed or skipped (see its run.log)"
    }
else say "1. Ronin JT numbers: skipped, $(hrs $(left)) h left"; fi

# 2./3. the JT reader fine-tune
for ep in 3 5; do
    need=$(( (ep * 3600 * 3 / 2) + 3600 ))                 # ~1.5 h an epoch on the 8B (a guess until the first run) + scoring
    if [ $(left) -gt $need ]; then
        gpu_free && { say "$([ $ep = 3 ] && echo 2 || echo 3). JT LoRA fine-tune, $ep epochs"
                      /bin/bash $P/jt_lora_run.sh $ep $(( $(left) - 5400 )) || say "JT LoRA $ep epochs: failed or skipped"; }
    else say "JT LoRA $ep epochs: skipped, $(hrs $(left)) h left (needs ~$(hrs $need) h)"; fi
done
say "training chain done"

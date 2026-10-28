#!/usr/bin/env bash
# Wait until no other GPU job is running before a training job starts (ExecStartPre of mps-finetune.service).
# "Other GPU job" = a runner script still running (log in run/done without its .rc), or a known batch/eval process.
# The OCR worker is not counted: training pauses it itself (PAUSE file). Gives up after MAX_WAIT seconds (exit 1,
# so the training run is skipped and the timer's result shows as failed on the dashboard).
RUN=/srv/ocr/scans/pipeline/_src/run
MAX_WAIT=${MAX_WAIT:-10800}       # 3 h
PAT='tc_batch|eval_jt|eval_inv|eval_dates|eval_model|po_batch|_backlog|training\.py (quick|finetune)'
waited=0
# cmdlines of this script's own ancestors: a runner job that (indirectly) started us is not "another" GPU job
ANC=""; pid=$$
while [ "${pid:-1}" -gt 1 ]; do ANC="$ANC
$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)"; pid=$(awk '{print $4}' /proc/$pid/stat 2>/dev/null); done
busy() {
    for f in "$RUN"/*.sh; do
        [ -e "$f" ] || continue
        n=$(basename "$f" .sh)
        case "$n" in mps-runner|install-runner) continue;; esac
        case "$ANC" in *"$n.sh"*) continue;; esac
        [ -e "$RUN/done/$n.log" ] && [ ! -e "$RUN/done/$n.rc" ] && { echo "runner job $n"; return 0; }
    done
    p=$(pgrep -af -- "$PAT" | grep -v -e "^$$ " -e gpu_wait | head -1)
    [ -n "$p" ] && { echo "process: ${p:0:120}"; return 0; }
    return 1
}
while what=$(busy); do
    [ $waited -ge $MAX_WAIT ] && { echo "GPU still busy after $((waited / 60)) min ($what) - skipping this run"; exit 1; }
    [ $((waited % 600)) -eq 0 ] && echo "waiting for the GPU: $what"
    sleep 60; waited=$((waited + 60))
done
[ $waited -gt 0 ] && echo "GPU free after $((waited / 60)) min"
exit 0

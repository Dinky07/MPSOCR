#!/usr/bin/env bash
# 563 (Dino 6 Nov: "start the training run now and postpone the monday PO Feed at three and take monday too. Start the
# worker again on tuesday at 3:00"): this weekend's training queue now instead of Saturday 17:00 - the quick training
# (PO checking files import, PO / WS / JT / TC) then the numbers A/B (Qwen2.5-VL-3B vs Qwen3-VL-4B) with a deadline of
# Tuesday 10 Nov 02:30. The worker stays paused (PAUSE written by runner 563) until mps-weekend-resume, Tuesday 03:00.
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline
say() { echo "$(date '+%F %T') $*"; }
say "weekend training run (Dino 6 Nov): quick training, then the numbers A/B to Tuesday 02:30"
w=0
while [ "$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))" 2>/dev/null)" != None ] && [ $w -lt 7200 ]; do
    [ $((w % 600)) -eq 0 ] && say "waiting for the worker to finish its file"
    sleep 30; w=$((w + 30))
done
cd $P && MPS_OCR_CONFIG=$P/config.json IN_QUEUE=1 python3 $P/nightly_training.py --only import,po,ws,jt,tc
rm -f $R/training/numbers_ab_request && say "A/B request used (the weekly numbers pass is back to normal next Saturday)"
export DEADLINE=$(date -d "2026-11-10 02:30" +%s) IN_QUEUE=1 MPS_OCR_CONFIG=$P/config.json
exec /bin/bash $P/numbers_ab.sh

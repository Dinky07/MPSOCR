#!/usr/bin/env bash
# 563/570: all OCR back on when the training is done (Dino 6 Nov "delay all OCR until all planned training is done and
# training is maximised ... even thursday"): run by the training chain at its end, or by mps-weekend-resume.timer on
# Thursday 12 Nov 03:00 at the latest. The worker and the CPU worker resume; the PO feed (Monday's 03:00 feed and any
# later ones held: it runs once now), the PO check, route sort, TC copies and the weekly training queue start again.
R=/srv/ocr/scans/pipeline
say() { echo "$(date '+%F %T') $*"; }
for u in mps-weekend-train.service mps-after-train.service; do
    if systemctl is-active --quiet $u && [ "${FROM_CHAIN:-0}" != 1 ]; then
        say "$u is still going at the latest resume time: stopping it (what it scored is kept)"
        sudo -n /usr/bin/systemctl stop $u
    fi
done
if grep -q "563" $R/PAUSE 2>/dev/null; then rm -f $R/PAUSE && say "worker resumed"
elif [ -e $R/PAUSE ]; then say "PAUSE is someone else's ($(head -c 80 $R/PAUSE)): left as it is"
else say "worker was already running"; fi
for t in mps-ocr-cpu-worker.service mps-po-feed.timer mps-po-check.timer mps-ocr-training.timer mps-route-sort.timer mps-tc-copies.timer; do
    sudo -n /usr/bin/systemctl start $t && say "started $t"
done
systemctl list-timers mps-po-feed.timer mps-ocr-training.timer --no-pager --no-legend

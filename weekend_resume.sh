#!/usr/bin/env bash
# 563: Tuesday 10 Nov 03:00 (Dino 6 Nov "Start the worker again on tuesday at 3:00"): the worker resumes; the PO feed
# (Monday's 03:00 feed postponed: it runs once now), the PO check and the weekly training queue timers start again.
R=/srv/ocr/scans/pipeline
say() { echo "$(date '+%F %T') $*"; }
if systemctl is-active --quiet mps-weekend-train.service; then
    say "the training run is still going after its 02:30 deadline: stopping it (its scored results are kept)"
    sudo -n /usr/bin/systemctl stop mps-weekend-train.service
fi
if grep -q "563" $R/PAUSE 2>/dev/null; then rm -f $R/PAUSE && say "worker resumed"
elif [ -e $R/PAUSE ]; then say "PAUSE is someone else's ($(head -c 80 $R/PAUSE)): left as it is"
else say "worker was already running"; fi
for t in mps-po-feed.timer mps-po-check.timer mps-ocr-training.timer mps-route-sort.timer mps-tc-copies.timer; do
    sudo -n /usr/bin/systemctl start $t && say "started $t"
done
systemctl list-timers mps-po-feed.timer mps-ocr-training.timer --no-pager --no-legend

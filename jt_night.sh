#!/usr/bin/env bash
# The JT reader fine-tune redo, 5 epochs, overnight (Dino 11 Nov "ocr back on first and we can do the JT Epoch run overnight
# tonight or thursday night"). The 10 Nov run cut every example at 2048 tokens, before the answer; train_lora.py now loads
# with an 8192 limit and checks the answers fit before training. mps-jt-night.timer fires Wed 11 and Thu 12 Nov at 21:00;
# Thursday's does nothing once a good run is done. OCR is held for the night and comes back by itself: at the end of the
# run, on any failure, and at the service's hard limit (07:00) through ExecStopPost.
#   jt_night.sh            the run
#   jt_night.sh --resume   OCR back on (only what this script held)
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline
DONE=$R/training/jt_night.done; HELD=$R/training/jt_night.held
TIMERS="mps-route-sort.timer mps-tc-copies.timer mps-po-check.timer"
say() { echo "$(date '+%F %T') $*"; }

resume() {
    if grep -qs '^jt night' $R/PAUSE; then rm -f $R/PAUSE && say "worker resumed"; fi
    if [ -f $HELD ]; then
        for t in $(cat $HELD); do sudo -n /usr/bin/systemctl start $t && say "started $t"; done
        rm -f $HELD
    fi
}
[ "${1:-}" = --resume ] && { resume; exit 0; }

t=$(date -d "${TRAIN_UNTIL_AT:-05:45}" +%s); [ $t -le $(date +%s) ] && t=$(date -d "tomorrow ${TRAIN_UNTIL_AT:-05:45}" +%s)
TRAIN_UNTIL=$t
say "JT night run: 5 epochs, training ends by $(date -d @$TRAIN_UNTIL '+%a %H:%M'), OCR back by 07:00 at the latest"
[ -f $DONE ] && { say "already done ($(cat $DONE)): nothing to do"; exit 0; }
[ -f $R/flags/jt_night_off ] && { say "switched off (flags/jt_night_off)"; exit 0; }
[ -e $R/PAUSE ] && { say "PAUSE is already set ($(head -c 80 $R/PAUSE)): not tonight"; exit 0; }
for s in mps-after-train mps-ab-finish mps-weekend-train mps-ocr-training mps-numbers mps-numbers-weekly mps-rjt-shadow; do
    systemctl is-active --quiet $s.service && { say "$s is running: not tonight"; exit 0; }
done

trap resume EXIT
trap 'say "stopped (signal)"; exit 1' INT TERM
echo "jt night run (Dino 11 Nov): OCR back by itself by 07:00" > $R/PAUSE; say "worker paused"
: > $HELD
for x in $TIMERS; do
    systemctl is-active --quiet $x && sudo -n /usr/bin/systemctl stop $x && echo $x >> $HELD && say "held $x"
done
w=0
while systemctl is-active --quiet mps-route-sort.service || systemctl is-active --quiet mps-tc-copies.service; do
    [ $w -ge 2400 ] && { say "route sort / TC copies still running after 40 min: not tonight"; exit 0; }
    sleep 30; w=$((w + 30))
done
sleep 60                                   # the worker finishes the page it is on
MAXS=$(( TRAIN_UNTIL - $(date +%s) - 600 ))
[ $MAXS -lt $((4 * 3600)) ] && { say "only $((MAXS / 60)) min left for training: not tonight"; exit 0; }
t0=$(date +%s)
/bin/bash $P/jt_lora_run.sh 5 $MAXS; rc=$?
rep=$(find $R/training/reports -name 'jt-lora-*-5ep.json' -newermt "@$t0" 2>/dev/null | sort | tail -1)
if [ $rc = 0 ] && [ -n "$rep" ]; then
    read -r n bad <<< "$(python3 -c "import json;d=json.load(open('$rep'));print(d.get('tests') or 0, d.get('unparseable') or 0)")"
    if [ "${n:-0}" -gt 0 ] && [ "$bad" -lt $(( n / 2 )) ]; then
        echo "$(basename $rep) $(date '+%F %T')" > $DONE; say "good run: $bad of $n test answers unreadable; report $rep"
    else say "run finished but $bad of $n test answers are unreadable: not marked done"; fi
else say "JT run failed (rc $rc): not marked done"; fi
say "done"

#!/usr/bin/env bash
# Tonight's one-off training (Dino 28 Oct 17:34 / 17:42), run by mps-train-now.service:
#   1. wait for the 19:30 checking import, the runner jobs and the worker's queues (feeds and the collector are paused
#      by 287a, so the queues only drain)
#   2. nightly_training.py --only import,po,ws,jt,tc   (the weekly queue without the long numbers pass; each step keeps
#      the current setup unless the new one scores better on the test documents)
#   3. resume what 287a paused (flags/paused_for_training), restart the worker (picks up the re-read patch) and
#      mps-route-sort-now if it was stopped here, then start the hold re-read timer (mps-po-reread.timer)
# The Saturday 17:00 weekly training stays as it is.
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline; F=$R/flags/paused_for_training
exec >> "$R/reports/training-now-$(date +%F).log" 2>&1
log() { echo "$(date '+%F %T') $*"; }
log "train_now started"
t=$(date -d '19:40' +%s); while [ $(date +%s) -lt $t ]; do sleep 60; done
while systemctl is-active --quiet mps-po-import.service; do sleep 60; done
log "checking import done"
gpu_queues() {
  local n=0 d T
  for d in $R/in/*/; do
    T=$(basename "$d")
    [ -e "$R/flags/process_off_$T" ] && continue
    [ "$T" = DeliveryNotes ] && continue                       # CPU only
    n=$((n + $(find "$d" -maxdepth 1 -type f -name '*.pdf' | wc -l)))
  done
  echo $n
}
deadline=$(date -d '23:30' +%s); [ $deadline -lt $(date +%s) ] && deadline=$(( $(date +%s) + 3600 ))
while :; do
  n=$(gpu_queues)
  [ "$n" -eq 0 ] && { log "worker queues empty"; break; }
  [ $(date +%s) -ge $deadline ] && { log "still $n files in the worker queues at the deadline: training anyway (it pauses the worker)"; break; }
  sleep 120
done
RS=0
if systemctl is-active --quiet mps-route-sort-now.service; then
  sudo -n systemctl stop mps-route-sort-now.service && RS=1 && log "mps-route-sort-now stopped for the training (restarted after)"
fi
MAX_WAIT=7200 bash $P/gpu_wait.sh || log "gpu_wait gave up: training anyway"
log "training: nightly_training.py --only import,po,ws,jt,tc"
python3 $P/nightly_training.py --only import,po,ws,jt,tc; rc=$?
log "training finished rc=$rc"
ls -t $R/training/reports/ 2>/dev/null | head -5 | sed 's/^/  report: /'
# resume
if [ -f "$F" ]; then
  while read -r what unit; do
    case "$what" in
      enabled) sudo -n systemctl enable "$unit" && sudo -n systemctl start "$unit" && log "resumed $unit";;
      active)  sudo -n systemctl start "$unit" && log "started $unit";;
    esac
  done < "$F"
  mv -f "$F" "$F.resumed-$(date +%H%M)"
fi
sudo -n systemctl restart mps-ocr-worker.service && log "worker restarted"
[ $RS = 1 ] && sudo -n systemctl start mps-route-sort-now.service && log "mps-route-sort-now restarted"
if [ -e "$R/flags/reread_after_training" ]; then
  sudo -n systemctl enable mps-po-reread.timer && sudo -n systemctl start mps-po-reread.timer && log "hold re-read timer started"
  rm -f "$R/flags/reread_after_training"
fi
systemctl list-timers --all --no-pager | grep -E "mps-(po|tc)-feed|mps-po-reread" | sed 's/^/  /'
log "train_now done"

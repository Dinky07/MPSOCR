#!/usr/bin/env bash
# 453 test (Dino 3 Nov "test it after the current items in the queue are finished, should be done by 11"): started by
# mps-tc-upright.service; waits until the worker has nothing to read (until 15:30, then skips), pauses it, tests the
# upright test-cert reader (tc_ab3.py), resumes the worker; on PASS installs tc_reader.py and restarts the worker (idle).
# Report: reports/tc reader upright test 2026-11-03.txt
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline
OUT="$R/reports/tc reader upright test 2026-11-03.txt"
exec > >(tee -a "$OUT") 2>&1
say() { echo "$(date '+%F %T') $*"; }
busy() {
  [ "$(find $R/in -maxdepth 2 -name '*.pdf' -not -path '*/CustomerOrders/*' | wc -l)" -gt 0 ] && return 0
  [ "$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))")" != "None" ] && return 0
  systemctl is-active --quiet mps-numbers-redo.service mps-numbers-weekly.service mps-ocr-training.service && return 0
  return 1
}
say "upright test-cert reader test: waiting for the queue to finish"
idle=0
until [ $idle -ge 2 ]; do
  [ "$(date +%H%M)" -ge 1530 ] && { say "worker still busy at 15:30: test skipped (nothing changed)"; exit 0; }
  if busy; then idle=0; else idle=$((idle + 1)); fi
  sleep 60
done
say "queue finished"
[ "$(md5sum < $P/tc_reader.py | cut -c1-32)" = "b0e7be4ffbc0c8a200daf9b9ba90bf1d" ] || { say "tc_reader.py changed since 09:41: test skipped"; exit 0; }
rm -rf /tmp/tc453 && mkdir -p /tmp/tc453 && cp -p $P/tc_reader.py /tmp/tc453/
python3 $P/tc_upright_patch.py /tmp/tc453/tc_reader.py && python3 -m py_compile /tmp/tc453/tc_reader.py || { say "PATCH FAILED"; exit 1; }
OWN=0; [ -e $R/PAUSE ] || { echo "tc reader upright test 453" > $R/PAUSE; OWN=1; say "worker paused"; }
release() { [ $OWN = 1 ] && rm -f $R/PAUSE && say "worker resumed"; OWN=0; }
trap release EXIT
sleep 45
cd $P && timeout 110m nice -n 5 python3 $P/tc_ab3.py
release
if grep -q "VERDICT PASS" "$OUT"; then
  cp -p $P/tc_reader.py $P/tc_reader.py.bak-453
  install -m 0664 /tmp/tc453/tc_reader.py $P/tc_reader.py && $P/commit.sh "tc_reader: sideways scanned pages turned upright before reading (Tesseract OSD) (453, tested)"
  B=$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))")
  [ "$B" = "None" ] && sudo -n /usr/bin/systemctl restart mps-ocr-worker.service && say "installed; worker restarted" || say "installed; worker busy - it picks the reader up at its next restart"
else
  say "NOT INSTALLED (verdict not PASS)"
fi

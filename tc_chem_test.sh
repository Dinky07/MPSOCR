#!/usr/bin/env bash
# 555 (Dino 6 Nov, DF2729 chemistry read left to right): test the test-cert reader with chemistry headings + rows
# (tc_chem_patch.py) against the live reader (tc_ab7.py). Started by the runner as mps-tc-chem; waits until the worker
# has nothing to read and the JT fix test is done (until 22:00, then skips), pauses the worker, tests, resumes it.
# On PASS: installs tc_reader.py and restarts the worker when idle. Nothing is re-read (Dino's call).
# Report: reports/tc reader chemistry test 2026-11-06.txt
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline; S=$R/_src/run/staged/code
OUT="$R/reports/tc reader chemistry test 2026-11-06.txt"
exec > >(tee -a "$OUT") 2>&1
say() { echo "$(date '+%F %T') $*"; }
cur() { python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))"; }
busy() {
  [ "$(find $R/in -maxdepth 2 -name '*.pdf' -not -path '*/CustomerOrders/*' | wc -l)" -gt 0 ] && return 0
  [ "$(cur)" != "None" ] && return 0
  [ -e $R/PAUSE ] && return 0
  [ "$(systemctl show -p ActiveState --value mps-jt-fix.service)" = "activating" ] && return 0
  systemctl is-active --quiet mps-numbers-redo.service mps-numbers-weekly.service mps-ocr-training.service mps-po-import.service && return 0
  return 1
}
say "chemistry test-cert reader test: waiting for the queue to finish"
idle=0
until [ $idle -ge 3 ]; do
  [ "$(date +%H%M)" -ge 2200 ] && { say "worker still busy at 22:00: test skipped (nothing changed)"; exit 0; }
  if busy; then idle=0; else idle=$((idle + 1)); fi
  sleep 60
done
say "queue finished"
[ "$(md5sum < $P/tc_reader.py | cut -c1-32)" = "dcea4e39dcba9b41ca528f06321257bb" ] || { say "tc_reader.py changed since the test was set up: test skipped"; exit 0; }
rm -rf /tmp/tc555 && mkdir -p /tmp/tc555 && cp -p $P/tc_reader.py /tmp/tc555/
python3 $S/tc_chem_patch.py /tmp/tc555/tc_reader.py && python3 -m py_compile /tmp/tc555/tc_reader.py || { say "PATCH FAILED"; exit 1; }
OWN=0; [ -e $R/PAUSE ] || { echo "tc reader chemistry test 555" > $R/PAUSE; OWN=1; say "worker paused"; }
release() { [ $OWN = 1 ] && rm -f $R/PAUSE && say "worker resumed"; OWN=0; }
trap release EXIT
sleep 45
cd $P && timeout 4h nice -n 5 python3 $S/tc_ab7.py
release
grep -q "VERDICT PASS" "$OUT" || { say "NOT INSTALLED (verdict not PASS)"; exit 0; }
cp -p $P/tc_reader.py $P/tc_reader.py.bak-555
install -m 0664 /tmp/tc555/tc_reader.py $P/tc_reader.py && $P/commit.sh "tc_reader: chemistry read as the page's headings + each heat's row, paired in code; factors per row (555, tested)" || exit 1
for i in $(seq 1 720); do [ "$(cur)" = "None" ] && break; sleep 5; done
[ "$(cur)" = "None" ] && sudo -n /usr/bin/systemctl restart mps-ocr-worker.service && say "installed; worker restarted" || say "installed; worker busy - restart it when idle"

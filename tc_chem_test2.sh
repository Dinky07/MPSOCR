#!/usr/bin/env bash
# 641 (Dino 10 Nov; 555 was skipped on 6 Nov, the worker busy past 22:00): test the test-cert reader with chemistry headings + rows
# + scan rules (tc_chem_patch.py, tc_scan_rules_patch.py) against the live reader (tc_ab8.py). Unit mps-tc-chem2; waits until the worker
# has nothing to read and the JT fix test is done (until Sat 14 Nov 16:00, then skips), pauses the worker, tests, resumes it.
# On PASS: installs tc_reader.py and restarts the worker when idle. Nothing is re-read (Dino's call).
# Report: reports/tc reader chemistry and scan rules test 2026-11.txt
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline; S=$R/_src/run/staged/code
OUT="$R/reports/tc reader chemistry and scan rules test 2026-11.txt"
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
  [ "$(date +%s)" -ge "$(date -d '2026-11-14 16:00' +%s)" ] && { say "worker still busy at Sat 14 Nov 16:00: test skipped (nothing changed)"; exit 0; }
  if busy; then idle=0; else idle=$((idle + 1)); fi
  sleep 60
done
say "queue finished"
[ "$(md5sum < $P/tc_reader.py | cut -c1-32)" = "dcea4e39dcba9b41ca528f06321257bb" ] || { say "tc_reader.py changed since the test was set up: test skipped"; exit 0; }
rm -rf /tmp/tc641 && mkdir -p /tmp/tc641 && cp -p $P/tc_reader.py /tmp/tc641/
python3 $S/tc_chem_patch.py /tmp/tc641/tc_reader.py && python3 $S/tc_scan_rules_patch.py /tmp/tc641/tc_reader.py && python3 -m py_compile /tmp/tc641/tc_reader.py || { say "PATCH FAILED"; exit 1; }
OWN=0; [ -e $R/PAUSE ] || { echo "tc reader chemistry test 641" > $R/PAUSE; OWN=1; say "worker paused"; }
release() { [ $OWN = 1 ] && rm -f $R/PAUSE && say "worker resumed"; OWN=0; }
trap release EXIT
sleep 45
cd $P && timeout 9h nice -n 5 python3 $S/tc_ab8.py
release
grep -q "VERDICT PASS" "$OUT" || { say "NOT INSTALLED (verdict not PASS)"; exit 0; }
cp -p $P/tc_reader.py $P/tc_reader.py.bak-641
install -m 0664 /tmp/tc641/tc_reader.py $P/tc_reader.py && $P/commit.sh "tc_reader: chemistry read as the page's headings + each heat's row, paired in code; handwritten DF / job on every page, repeats dropped, swapped yield/tensile (641, tested)" || exit 1
for i in $(seq 1 720); do [ "$(cur)" = "None" ] && break; sleep 5; done
[ "$(cur)" = "None" ] && sudo -n /usr/bin/systemctl restart mps-ocr-worker.service && say "installed; worker restarted" || say "installed; worker busy - restart it when idle"

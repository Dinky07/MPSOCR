#!/usr/bin/env bash
# 701 (Dino 11 Nov "fix and re-test"; the 643 test lost chemistry on Xinfeng / Jinan Rich / newer Huitong forms and its scorer
# crashed): the test-cert reader with chemistry headings + rows, a fall back to the 10 Nov prompt on pages that answer fails on,
# the Sumitomo factor legend, copies with a letter of the number off, NB8 = 8 inch (tc_chem_patch.py, tc_scan_rules_patch.py)
# against the live reader (tc_ab9.py: + 5 Sumitomo pages). Unit mps-tc-chem4: waits for another job's pause to end, pauses the
# worker (after its current page), tests, resumes it.
# On PASS: installs tc_reader.py and restarts the worker while it is paused. Nothing is re-read (Dino's call).
# Report: reports/tc reader chemistry and scan rules test 2026-11-12.txt
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline; S=$R/_src/run/staged/code
OUT="$R/reports/tc reader chemistry and scan rules test 2026-11-12.txt"
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
say "chemistry test-cert reader test (701, Dino 11 Nov: fix and re-test): pausing the worker"
for i in $(seq 1 240); do [ -e $R/PAUSE ] || break; sleep 30; done      # another job's pause (the Sumitomo check 700)
[ -e $R/PAUSE ] && { say "the worker stayed paused by another job for 2 h: test skipped"; exit 0; }
OWN=0; [ -e $R/PAUSE ] || { echo "tc reader chemistry test 701" > $R/PAUSE; OWN=1; say "worker paused"; }
release() { [ $OWN = 1 ] && rm -f $R/PAUSE && say "worker resumed"; OWN=0; }
trap release EXIT
for i in $(seq 1 360); do [ "$(cur)" = "None" ] && break; sleep 5; done
[ "$(cur)" = "None" ] || { say "worker still on a page after 30 min: test skipped"; exit 0; }
systemctl is-active --quiet mps-ocr-training.service mps-numbers-redo.service mps-numbers-weekly.service && { say "training running: test skipped"; exit 0; }
say "worker idle"
[ "$(md5sum < $P/tc_reader.py | cut -c1-32)" = "dcea4e39dcba9b41ca528f06321257bb" ] || { say "tc_reader.py changed since the test was set up: test skipped"; exit 0; }
rm -rf /tmp/tc701 && mkdir -p /tmp/tc701 && cp -p $P/tc_reader.py /tmp/tc701/
python3 $S/tc_chem_patch.py /tmp/tc701/tc_reader.py && python3 $S/tc_scan_rules_patch.py /tmp/tc701/tc_reader.py && python3 -m py_compile /tmp/tc701/tc_reader.py || { say "PATCH FAILED"; exit 1; }
sleep 20
cd $P && timeout 9h nice -n 5 python3 $S/tc_ab9.py
grep -q "VERDICT PASS" "$OUT" || { say "NOT INSTALLED (verdict not PASS)"; exit 0; }
cp -p $P/tc_reader.py $P/tc_reader.py.bak-701
install -m 0664 /tmp/tc701/tc_reader.py $P/tc_reader.py && $P/commit.sh "tc_reader: chemistry as headings + rows with the 10 Nov prompt as fall back; handwritten DF / job; repeats dropped; swapped yield/tensile; chemistry joined to item lines by item no.; Sumitomo factors, copies and NB inch sizes (641-701, tested)" || exit 1
sudo -n /usr/bin/systemctl restart mps-ocr-worker.service && say "installed; worker restarted" || say "installed; worker NOT restarted"
sleep 10
release

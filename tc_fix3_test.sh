#!/usr/bin/env bash
# 489 test (Dino 3 Nov: no heats on the Test Certs hold -> reader changes): started by mps-tc-fix3.timer at 20:00; waits
# until the worker has nothing to read (until 23:30, then skips), pauses it, tests the fixed test-cert reader
# (tc_ab4.py), resumes the worker; on PASS installs tc_reader.py and restarts the worker (idle). Done before 03:00.
# Report: reports/tc reader no-heats test 2026-11-03.txt
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline
OUT="$R/reports/tc reader no-heats test 2026-11-03.txt"
exec > >(tee -a "$OUT") 2>&1
say() { echo "$(date '+%F %T') $*"; }
busy() {
  [ "$(find $R/in -maxdepth 2 -name '*.pdf' -not -path '*/CustomerOrders/*' | wc -l)" -gt 0 ] && return 0
  [ "$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))")" != "None" ] && return 0
  systemctl is-active --quiet mps-numbers-redo.service mps-numbers-weekly.service mps-ocr-training.service mps-po-import.service && return 0
  return 1
}
say "no-heats test-cert reader test: waiting for the queue to finish"
idle=0
until [ $idle -ge 2 ]; do
  [ "$(date +%H%M)" -ge 2330 ] && { say "worker still busy at 23:30: test skipped (nothing changed)"; exit 0; }
  if busy; then idle=0; else idle=$((idle + 1)); fi
  sleep 60
done
say "queue finished"
[ "$(md5sum < $P/tc_reader.py | cut -c1-32)" = "d38b726fd815ff8d8d8c84efe19567a8" ] || { say "tc_reader.py changed since 17:00: test skipped"; exit 0; }
rm -rf /tmp/tc489 && mkdir -p /tmp/tc489 && cp -p $P/tc_reader.py /tmp/tc489/
python3 $P/tc_fix3_patch.py /tmp/tc489/tc_reader.py && python3 -m py_compile /tmp/tc489/tc_reader.py || { say "PATCH FAILED"; exit 1; }
OWN=0; [ -e $R/PAUSE ] || { echo "tc reader no-heats test 489" > $R/PAUSE; OWN=1; say "worker paused"; }
release() { [ $OWN = 1 ] && rm -f $R/PAUSE && say "worker resumed"; OWN=0; }
trap release EXIT
sleep 45
cd $P && timeout 6h nice -n 5 python3 $P/tc_ab4.py
release
if grep -q "VERDICT PASS" "$OUT"; then
  cp -p $P/tc_reader.py $P/tc_reader.py.bak-489
  install -m 0664 /tmp/tc489/tc_reader.py $P/tc_reader.py && $P/commit.sh "tc_reader: no-heats fixes - cert pages kept from the L/C rule, no-heats pages re-asked, unsure orientation by OCR, crop / landscape size (489, tested)"
  B=$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))")
  [ "$B" = "None" ] && sudo -n /usr/bin/systemctl restart mps-ocr-worker.service && say "installed; worker restarted" || say "installed; worker busy - it picks the reader up at its next restart"
else
  say "NOT INSTALLED (verdict not PASS)"
fi

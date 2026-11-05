#!/usr/bin/env bash
# 528 retest of 518 (Dino 5 Nov: held test certs read as "no items" / "not a test cert" -> another pass): started by
# mps-tc-rescue2.timer at 19:50; waits until the worker has nothing to read (until 23:30, then skips), pauses it, tests
# the reader with the second look (tc_ab6.py), resumes the worker. On PASS: installs tc_reader.py, restarts the worker
# when idle, and feeds every held test cert to be read again (tc_reread.py --apply). Done before 03:00.
# Report: reports/tc reader second look test 2 2026-11-05.txt
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline
OUT="$R/reports/tc reader second look test 2 2026-11-05.txt"
exec > >(tee -a "$OUT") 2>&1
say() { echo "$(date '+%F %T') $*"; }
cur() { python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))"; }
busy() {
  [ "$(find $R/in -maxdepth 2 -name '*.pdf' -not -path '*/CustomerOrders/*' | wc -l)" -gt 0 ] && return 0
  [ "$(cur)" != "None" ] && return 0
  systemctl is-active --quiet mps-numbers-redo.service mps-numbers-weekly.service mps-ocr-training.service mps-po-import.service && return 0
  return 1
}
say "second-look test-cert reader test: waiting for the queue to finish"
idle=0
until [ $idle -ge 2 ]; do
  [ "$(date +%H%M)" -ge 2330 ] && { say "worker still busy at 23:30: test skipped (nothing changed)"; exit 0; }
  if busy; then idle=0; else idle=$((idle + 1)); fi
  sleep 60
done
say "queue finished"
[ "$(md5sum < $P/tc_reader.py | cut -c1-32)" = "$(cat $R/work/tc_rescue/base.md5)" ] || { say "tc_reader.py changed since the test was set up: test skipped"; exit 0; }
rm -rf /tmp/tc528 && mkdir -p /tmp/tc528 && cp -p $P/tc_reader.py /tmp/tc528/
python3 $P/tc_rescue_patch.py /tmp/tc528/tc_reader.py && python3 -m py_compile /tmp/tc528/tc_reader.py || { say "PATCH FAILED"; exit 1; }
OWN=0; [ -e $R/PAUSE ] || { echo "tc reader second-look test 528" > $R/PAUSE; OWN=1; say "worker paused"; }
release() { [ $OWN = 1 ] && rm -f $R/PAUSE && say "worker resumed"; OWN=0; }
trap release EXIT
sleep 45
cd $P && timeout 6h nice -n 5 python3 $P/tc_ab6.py
release
grep -q "VERDICT PASS" "$OUT" || { say "NOT INSTALLED (verdict not PASS)"; exit 0; }
cp -p $P/tc_reader.py $P/tc_reader.py.bak-528
install -m 0664 /tmp/tc528/tc_reader.py $P/tc_reader.py && $P/commit.sh "tc_reader: second look at a file read with no items / heats - best way up, 2400 px, items in a dimensions block (518 + 528 keeps a first reading's items, tested)" || exit 1
for i in $(seq 1 720); do [ "$(cur)" = "None" ] && break; sleep 5; done
[ "$(cur)" = "None" ] && sudo -n /usr/bin/systemctl restart mps-ocr-worker.service || { say "installed; worker busy - held certs NOT fed (the old reader would read them)"; exit 0; }
say "installed; worker restarted"
sleep 20
cd $P && python3 $P/tc_reread.py --apply && say "held test certs fed to be read again"
echo "in/TestCerts: $(ls $R/in/TestCerts/*.pdf 2>/dev/null | wc -l) waiting | hold left: $(ls $R/out/TestCerts/hold/*.json 2>/dev/null | wc -l)"

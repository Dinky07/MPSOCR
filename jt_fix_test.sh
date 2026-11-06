#!/usr/bin/env bash
# 531 test (Dino 6 Nov "Yes, fix it"): started by mps-jt-fix.timer; waits until the worker has nothing to read (until
# Saturday 15:00, before the weekend training; then skips), pauses it, tests the JT reader fix (jt_ab.py), resumes;
# on PASS installs jt_reader.py + worker.py (worker restarted when idle) and sends the failed JT scans back to be read.
# Report: reports/jt reader fix test 2026-11-06.txt
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline
OUT="$R/reports/jt reader fix test 2026-11-06.txt"
exec > >(tee -a "$OUT") 2>&1
say() { echo "$(date '+%F %T') $*"; }
busy() {
  [ "$(find $R/in -maxdepth 2 -name '*.pdf' -not -path '*/CustomerOrders/*' | wc -l)" -gt 0 ] && return 0
  [ "$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))")" != "None" ] && return 0
  systemctl is-active --quiet mps-numbers-redo.service mps-numbers-weekly.service mps-ocr-training.service && return 0
  return 1
}
say "JT reader fix test: waiting for the worker to be idle"
idle=0
until [ $idle -ge 2 ]; do
  if [ "$(date +%u)" -ge 6 ] && [ "$(date +%H%M)" -ge 1500 ]; then say "still busy at Saturday 15:00: test skipped (nothing changed)"; exit 0; fi
  if busy; then idle=0; else idle=$((idle + 1)); fi
  sleep 60
done
WMD5=$(md5sum < $P/worker.py | cut -c1-32); JMD5=$(md5sum < $P/jt_reader.py | cut -c1-32)
rm -rf /tmp/jt531 && mkdir -p /tmp/jt531 && cp -p $P/jt_reader.py $P/worker.py /tmp/jt531/
python3 $P/jt_fix_patch.py /tmp/jt531/jt_reader.py /tmp/jt531/worker.py && python3 -m py_compile /tmp/jt531/jt_reader.py /tmp/jt531/worker.py || { say "PATCH FAILED"; exit 1; }
OWN=0; [ -e $R/PAUSE ] || { echo "jt reader fix test 531" > $R/PAUSE; OWN=1; say "worker paused"; }
release() { [ $OWN = 1 ] && rm -f $R/PAUSE && say "worker resumed"; OWN=0; }
trap release EXIT
sleep 45
cd $P && timeout 150m nice -n 5 python3 $P/jt_ab.py
release
grep -q "VERDICT PASS" "$OUT" || { say "NOT INSTALLED (verdict not PASS)"; exit 0; }
[ "$(md5sum < $P/worker.py | cut -c1-32)" = "$WMD5" ] && [ "$(md5sum < $P/jt_reader.py | cut -c1-32)" = "$JMD5" ] \
  || { say "worker.py / jt_reader.py changed during the test: NOT INSTALLED"; exit 0; }
cp -p $P/jt_reader.py $P/jt_reader.py.bak-531; cp -p $P/worker.py $P/worker.py.bak-531
install -m 0664 /tmp/jt531/jt_reader.py $P/jt_reader.py && install -m 0664 /tmp/jt531/worker.py $P/worker.py \
  && $P/commit.sh "JT reader: pages turned upright, non-JSON asked again / salvaged, a bad page held on its own (531, tested)"
for i in $(seq 1 30); do [ "$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))")" = "None" ] && break; sleep 20; done
sudo -n /usr/bin/systemctl restart mps-ocr-worker.service && say "installed; worker restarted"
# the failed JT scans go back to be read by the fixed reader (a _meta note so the worker reads them again)
for t in JobTransfers RoninJobTransfers; do
  mkdir -p $R/in/$t/_meta
  for f in $R/failed/$t/*.pdf; do
    [ -e "$f" ] || continue; n=$(basename "$f")
    python3 -c "import json,sys;json.dump({'original_name':sys.argv[1],'reread_of':'failed/$t/'+sys.argv[1],'why':'JT reader fix 531 (Dino 6 Nov)'},open(sys.argv[2],'w'))" "$n" "$R/in/$t/_meta/$n.json"
    mv "$f" "$R/in/$t/$n.part" && mv "$R/in/$t/$n.part" "$R/in/$t/$n" && rm -f "$f.error.txt" && say "re-read: $t/$n"
  done
done

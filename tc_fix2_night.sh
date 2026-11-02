#!/usr/bin/env bash
# 445 night test (Dino 2 Nov "Test the narrow version"), run once by mps-tc-fix2.timer at 01:00: waits until the worker
# has nothing to read (tonight's refeed) up to 02:00, pauses it, tests the narrow test-cert reader fix (tc_ab2.py),
# resumes the worker; on PASS installs tc_reader.py and restarts the worker (idle). Done before the 03:00 batch.
# Report: reports/tc reader test 2026-11-03.txt
set -uo pipefail
P=/srv/ocr/pipeline; S=$P; R=/srv/ocr/scans/pipeline
OUT="$R/reports/tc reader test 2026-11-03.txt"
exec > >(tee -a "$OUT") 2>&1
say() { echo "$(date '+%F %T') $*"; }
busy() {
  [ "$(find $R/in -maxdepth 2 -name '*.pdf' -not -path '*/CustomerOrders/*' | wc -l)" -gt 0 ] && return 0
  [ "$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))")" != "None" ] && return 0
  systemctl is-active --quiet mps-numbers-redo.service mps-numbers-weekly.service mps-ocr-training.service && return 0
  return 1
}
say "narrow test-cert reader test"
until ! busy; do
  [ "$(date +%H%M)" -ge 0200 ] && [ "$(date +%H%M)" -lt 0300 ] && { say "worker still busy at 02:00: test skipped (nothing changed)"; exit 0; }
  sleep 60
done
[ "$(md5sum < $P/tc_reader.py | cut -c1-32)" = "de7f15c5488ef65f547035fa799d26c1" ] || { say "tc_reader.py changed since 2 Nov: test skipped"; exit 0; }
rm -rf /tmp/tc445 && mkdir -p /tmp/tc445 && cp -p $P/tc_reader.py /tmp/tc445/
python3 $P/tc_fix2_patch.py /tmp/tc445/tc_reader.py && python3 -m py_compile /tmp/tc445/tc_reader.py || { say "PATCH FAILED"; exit 1; }
PYTHONPATH=$P python3 - <<'EOF' || { say "UNIT TEST FAILED"; exit 1; }
import importlib.util
spec = importlib.util.spec_from_file_location("n", "/tmp/tc445/tc_reader.py"); N = importlib.util.module_from_spec(spec); spec.loader.exec_module(N)
pages = [{"page": 1, "how": "vision", "answer": {"page_type": "mtc", "mill": "HULUDAO", "handwritten": {"job": "JOB 4122 A49797", "order": None},
          "order_numbers": [{"label": "ORDER NO.", "value": "4500186774"}], "items": [], "heats": [{"heat_no": "340", "chem": {"C": 30}}]}}]
f, _ = N.to_fields(N.merge(pages, "DOC280117-007.pdf", "Fittings"), "DOC280117-007.pdf", "Fittings")
print({k: f.get(k) for k in ("mps_po", "mps_job")}); assert f["mps_po"] == "A49797" and f["mps_job"] == "4122"
EOF
OWN=0; [ -e $R/PAUSE ] || { echo "tc reader test 445" > $R/PAUSE; OWN=1; say "worker paused"; }
release() { [ $OWN = 1 ] && rm -f $R/PAUSE && say "worker resumed"; OWN=0; }
trap release EXIT
sleep 45
cd $P && timeout 75m nice -n 5 python3 $S/tc_ab2.py
release
if grep -q "VERDICT PASS" "$OUT"; then
  cp -p $P/tc_reader.py $P/tc_reader.py.bak-445
  install -m 0664 /tmp/tc445/tc_reader.py $P/tc_reader.py && $P/commit.sh "tc_reader: a certificate page answered empty is asked once more, handwritten job / order (445, tested)"
  B=$(python3 -c "import json;print(json.load(open('$R/status.json')).get('current'))")
  [ "$B" = "None" ] && sudo -n /usr/bin/systemctl restart mps-ocr-worker.service && say "installed; worker restarted" || say "installed; worker busy - it picks the reader up at its next restart"
else
  say "NOT INSTALLED (verdict not PASS)"
fi

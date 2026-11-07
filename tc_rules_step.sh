#!/usr/bin/env bash
# 591: the training chain's test-cert steps (Dino 7 Nov "apply the rules and the read documents from the test certs fork
# and prepare the test cert training"):
#   1. tc_rules_test.py: the reader with the 7 Nov reading rules vs the live reader on up to 100 test certs; on PASS the
#      new tc_reader.py is installed (the live file must be the one tested against) and the worker restarted while paused
#   2. tc_training.py quick: worked examples tried on the 384 checked certs (test set 100), kept only when better
# The GPU must be free of training (the caller unloads; Ollama loads the TC model itself). Report lines go to stdout.
set -uo pipefail
P=/srv/ocr/pipeline; R=/srv/ocr/scans/pipeline
say() { echo "$(date '+%F %T') $*"; }
OUT="$R/reports/tc rules test $(date +%Y-%m-%d).txt"
LIVE_MD5=${LIVE_MD5:-dcea4e39dcba9b41ca528f06321257bb}
say "TC 1: reading rules test (log: $OUT)"
cd $P && python3 $P/tc_rules_test.py 100 > "$OUT" 2>&1
grep -E '^(model|OLD|NEW|controls|VERDICT|report)' "$OUT"
if grep -q "^VERDICT PASS" "$OUT"; then
    if [ "$(md5sum < $P/tc_reader.py | cut -c1-32)" = "$LIVE_MD5" ]; then
        cp -p $P/tc_reader.py $P/tc_reader.py.bak-591 && install -m 0664 $P/tc_reader.591.py $P/tc_reader.py \
          && $P/commit.sh "TC reader: reading rules from the certs checked by eye (591, tested on the test certs)" >/dev/null 2>&1
        say "TC reading rules INSTALLED"
        sudo -n /usr/bin/systemctl restart mps-ocr-worker && say "worker restarted (still paused) to load them"
    else say "tc_reader.py changed since the test copy was made: NOT installed"; fi
else say "TC reading rules NOT installed (verdict not PASS: Dino picks from the report)"; fi
say "TC 2: quick training on the checked certs (test set 100)"
cd $P && IN_QUEUE=1 python3 $P/tc_training.py quick || say "TC quick training: exit $?"
python3 -c "import json;j=json.load(open('$R/training/job.json'));print('TC quick:', j.get('state'), j.get('summary'))" 2>/dev/null

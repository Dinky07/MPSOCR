#!/usr/bin/env bash
# Numbers pass: build the dataset from every checked purchase order (+ the current reader on the test orders, via
# Ollama) -> pause the worker, unload Ollama -> smoke test -> fine-tune the 3B numbers model -> score it -> resume.
# Runs as the last step of the weekly training queue (nightly_training.py, Saturday 17:00, IN_QUEUE=1), or ad hoc as the
# transient unit mps-numbers (runner). Nothing goes live: the result is /srv/ml/numbers/run-<stamp>/eval.json and the
# report training/reports/numbers-<stamp>.json.
set -uo pipefail
P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; D=/srv/ml/numbers
ROOT=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["root"])')
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
RUNID=run-$(date +%Y%m%d-%H%M); R=$D/$RUNID
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
LATEST=${LATEST_START:-0100}         # don't start fine-tuning between 01:00 and 12:00 (the worker must be back for 03:00)
TRAIN_MAX=${TRAIN_MAX:-6h}
TEST_MAX=${TEST_MAX:-150}
mkdir -p $R; [ "$(id -u)" = 0 ] && chown -R dinoc:dinoc $D
as_dinoc() { if [ "$(id -un)" = dinoc ]; then env HF_HOME=$HF_HOME PYTHONUNBUFFERED=1 "$@"; else sudo -u dinoc -E "$@"; fi; }
exec > >(tee -a $R/run.log) 2>&1
cd $D                                # Unsloth writes its compile cache in the working directory
say() { echo "$(date '+%F %T') $*"; }
MADE_PAUSE=0
resume() { [ $MADE_PAUSE = 1 ] && rm -f "$ROOT/PAUSE" && say "worker resumed"; }
trap resume EXIT
unload() {
    for m in $(curl -s $OLL/api/ps | python3 -c 'import json,sys;print(" ".join(x["name"] for x in json.load(sys.stdin).get("models",[])))'); do
        curl -s $OLL/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null && say "unloaded $m"
    done; sleep 5
}

say "numbers pass $RUNID"
# 1. ad hoc: wait for the training queue and any other GPU job (in the queue that has already been done)
if [ "${IN_QUEUE:-0}" != 1 ]; then
    while systemctl is-active --quiet mps-ocr-training.service || pgrep -f "training\.py (quick|finetune)" >/dev/null; do sleep 60; done
    MAX_WAIT=7200 as_dinoc $P/gpu_wait.sh || { say "GPU busy - stopping"; exit 1; }
fi

# 2. dataset + the current reader's reading of the test orders
if [ ! -e "$ROOT/PAUSE" ]; then echo "numbers pass fine-tune ($RUNID)" > "$ROOT/PAUSE"; MADE_PAUSE=1; say "worker paused"; fi
sleep 90                             # let the worker finish the file it is on
if [ "${REUSE_DATA:-0}" = 1 ] && [ -s $D/data/test.jsonl ] && [ -s $D/data/train.jsonl ]; then say "reusing the dataset built earlier"
else say "building the dataset"; as_dinoc python3 $P/numbers_data.py --out $D/data --test-max $TEST_MAX || { say "dataset failed"; exit 1; }; fi
say "train $(wc -l < $D/data/train.jsonl) / test $(wc -l < $D/data/test.jsonl) orders"

# 3. fine-tune
[ "$(date +%H%M)" -ge "$LATEST" ] && [ "$(date +%H%M)" -lt 1200 ] && { say "too late to start fine-tuning; dataset kept"; exit 0; }
unload
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
say "smoke test (8 orders, 2 steps)"
as_dinoc $V $P/numbers_train.py --data $D/data --out $D/smoke --limit 8 --max-steps 2 || { say "smoke test failed"; exit 1; }
rm -rf $D/smoke
say "fine-tuning (limit $TRAIN_MAX)"
as_dinoc timeout $TRAIN_MAX $V $P/numbers_train.py --data $D/data --out $R || { say "fine-tuning failed or ran out of time"; exit 1; }

# 4. score on the test orders, and a report beside the other training reports
say "scoring"
as_dinoc $V $P/numbers_eval.py --data $D/data --run $R || { say "scoring failed"; exit 1; }
python3 - "$R" "$ROOT/training/reports/numbers-${RUNID#run-}.json" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
r = Path(sys.argv[1]); e = json.load(open(r / "eval.json"))["results"]; i = json.load(open(r / "train_info.json"))
tests = sum(1 for _ in open(r.parent / "data" / "test.jsonl"))
def side(k):
    q, p = e[k]["qty"]["acc"], e[k]["price"]["acc"]
    return {"line_qty": q, "line_price": p, "overall": round((q + p) / 2, 4)}
cur, cand = side("main"), side("combined")
pc = lambda v: f"{v * 100:.1f} %"
# the Training tab (purchase orders) lists it under Job results: Before = current reader, After = with the numbers pass
rep = {"id": r.name, "kind": "numbers", "at": datetime.now().isoformat(timespec="seconds"), "run": str(r),
       "trained_on": i.get("orders"), "train_loss": i.get("train_loss"), "tests": tests, "lines": e["main"]["qty"]["counted"],
       "current": cur, "candidate": cand, "numbers_alone": side("numbers"), "numbers_used_on": e.get("numbers_used_on"),
       "secs_per_order": e.get("secs_per_order"), "installed": False,
       "why": (f"scored only, not live: line qty {pc(cur['line_qty'])} -> {pc(cand['line_qty'])}, line price "
               f"{pc(cur['line_price'])} -> {pc(cand['line_price'])} on {tests} test orders; trained on {i.get('orders')}")}
json.dump(rep, open(sys.argv[2], "w"), indent=1)
print("report:", sys.argv[2], "|", rep["why"])
PY
say "done: $R/eval.json"

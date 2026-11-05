#!/usr/bin/env bash
# Ronin JT numbers model (Dino 5 Nov 2026: qty + stock code from the RoninStock capture, the code built from the
# description where none is written): dataset from the Ronin JT lines sheet -> pause the worker, unload Ollama -> smoke
# test -> fine-tune Qwen2.5-VL-3B (QLoRA, as the PO numbers model) -> score it and the current reader on the test JTs.
# Nothing goes live: report training/reports/rjt-numbers-<stamp>.json; the JT pass in the worker comes after Dino has seen
# the score. Weekday limits as the PO pass: no start between 01:00 and 12:00, fine-tune capped at TRAIN_MAX (3 h).
set -uo pipefail
P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; D=/srv/ml/rjt
ROOT=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["root"])')
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
RUNID=run-$(date +%Y%m%d-%H%M); R=$D/$RUNID
BASE=${BASE:-unsloth/Qwen2.5-VL-3B-Instruct-bnb-4bit}
EPOCHS=${EPOCHS:-3}
TRAIN_MAX=${TRAIN_MAX:-3h}
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
mkdir -p $R; [ "$(id -u)" = 0 ] && chown -R dinoc:dinoc $D
as_dinoc() { if [ "$(id -un)" = dinoc ]; then env HF_HOME=$HF_HOME PYTHONUNBUFFERED=1 "$@"; else sudo -u dinoc -E "$@"; fi; }
exec > >(tee -a $R/run.log) 2>&1
cd $D
say() { echo "$(date '+%F %T') $*"; }
MADE_PAUSE=0
resume() { [ $MADE_PAUSE = 1 ] && rm -f "$ROOT/PAUSE" && say "worker resumed"; MADE_PAUSE=0; }
trap resume EXIT
trap 'say "stopped (signal)"; exit 1' INT TERM
unload() {
    for m in $(curl -s $OLL/api/ps | python3 -c 'import json,sys;print(" ".join(x["name"] for x in json.load(sys.stdin).get("models",[])))'); do
        curl -s $OLL/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null && say "unloaded $m"
    done; sleep 5
}
say "Ronin JT numbers model $RUNID (base $BASE)"
while systemctl is-active --quiet mps-ocr-training.service mps-numbers-weekly.service || pgrep -f "training\.py (quick|finetune)|numbers_train|numbers_ab" >/dev/null; do sleep 60; done
MAX_WAIT=7200 as_dinoc $P/gpu_wait.sh || { say "GPU busy - stopping"; exit 1; }
[ "$(date +%H%M)" -ge 0100 ] && [ "$(date +%H%M)" -lt 1200 ] && { say "too late to start (01:00-12:00)"; exit 0; }
if [ ! -e "$ROOT/PAUSE" ]; then echo "Ronin JT numbers fine-tune ($RUNID)" > "$ROOT/PAUSE"; MADE_PAUSE=1; say "worker paused"; fi
sleep 90
say "building the dataset from the Ronin JT lines sheet"
as_dinoc python3 $P/rjt_numbers_data.py --out $D/data || { say "dataset failed"; exit 1; }
NTRAIN=$(wc -l < $D/data/train.jsonl); NTEST=$(wc -l < $D/data/test.jsonl)
say "train $NTRAIN / test $NTEST JTs"
[ "$NTRAIN" -ge 150 ] || { say "fewer than 150 training JTs: not worth a fine-tune yet"; exit 0; }
unload
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
say "smoke test (8 JTs, 2 steps, scoring 2)"
rm -rf $D/smoke
as_dinoc $V $P/numbers_train.py --base $BASE --data $D/data --out $D/smoke --limit 8 --max-steps 2 || { say "smoke test failed"; exit 1; }
as_dinoc $V $P/rjt_numbers_eval.py --data $D/data --run $D/smoke --limit 2 || { say "smoke test of the scoring failed"; exit 1; }
rm -rf $D/smoke
say "fine-tuning $NTRAIN JTs x $EPOCHS epochs (limit $TRAIN_MAX)"
as_dinoc timeout $TRAIN_MAX $V $P/numbers_train.py --base $BASE --data $D/data --out $R --epochs $EPOCHS || { say "fine-tuning failed or ran out of time"; exit 1; }
say "scoring on $NTEST test JTs"
as_dinoc timeout 2h $V $P/rjt_numbers_eval.py --data $D/data --run $R || { say "scoring failed"; exit 1; }
resume
python3 - "$R" "$ROOT/training/reports/rjt-numbers-${RUNID#run-}.json" "$BASE" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
r, rep_p, base = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
e = json.load(open(r / "eval.json"))["results"]; i = json.load(open(r / "train_info.json"))
side = lambda k: {f: e[k][f]["acc"] for f in ("qty", "code", "code_near", "code_written", "code_built", "description", "code_in_stock", "lines_count")}
rep = {"id": r.name, "kind": "rjt-numbers", "at": datetime.now().isoformat(timespec="seconds"), "run": str(r), "base": base,
       "trained_on": i.get("orders"), "epochs": i.get("epochs"), "train_loss": i.get("train_loss"), "tests": e["tests"],
       "reader": side("main"), "model": side("model"), "secs_per_jt": e["secs_per_jt"], "installed": False}
pc = lambda v: "-" if v is None else f"{v * 100:.1f} %"
rep["why"] = (f"scored only, not live: stock code {pc(rep['reader']['code'])} (reader) -> {pc(rep['model']['code'])} (model), "
              f"written {pc(rep['model']['code_written'])} / built from the description {pc(rep['model']['code_built'])}; "
              f"qty {pc(rep['reader']['qty'])} -> {pc(rep['model']['qty'])}; codes in the stock list {pc(rep['model']['code_in_stock'])}; "
              f"{e['tests']} test JTs, trained on {i.get('orders')}")
json.dump(rep, open(rep_p, "w"), indent=1)
print("report:", rep_p, "|", rep["why"])
PY
say "done: $R"

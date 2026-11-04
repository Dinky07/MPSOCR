#!/usr/bin/env bash
# Numbers pass: build the dataset from every checked purchase order (+ the current reader on the test orders, via
# Ollama, cached) -> pause the worker, unload Ollama -> smoke test -> fine-tune the 3B numbers model -> score it AND the
# live model on the same test orders -> install the new one when it scores better -> resume.
# Runs as the last step of the weekly training queue (nightly_training.py, Saturday 17:00, IN_QUEUE=1), or ad hoc as the
# transient unit mps-numbers (runner).
# 335 (Dino 29 Oct): all checked orders (~4,000 train / 530 test), descriptions learnt too, auto-install. At the weekend
# (Saturday / Sunday) the job works to a deadline of Monday 02:30 (Dino: "training can be from Saturday 17:00 to Monday
# 03:00, there is no time limit"); ad hoc on a weekday the old limits stay (no start between 01:00 and 12:00, 6 h).
# Install rule: line qty + price (the pipeline's combined reading) better than the live model's by 0.5 point on the same
# test orders, neither of them worse by more than 1 point. Descriptions are applied by the pass only when the new model's
# beat the main reader's by 2 points (install.json "apply"). Report: training/reports/numbers-<stamp>.json.
set -uo pipefail
P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; D=/srv/ml/numbers
ROOT=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["root"])')
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
RUNID=run-$(date +%Y%m%d-%H%M); R=$D/$RUNID
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
TEST_MAX=${TEST_MAX:-0}              # 0 = every test order
EPOCHS=${EPOCHS:-2}
EVAL_BUDGET=${EVAL_BUDGET:-14400}    # seconds kept for scoring both models
DOW=$(date +%u)
if [ "$DOW" -ge 6 ]; then
    DEADLINE=$(date -d "next monday 02:30" +%s)
    WEEKEND=1
else
    WEEKEND=0
    LATEST=${LATEST_START:-0100}     # weekday ad hoc: don't start fine-tuning between 01:00 and 12:00
fi
mkdir -p $R; [ "$(id -u)" = 0 ] && chown -R dinoc:dinoc $D
as_dinoc() { if [ "$(id -un)" = dinoc ]; then env HF_HOME=$HF_HOME PYTHONUNBUFFERED=1 "$@"; else sudo -u dinoc -E "$@"; fi; }
exec > >(tee -a $R/run.log) 2>&1
cd $D                                # Unsloth writes its compile cache in the working directory
say() { echo "$(date '+%F %T') $*"; }
MADE_PAUSE=0
resume() { [ $MADE_PAUSE = 1 ] && rm -f "$ROOT/PAUSE" && say "worker resumed"; MADE_PAUSE=0; }
trap resume EXIT
trap 'say "stopped (signal)"; exit 1' INT TERM
left() { echo $(( DEADLINE - $(date +%s) )); }
unload() {
    for m in $(curl -s $OLL/api/ps | python3 -c 'import json,sys;print(" ".join(x["name"] for x in json.load(sys.stdin).get("models",[])))'); do
        curl -s $OLL/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null && say "unloaded $m"
    done; sleep 5
}

say "numbers pass $RUNID$([ $WEEKEND = 1 ] && echo " (weekend: deadline $(date -d @$DEADLINE '+%a %F %H:%M'))")"
ACTIVE=$(readlink -f $D/active 2>/dev/null || true)
say "live model: ${ACTIVE:-none}"
# 469 (Dino 4 Nov): a requested A/B (training/numbers_ab_request: Qwen2.5-VL vs Qwen3-VL on the corrected training
# sheet) runs in place of this pass; the request is used up
if [ -e "$ROOT/training/numbers_ab_request" ]; then
    rm -f "$ROOT/training/numbers_ab_request"; say "A/B requested: numbers_ab.sh runs instead of this pass"
    exec /bin/bash $P/numbers_ab.sh
fi
# 1. ad hoc: wait for the training queue and any other GPU job (in the queue that has already been done)
if [ "${IN_QUEUE:-0}" != 1 ]; then
    while systemctl is-active --quiet mps-ocr-training.service || pgrep -f "training\.py (quick|finetune)" >/dev/null; do sleep 60; done
    MAX_WAIT=7200 as_dinoc $P/gpu_wait.sh || { say "GPU busy - stopping"; exit 1; }
fi

# 2. dataset + the current reader's reading of the test orders (cached per reader setup)
if [ ! -e "$ROOT/PAUSE" ]; then echo "numbers pass fine-tune ($RUNID)" > "$ROOT/PAUSE"; MADE_PAUSE=1; say "worker paused"; fi
sleep 90                             # let the worker finish the file it is on
# 378: a restart later the same weekend reuses the dataset built since Saturday 17:00 (rebuilding takes ~2 h)
if [ -z "${REUSE_DATA:-}" ] && [ $WEEKEND = 1 ] && [ -s $D/data/test.jsonl ]; then
    WSTART=$([ "$DOW" = 6 ] && date -d "today 17:00" +%s || date -d "yesterday 17:00" +%s)
    [ "$(stat -c %Y $D/data/test.jsonl)" -ge "$WSTART" ] && REUSE_DATA=1
fi
if [ "${REUSE_DATA:-0}" = 1 ] && [ -s $D/data/test.jsonl ] && [ -s $D/data/train.jsonl ]; then say "reusing the dataset built earlier"
else say "building the dataset"; as_dinoc python3 $P/numbers_data.py --out $D/data --test-max $TEST_MAX || { say "dataset failed"; exit 1; }; fi
NTRAIN=$(wc -l < $D/data/train.jsonl); NTEST=$(wc -l < $D/data/test.jsonl)
say "train $NTRAIN / test $NTEST orders"

# 3. fine-tune
if [ $WEEKEND = 1 ]; then
    TRAIN_MAX=$(( $(left) - EVAL_BUDGET ))
    [ $TRAIN_MAX -lt 3600 ] && { say "less than an hour left for fine-tuning before the deadline; dataset kept"; exit 0; }
    TRAIN_MAX=${TRAIN_MAX}s
else
    [ "$(date +%H%M)" -ge "$LATEST" ] && [ "$(date +%H%M)" -lt 1200 ] && { say "too late to start fine-tuning; dataset kept"; exit 0; }
    TRAIN_MAX=${TRAIN_MAX:-6h}
fi
unload
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
say "smoke test (8 orders, 2 steps, then scoring 2 test orders)"
as_dinoc $V $P/numbers_train.py --data $D/data --out $D/smoke --limit 8 --max-steps 2 || { say "smoke test failed"; exit 1; }
as_dinoc $V $P/numbers_eval.py --data $D/data --run $D/smoke --limit 2 || { say "smoke test of the scoring failed"; exit 1; }
rm -rf $D/smoke
say "fine-tuning $NTRAIN orders x $EPOCHS epochs (limit $TRAIN_MAX)"
as_dinoc timeout $TRAIN_MAX $V $P/numbers_train.py --data $D/data --out $R --epochs $EPOCHS || { say "fine-tuning failed or ran out of time"; exit 1; }

# 4. score the new model and the live one on the same test orders
EVAL_MAX=$([ $WEEKEND = 1 ] && echo "$(( $(left) - 900 ))s" || echo 4h)
say "scoring the new model (limit $EVAL_MAX)"
as_dinoc timeout $EVAL_MAX $V $P/numbers_eval.py --data $D/data --run $R || { say "scoring failed"; exit 1; }
if [ -n "$ACTIVE" ] && [ -d "$ACTIVE/lora" ]; then
    EVAL_MAX=$([ $WEEKEND = 1 ] && echo "$(( $(left) - 600 ))s" || echo 4h)
    say "scoring the live model $(basename $ACTIVE) on the same test orders (limit $EVAL_MAX)"
    as_dinoc timeout $EVAL_MAX $V $P/numbers_eval.py --data $D/data --run $ACTIVE --out-name eval-$RUNID.json \
        || { say "scoring the live model failed: the new model is not installed"; ACTIVE_FAILED=1; }
fi

# 5. decide, install, report
python3 - "$R" "${ACTIVE:-}" "$RUNID" "$ROOT/training/reports/numbers-${RUNID#run-}.json" "${ACTIVE_FAILED:-0}" <<'PY'
import json, os, sys
from datetime import datetime
from pathlib import Path
r, active, runid, rep_path, active_failed = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "1"
D = r.parent
e = json.load(open(r / "eval.json"))["results"]
i = json.load(open(r / "train_info.json"))
cur_e = None
if active and not active_failed and (Path(active) / f"eval-{runid}.json").exists():
    cur_e = json.load(open(Path(active) / f"eval-{runid}.json"))["results"]
acc = lambda res, k, f: (res.get(k, {}).get(f) or {}).get("acc")
def side(res, k="combined"):
    q, p, d = acc(res, k, "qty"), acc(res, k, "price"), acc(res, k, "description")
    return {"line_qty": q, "line_price": p, "line_description": d, "overall": round((q + p) / 2, 4)}
cand = side(e)
cur = side(cur_e) if cur_e else None
main = side(e, "main")
pc = lambda v: "-" if v is None else f"{v * 100:.1f} %"
if active_failed:
    install, why = False, "the live model could not be scored on the new test orders"
elif cur is None:
    install = cand["overall"] >= main["overall"] + 0.005
    why = f"no live model scored: qty + price {pc(main['overall'])} (reader) -> {pc(cand['overall'])}"
else:
    better = cand["overall"] >= cur["overall"] + 0.005
    no_worse = cand["line_qty"] >= cur["line_qty"] - 0.01 and cand["line_price"] >= cur["line_price"] - 0.01
    install = better and no_worse
    why = (f"qty + price {pc(cur['overall'])} (live) -> {pc(cand['overall'])} (new): "
           + ("installed" if install else "better by 0.5 point needed, neither worse by over 1 point")
           + f" | qty {pc(cur['line_qty'])} -> {pc(cand['line_qty'])}, price {pc(cur['line_price'])} -> {pc(cand['line_price'])}")
desc_new, desc_main = acc(e, "combined_desc", "description"), acc(e, "main", "description")
apply_desc = bool(desc_new is not None and desc_main is not None and desc_new >= desc_main + 0.02)
if install:
    json.dump({"apply": {"qty": True, "price": True, "description": apply_desc}, "installed_at": datetime.now().isoformat(timespec="seconds"),
               "replaced": active or None, "why": why}, open(r / "install.json", "w"), indent=1)
    if active:
        (D / "active.previous").write_text(active + "\n")
    tmp = D / "active.new"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(r, tmp)
    os.replace(tmp, D / "active")          # atomic: the pass reads either the old run or the new one
rep = {"id": r.name, "kind": "numbers", "at": datetime.now().isoformat(timespec="seconds"), "run": str(r),
       "trained_on": i.get("orders"), "epochs": i.get("epochs"), "train_loss": i.get("train_loss"), "tests": e.get("tests"),
       "lines": e["main"]["qty"]["counted"], "reader": main, "current": cur or main, "candidate": cand,
       "numbers_alone": side(e, "numbers"), "descriptions": {"reader": desc_main, "new_model": desc_new, "applied": apply_desc and install},
       "numbers_used_on": e.get("numbers_used_on"), "secs_per_order": e.get("secs_per_order"),
       "live_model": active or None, "installed": install,
       "why": why + (f"; descriptions {pc(desc_main)} (reader) -> {pc(desc_new)}: " + ("applied" if apply_desc and install else "not applied"))}
json.dump(rep, open(rep_path, "w"), indent=1)
print("report:", rep_path, "|", rep["why"])
PY
say "done: $R  (live model now: $(readlink -f $D/active))"

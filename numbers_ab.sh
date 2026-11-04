#!/usr/bin/env bash
# 469 (Dino 4 Nov "set up training on Qwen2.5 and 3 and A/B test them"): the numbers model fine-tuned twice the same way
# (QLoRA, rank 16, 2 epochs, full 1600 px pages) on the corrected training sheet (training/po_lines, Use = Y orders) -
#   A  Qwen2.5-VL-3B (the live model's base)      B  Qwen3-VL-4B (the nearest Qwen3 size)
# - then A, B and the live model scored on the same test orders (never trained by any of them). Nothing is installed:
# report training/reports/numbers-ab-<stamp>.json; Dino picks. Runs in place of the weekly numbers pass when
# training/numbers_ab_request exists (numbers_run.sh hands over; the request is removed), or ad hoc. Weekend deadline
# Monday 02:30 as the weekly pass; the worker is paused throughout and resumed at the end.
set -uo pipefail
P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; D=/srv/ml/numbers; DS=$D/data-sheet
ROOT=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["root"])')
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
STAMP=$(date +%Y%m%d-%H%M); AB=$D/ab-$STAMP
QA=${QA:-unsloth/Qwen2.5-VL-3B-Instruct-bnb-4bit}
QB=${QB:-unsloth/Qwen3-VL-4B-Instruct-unsloth-bnb-4bit}
EPOCHS=${EPOCHS:-2}
TEST_MAX=${TEST_MAX:-600}
EVAL_EACH=${EVAL_EACH:-5400}         # seconds kept per scoring (600 orders at ~8 s)
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
if [ "$(date +%u)" -ge 6 ]; then DEADLINE=$(date -d "next monday 02:30" +%s); else DEADLINE=${DEADLINE:-$(date -d "+30 hours" +%s)}; fi
mkdir -p $AB; [ "$(id -u)" = 0 ] && chown -R dinoc:dinoc $D
as_dinoc() { if [ "$(id -un)" = dinoc ]; then env HF_HOME=$HF_HOME PYTHONUNBUFFERED=1 "$@"; else sudo -u dinoc -E "$@"; fi; }
exec > >(tee -a $AB/run.log) 2>&1
cd $D
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
ACTIVE=$(readlink -f $D/active 2>/dev/null || true)
say "numbers A/B $STAMP: A $QA | B $QB | live ${ACTIVE:-none} | deadline $(date -d @$DEADLINE '+%a %F %H:%M')"
if [ "${IN_QUEUE:-0}" != 1 ]; then
    while systemctl is-active --quiet mps-ocr-training.service || pgrep -f "training\.py (quick|finetune)|numbers_train" >/dev/null; do sleep 60; done
    MAX_WAIT=7200 as_dinoc $P/gpu_wait.sh || { say "GPU busy - stopping"; exit 1; }
fi
if [ ! -e "$ROOT/PAUSE" ]; then echo "numbers A/B ($STAMP)" > "$ROOT/PAUSE"; MADE_PAUSE=1; say "worker paused"; fi
sleep 90

# 1. the dataset from the training sheet as Dino left it
say "building the dataset from the training sheet"
as_dinoc python3 $P/numbers_data_sheet.py --out $DS --test-max $TEST_MAX || { say "dataset failed"; exit 1; }
NTRAIN=$(wc -l < $DS/train.jsonl); NTEST=$(wc -l < $DS/test.jsonl)
say "train $NTRAIN / test $NTEST orders"

# 2. smoke tests (8 orders, 2 steps, scoring 2 orders) for both bases
unload
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
declare -A OK BASE=([A]=$QA [B]=$QB)
for k in A B; do
    rm -rf $D/smoke-$k
    say "smoke test $k (${BASE[$k]})"
    if as_dinoc $V $P/numbers_train.py --base ${BASE[$k]} --data $DS --out $D/smoke-$k --limit 8 --max-steps 2 \
       && as_dinoc $V $P/numbers_eval.py --data $DS --run $D/smoke-$k --limit 2; then OK[$k]=1; else OK[$k]=0; say "smoke test $k FAILED"; fi
    rm -rf $D/smoke-$k
done
[ "${OK[A]}${OK[B]}" = "00" ] && { say "both smoke tests failed: nothing trained"; exit 1; }

# 3. fine-tune each (equal epochs); the time left is shared out: A 45 %, B the rest, scoring time kept back
EVALS=$(( (${OK[A]} + ${OK[B]} + 1) * EVAL_EACH ))
for k in A B; do
    [ "${OK[$k]}" = 1 ] || continue
    budget=$(( $(left) - EVALS - 900 ))
    [ $k = A ] && [ "${OK[B]}" = 1 ] && budget=$(( budget * 45 / 100 ))
    [ $budget -lt 3600 ] && { say "less than an hour left to fine-tune $k: skipped"; OK[$k]=0; continue; }
    say "fine-tuning $k (${BASE[$k]}): $NTRAIN orders x $EPOCHS epochs (limit ${budget}s)"
    t0=$(date +%s)
    if as_dinoc timeout ${budget}s $V $P/numbers_train.py --base ${BASE[$k]} --data $DS --out $AB/$k --epochs $EPOCHS; then
        say "fine-tuned $k in $(( ($(date +%s) - t0) / 60 )) min"
    else
        say "fine-tuning $k failed or ran out of time"; OK[$k]=0
    fi
done

# 4. score A, B and the live model on the same test orders
for k in A B live; do
    if [ $k = live ]; then
        [ -n "$ACTIVE" ] && [ -d "$ACTIVE/lora" ] || continue
        mkdir -p $AB/live && ln -sfn $ACTIVE/lora $AB/live/lora; RUN=$AB/live
    else
        [ "${OK[$k]}" = 1 ] || continue; RUN=$AB/$k
    fi
    lim=$(( $(left) - 600 )); [ $lim -lt 1200 ] && { say "no time left to score $k"; continue; }
    say "scoring $k (limit ${lim}s)"
    as_dinoc timeout ${lim}s $V $P/numbers_eval.py --data $DS --run $RUN || say "scoring $k failed"
done
resume

# 5. report
python3 - "$AB" "$ROOT/training/reports/numbers-ab-$STAMP.json" "$QA" "$QB" "$ACTIVE" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
ab, rep_p, qa, qb, active = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
def side(k):
    p = ab / k / "eval.json"
    if not p.exists():
        return None
    e = json.load(open(p))
    res = e["results"]
    out = {}
    for kind in ("numbers", "combined", "main"):
        if kind in res:
            q, pr = res[kind]["qty"]["acc"], res[kind]["price"]["acc"]
            d = (res[kind].get("description") or {}).get("acc")
            out[kind] = {"line_qty": q, "line_price": pr, "line_description": d, "overall": round((q + pr) / 2, 4)}
    out["secs_per_order"] = res.get("secs_per_order") or e.get("secs_per_order")
    ti = ab / k / "train_info.json"
    if ti.exists():
        out["train"] = json.load(open(ti))
    return out
r = {"id": ab.name, "kind": "numbers-ab", "at": datetime.now().isoformat(timespec="seconds"), "data": json.load(open(ab.parent / "data-sheet" / "source.json")),
     "A": {"base": qa, **(side("A") or {"failed": True})}, "B": {"base": qb, **(side("B") or {"failed": True})},
     "live": {"run": active, **(side("live") or {"failed": True})}, "installed": False}
pc = lambda v: "-" if v is None else f"{v * 100:.1f} %"
lines = []
for k in ("live", "A", "B"):
    s = r[k].get("combined") or r[k].get("numbers")
    lines.append(f"{k}: " + ("failed" if not s else f"qty {pc(s['line_qty'])}, price {pc(s['line_price'])}, description {pc(s.get('line_description'))}"))
reader = (r["live"].get("main") or r["A"].get("main") or {})
r["reader"] = reader
r["why"] = "scored only, not installed (Dino picks): " + " | ".join(lines) + (f" | reader alone: qty {pc(reader.get('line_qty'))}, price {pc(reader.get('line_price'))}" if reader else "")
json.dump(r, open(rep_p, "w"), indent=1)
print("report:", rep_p, "|", r["why"])
PY
say "done: $AB"

#!/usr/bin/env bash
# 469 (Dino 4 Nov "set up training on Qwen2.5 and 3 and A/B test them"): the numbers model fine-tuned twice the same way
# (QLoRA, rank 16, full 1600 px pages) on the corrected training sheet (training/po_lines, Use = Y orders) -
#   A  Qwen2.5-VL-3B (the live model's base)      B  Qwen3-VL-4B (the nearest Qwen3 size)
# - then A, B and the live model scored on the same test orders (never trained by any of them). Nothing is installed:
# report training/reports/numbers-ab-<stamp>.json; Dino picks. Runs in place of the weekly numbers pass when
# training/numbers_ab_request exists (numbers_run.sh hands over; the request is removed), or ad hoc. Weekend deadline
# Monday 02:30 as the weekly pass; the worker is paused throughout and resumed at the end.
# 561 (Dino 6 Nov "prepare everything for the training this weekend and check it will go off without problems"):
#  - the dataset is built before the worker is paused (CPU only)
#  - the GPU is kept for training: the timers whose jobs load a model on the GPU (route sort, TC copies, the PO check's
#    numbers pass) are stopped for the run and started again at the end; Ollama is unloaded and the GPU checked free
#    before every training and scoring step (the RJT run on 5 Nov failed with 1.7 GB free)
#  - timed smoke tests on the 48 longest orders (the worst case for memory) measure each model's speed; the epochs are
#    then set so that both trainings and all three scorings fit before the deadline (at most EPOCHS, the same for A and
#    B); 2 epochs of 5,376 orders on both would need ~37 h of the ~31 h there are, and a run cut off by the time limit
#    used to leave no adapter at all
#  - training and scoring stop gracefully at their time limit (adapter kept / scored orders kept)
#  SMOKE_ONLY=1: smoke tests and the plan only (nothing trained), PLAN_LEFT=<s> plans as if that much time were left.
set -uo pipefail
P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; D=/srv/ml/numbers; DS=$D/data-sheet
ROOT=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["root"])')
OLL=$(python3 -c 'import json;print(json.load(open("/srv/ocr/pipeline/config.json"))["ollama_url"].rstrip("/"))')
STAMP=$(date +%Y%m%d-%H%M); AB=$D/ab-$STAMP
SMOKE_ONLY=${SMOKE_ONLY:-0}; [ "$SMOKE_ONLY" = 1 ] && AB=$D/ab-smoke-$STAMP
QA=${QA:-unsloth/Qwen2.5-VL-3B-Instruct-bnb-4bit}
QB=${QB:-unsloth/Qwen3-VL-4B-Instruct-unsloth-bnb-4bit}
EPOCHS=${EPOCHS:-2}                  # at most; fewer when both do not fit before the deadline
TEST_MAX=${TEST_MAX:-0}              # 0 = every usable test order (Dino 4 Nov "Yes, use all")
SMOKE_STEPS=${SMOKE_STEPS:-6}
GPU_TIMERS=${GPU_TIMERS:-"mps-route-sort.timer mps-tc-copies.timer mps-po-check.timer"}
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
if [ "$(date +%u)" -ge 6 ]; then DEADLINE=${DEADLINE:-$(date -d "next monday 02:30" +%s)}; else DEADLINE=${DEADLINE:-$(date -d "+30 hours" +%s)}; fi
mkdir -p $AB; [ "$(id -u)" = 0 ] && chown -R dinoc:dinoc $D
as_dinoc() { if [ "$(id -un)" = dinoc ]; then env HF_HOME=$HF_HOME PYTHONUNBUFFERED=1 "$@"; else sudo -u dinoc -E "$@"; fi; }
exec > >(tee -a $AB/run.log) 2>&1
cd $D
say() { echo "$(date '+%F %T') $*"; }
MADE_PAUSE=0; STOPPED=""; PW=""
resume() {
    [ -n "$PW" ] && kill $PW 2>/dev/null; PW=""
    [ $MADE_PAUSE = 1 ] && rm -f "$ROOT/PAUSE" && say "worker resumed"; MADE_PAUSE=0
    for t in $STOPPED; do sudo -n /usr/bin/systemctl start $t && say "started $t again"; done; STOPPED=""
}
trap resume EXIT
trap 'say "stopped (signal)"; exit 1' INT TERM
left() { echo $(( DEADLINE - $(date +%s) )); }
unload() {
    for m in $(curl -s $OLL/api/ps | python3 -c 'import json,sys;print(" ".join(x["name"] for x in json.load(sys.stdin).get("models",[])))'); do
        curl -s $OLL/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null && say "unloaded $m"
    done; sleep 5
}
state() { systemctl show -p ActiveState --value "$1" 2>/dev/null; }
gpu_jobs_running() {
    for t in $GPU_TIMERS; do s=${t%.timer}.service; case "$(state $s)" in active|activating|reloading) printf '%s ' "$s";; esac; done
}
guard_on() {                         # 561: no other job loads a model on the GPU while this runs
    for t in $GPU_TIMERS; do
        [ "$(state $t)" = active ] || continue
        sudo -n /usr/bin/systemctl stop $t && STOPPED="$STOPPED $t"
    done
    [ -n "$STOPPED" ] && say "GPU kept for training: stopped$STOPPED (started again at the end)"
    local w=0 r
    while r=$(gpu_jobs_running); [ -n "$r" ]; do
        if [ $w -ge 2700 ] && [[ "$r" == *mps-po-check* ]]; then   # waits for a day's batch; not an Ollama client
            sudo -n /usr/bin/systemctl stop mps-po-check.service && say "stopped mps-po-check.service after 45 min (it runs again tomorrow)"
            sleep 20; w=$((w + 60)); [ $w -ge 5400 ] && break; continue
        fi
        [ $w -ge 5400 ] && { say "still running after 90 min: $r- going on"; break; }
        [ $((w % 600)) -eq 0 ] && say "waiting for ${r}to finish (never stopped mid-request)"
        sleep 60; w=$((w + 60))
    done
}
gpu_free() {                         # 561: Ollama unloaded and the card free before a training / scoring step
    local i used
    for i in $(seq 1 20); do
        unload
        used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' ')
        [ "${used:-99999}" -lt 2500 ] && { say "GPU free (${used} MiB in use)"; return 0; }
        [ $i = 1 ] && say "GPU has ${used} MiB in use: $(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader | tr '\n' ' ')"
        sleep 30
    done
    say "GPU not free after 10 min (${used} MiB in use)"; return 1
}
peak_on() { ( while :; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; sleep 5; done ) > $AB/gpu-$1.txt 2>/dev/null & PW=$!; }
peak_off() { [ -n "$PW" ] && kill $PW 2>/dev/null; PW=""; sort -n $AB/gpu-$1.txt 2>/dev/null | tail -1; }
jget() { python3 -c 'import json,sys
v=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): v=(v or {}).get(k)
print("" if v is None else v)' "$1" "$2" 2>/dev/null; }

ACTIVE=$(readlink -f $D/active 2>/dev/null || true)
say "numbers A/B $STAMP$([ $SMOKE_ONLY = 1 ] && echo " (SMOKE TEST ONLY)"): A $QA | B $QB | live ${ACTIVE:-none} | deadline $(date -d @$DEADLINE '+%a %F %H:%M')"
if [ "${IN_QUEUE:-0}" != 1 ]; then
    while systemctl is-active --quiet mps-ocr-training.service || pgrep -f "training\.py (quick|finetune)|numbers_train" >/dev/null; do sleep 60; done
    MAX_WAIT=7200 as_dinoc $P/gpu_wait.sh || { say "GPU busy - stopping"; exit 1; }
fi

# 1. the dataset from the training sheet as Dino left it (CPU only: built before the worker is paused)
if [ "${REUSE_DATA:-0}" = 1 ] && [ -s $DS/train.jsonl ] && [ -s $DS/test.jsonl ]; then say "reusing the dataset in $DS"
else
    say "building the dataset from the training sheet"
    as_dinoc python3 $P/numbers_data_sheet.py --out $DS --test-max $TEST_MAX || { say "dataset failed"; exit 1; }
fi
NTRAIN=$(wc -l < $DS/train.jsonl); NTEST=$(wc -l < $DS/test.jsonl)
say "train $NTRAIN / test $NTEST orders"

if [ ! -e "$ROOT/PAUSE" ]; then echo "numbers A/B ($STAMP)" > "$ROOT/PAUSE"; MADE_PAUSE=1; say "worker paused"; fi
guard_on
# let the worker finish the file it is on (it stops between files: a 24-page test cert takes ~5 min, a 40-page JT scan
# ~40 min), so it cannot load Ollama again after the GPU is checked free; up to 2 h
sleep 30; w=0
while [ "$(python3 -c "import json;print(json.load(open('$ROOT/status.json')).get('current'))" 2>/dev/null)" != None ] && [ $w -lt 7200 ]; do
    [ $((w % 300)) -eq 0 ] && say "waiting for the worker to finish its file"; sleep 20; w=$((w + 20))
done

# 2. timed smoke tests on the longest orders (most memory), scoring 3 test orders, for both bases
SM=$D/smoke-data; rm -rf $SM; mkdir -p $SM; ln -s $DS/img $SM/img; cp $DS/prompt.txt $SM/ 2>/dev/null
python3 - "$DS" "$SM" "$((SMOKE_STEPS * 8))" <<'PY'
import json, sys
ds, sm, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
rows = [l for l in open(f"{ds}/train.jsonl", encoding="utf-8") if l.strip()]
rows.sort(key=lambda l: len(json.loads(l)["answer"]), reverse=True)
open(f"{sm}/train.jsonl", "w", encoding="utf-8").writelines(rows[:n])
tests = [l for l in open(f"{ds}/test.jsonl", encoding="utf-8") if l.strip()]
open(f"{sm}/test.jsonl", "w", encoding="utf-8").writelines(tests[:3])
print(f"smoke data: the {n} longest training orders (longest answer {len(json.loads(rows[0])['answer'])} chars), 3 test orders")
PY
declare -A OK=([A]=0 [B]=0) SPS=([A]=0 [B]=0) SPO=([A]=0 [B]=0) PEAK=([A]=0 [B]=0) BASE=([A]=$QA [B]=$QB)
for k in A B; do
    rm -rf $D/smoke-$k
    gpu_free || { say "smoke test $k skipped: GPU not free"; continue; }
    say "smoke test $k (${BASE[$k]}): $SMOKE_STEPS steps on the longest orders, then scoring 3 test orders"
    peak_on $k
    if as_dinoc $V $P/numbers_train.py --base ${BASE[$k]} --data $SM --out $D/smoke-$k --max-steps $SMOKE_STEPS \
       && as_dinoc $V $P/numbers_eval.py --data $SM --run $D/smoke-$k; then
        OK[$k]=1; SPS[$k]=$(jget $D/smoke-$k/train_info.json secs_per_step); SPO[$k]=$(jget $D/smoke-$k/eval.json results.secs_median)
    else say "smoke test $k FAILED"; fi
    PEAK[$k]=$(peak_off $k)
    [ "${OK[$k]}" = 1 ] && say "smoke test $k passed: ${SPS[$k]} s a step, ${SPO[$k]} s an order (barely trained), GPU peak ${PEAK[$k]} MiB"
    rm -rf $D/smoke-$k
done
rm -rf $SM
[ "${OK[A]}${OK[B]}" = "00" ] && { say "both smoke tests failed: nothing trained"; exit 1; }

# 3. the plan: the same epochs for A and B (at most EPOCHS) so both trainings and the three scorings fit
PLEFT=${PLAN_LEFT:-$(left)}
eval "$(python3 - "$NTRAIN" "$NTEST" "$PLEFT" "$EPOCHS" "${OK[A]}" "${OK[B]}" "${SPS[A]:-0}" "${SPS[B]:-0}" "${SPO[A]:-0}" "${SPO[B]:-0}" \
        "$([ -n "$ACTIVE" ] && [ -d "$ACTIVE/lora" ] && echo 1 || echo 0)" "$AB/plan.json" <<'PY'
import json, math, sys
ntr, nte, left, emax = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
ok = {"A": sys.argv[5] == "1", "B": sys.argv[6] == "1"}
sps = {"A": float(sys.argv[7] or 0) * 1.1, "B": float(sys.argv[8] or 0) * 1.1}
spo = {"A": float(sys.argv[9] or 0), "B": float(sys.argv[10] or 0)}
live = sys.argv[11] == "1"
spe = math.ceil(ntr / 8)                                  # steps per epoch (batch 1 x 8 accumulated)
base_e = 8.0 * 1.2                                        # the 3B scored 7.3-8.1 s an order; the answer is longer now
ratio = min(3.0, max(0.7, spo["B"] / spo["A"])) if ok["A"] and ok["B"] and spo["A"] else 1.5
ev = {"A": base_e if ok["A"] else 0, "B": base_e * ratio if ok["B"] else 0, "live": base_e if live else 0}
note = []
def fit(o):
    teval = nte * (ev["live"] + (ev["A"] if o["A"] else 0) + (ev["B"] if o["B"] else 0))
    per_epoch = spe * sum(sps[k] for k in "AB" if o[k])
    avail = left - teval - 3600                            # model loading, the dataset, margin
    return teval, (avail / per_epoch if per_epoch else 0)
teval, e = fit(ok)
if ok["A"] and ok["B"] and e < 0.5:
    note.append(f"both fit only {e:.2f} epochs: B (the slower) dropped, A trained alone")
    ok["B"] = False; ev["B"] = 0
    teval, e = fit(ok)
e = math.floor(min(emax, e) * 10) / 10
if e < 0.3:
    note.append("less than 0.3 epochs fit before the deadline: nothing trained")
    e = 0
bud = {k: (round(spe * e * sps[k] * 1.25) if ok[k] and e else 0) for k in "AB"}
est = {k: round(spe * e * sps[k]) if ok[k] else 0 for k in "AB"}
plan = {"epochs": e, "epochs_max": emax, "steps_per_epoch": spe, "train": ntr, "test": nte, "secs_left": left,
        "secs_per_step": sps, "eval_secs_per_order": ev, "eval_ratio_B_to_A": round(ratio, 2), "train_est_s": est,
        "train_limit_s": bud, "eval_est_s": {k: round(nte * v) for k, v in ev.items()}, "use": ok, "notes": note,
        "total_est_h": round((sum(est.values()) + teval) / 3600, 1)}
json.dump(plan, open(sys.argv[12], "w"), indent=1)
print(f"E={e}; OK[A]={int(ok['A'])}; OK[B]={int(ok['B'])}; BUD_A={bud['A']}; BUD_B={bud['B']}; EST_A={est['A']}; EST_B={est['B']}; "
      f"EV_A={round(nte * ev['A'])}; EV_B={round(nte * ev['B'])}; EV_L={round(nte * ev['live'])}; PLAN_NOTE='{'; '.join(note)}'")
PY
)"
say "plan: $E epochs each ($(jget $AB/plan.json steps_per_epoch) steps an epoch) | A ~$((EST_A / 3600)) h | B ~$((EST_B / 3600)) h | scoring A ~$((EV_A / 3600)) h, B ~$((EV_B / 3600)) h, live ~$((EV_L / 3600)) h | all ~$(jget $AB/plan.json total_est_h) h of $((PLEFT / 3600)) h${PLAN_NOTE:+ | $PLAN_NOTE}"
if [ "$SMOKE_ONLY" = 1 ]; then cp $AB/run.log "$ROOT/reports/numbers ab smoke test $STAMP.txt" 2>/dev/null; say "smoke test only: done"; exit 0; fi
[ "$E" = 0 ] && { say "nothing trained"; exit 1; }

# 4. fine-tune each with the same epochs; a graceful time limit keeps the adapter if a run is slower than measured
for k in A B; do
    [ "${OK[$k]}" = 1 ] || continue
    rest=$(( EV_A + EV_B + EV_L + 1800 )); [ $k = A ] && [ "${OK[B]}" = 1 ] && rest=$(( rest + EST_B ))
    bud=$([ $k = A ] && echo $BUD_A || echo $BUD_B); cap=$(( $(left) - rest )); [ $cap -lt $bud ] && bud=$cap
    [ $bud -lt 1800 ] && { say "less than 30 min left to fine-tune $k: skipped"; OK[$k]=0; continue; }
    gpu_free || { say "fine-tuning $k skipped: GPU not free"; OK[$k]=0; continue; }
    say "fine-tuning $k (${BASE[$k]}): $NTRAIN orders x $E epochs (time limit ${bud}s)"
    t0=$(date +%s)
    if as_dinoc timeout $(( bud + 2700 ))s $V $P/numbers_train.py --base ${BASE[$k]} --data $DS --out $AB/$k --epochs $E --max-seconds $bud \
       && [ -d $AB/$k/lora ]; then
        say "fine-tuned $k in $(( ($(date +%s) - t0) / 60 )) min: $(jget $AB/$k/train_info.json epochs_done) epochs, $(jget $AB/$k/train_info.json steps) steps$([ "$(jget $AB/$k/train_info.json stopped_at_time_limit)" = True ] && echo ' (stopped at the time limit)')"
    else
        say "fine-tuning $k failed"; OK[$k]=0
    fi
done

# 5. score A, B and the live model on the same test orders
for k in A B live; do
    if [ $k = live ]; then
        [ -n "$ACTIVE" ] && [ -d "$ACTIVE/lora" ] || continue
        mkdir -p $AB/live && ln -sfn $ACTIVE/lora $AB/live/lora; RUN=$AB/live
        [ -f $ACTIVE/prompt.txt ] && cp $ACTIVE/prompt.txt $AB/live/   # 497: the live model reads with the prompt it learnt
        est=$EV_L; rest=0
    else
        [ "${OK[$k]}" = 1 ] || continue; RUN=$AB/$k
        if [ $k = A ]; then est=$EV_A; rest=$(( ${OK[B]} * EV_B + EV_L )); else est=$EV_B; rest=$EV_L; fi
    fi
    lim=$(( $(left) - rest - 600 )); [ $lim -gt $(( est * 13 / 10 + 1800 )) ] && lim=$(( est * 13 / 10 + 1800 ))
    [ $lim -lt 1200 ] && { say "no time left to score $k"; continue; }
    gpu_free || { say "scoring $k skipped: GPU not free"; continue; }
    say "scoring $k (time limit ${lim}s)"
    as_dinoc timeout $(( lim + 1800 ))s $V $P/numbers_eval.py --data $DS --run $RUN --max-seconds $lim || say "scoring $k failed"
done
resume

# 6. report
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
            out[kind] = {"line_qty": q, "line_price": pr, "line_description": d,
                         "overall": round((q + pr) / 2, 4) if q is not None and pr is not None else None,
                         "supplier": (res[kind].get("supplier") or {}).get("acc"), "date": (res[kind].get("date") or {}).get("acc"),
                         "job": (res[kind].get("job") or {}).get("acc")}
    out["secs_per_order"] = res.get("secs_per_order") or e.get("secs_per_order")
    out["tests_scored"], out["scoring_stopped_at_time_limit"] = res.get("tests"), res.get("stopped_at_time_limit")
    ti = ab / k / "train_info.json"
    if ti.exists():
        out["train"] = json.load(open(ti))
    return out
plan = json.load(open(ab / "plan.json")) if (ab / "plan.json").exists() else None
r = {"id": ab.name, "kind": "numbers-ab", "at": datetime.now().isoformat(timespec="seconds"), "data": json.load(open(ab.parent / "data-sheet" / "source.json")),
     "plan": plan, "A": {"base": qa, **(side("A") or {"failed": True})}, "B": {"base": qb, **(side("B") or {"failed": True})},
     "live": {"run": active, **(side("live") or {"failed": True})}, "installed": False}
pc = lambda v: "-" if v is None else f"{v * 100:.1f} %"
lines = []
for k in ("live", "A", "B"):
    s, nm = r[k].get("combined") or r[k].get("numbers"), r[k].get("numbers") or {}
    lines.append(f"{k}: " + ("failed" if not s else f"qty {pc(s['line_qty'])}, price {pc(s['line_price'])}, description {pc(s.get('line_description'))}, "
                             f"supplier {pc(nm.get('supplier'))}, date {pc(nm.get('date'))}, job {pc(nm.get('job'))}"
                             + (f" ({r[k]['tests_scored']} orders)" if r[k].get("tests_scored") else "")))
reader = (r["live"].get("main") or r["A"].get("main") or {})
r["reader"] = reader
r["why"] = "scored only, not installed (Dino picks): " + (f"{plan['epochs']} epochs each | " if plan else "") + " | ".join(lines) + (
    f" | reader alone: qty {pc(reader.get('line_qty'))}, price {pc(reader.get('line_price'))}, supplier {pc(reader.get('supplier'))}, date {pc(reader.get('date'))}" if reader else "")
json.dump(r, open(rep_p, "w"), indent=1)
print("report:", rep_p, "|", r["why"])
PY
say "done: $AB"

#!/usr/bin/env bash
# 573 (Dino 6 Nov "and the Job Transfer training?"): the JT reader fine-tune - Qwen3-VL-8B (4-bit) + LoRA on the reviewed
# job transfers (scans/training/train.jsonl: 469 forms, 299 of them read by Claude and checked by Dino in part), scored
# on the 34 held-out test forms against the reader's own reading (eval_lora.py). Nothing goes live: the adapter is in
# /srv/ocr/models/jt-lora-<stamp>-<epochs>ep with test_predictions.jsonl and a report in training/reports.
# Runs in the training venv (train_lora.py patched for it); when its smoke test fails, the Unsloth docker image is tried
# (the way the 16 Oct run was made). The GPU must be free (Ollama unloaded by the caller).
#   jt_lora_run.sh <epochs> [max-seconds]
set -uo pipefail
EP=${1:-3}; MAXS=${2:-0}
SC=/srv/ocr/scans; P=/srv/ocr/pipeline; V=/srv/ml/train-venv/bin/python; R=/srv/ocr/scans/pipeline
STAMP=$(date +%Y%m%d-%H%M); OUT=/srv/ocr/models/jt-lora-$STAMP-${EP}ep
export HF_HOME=/srv/ocr/hf-cache PYTHONUNBUFFERED=1
say() { echo "$(date '+%F %T') $*"; }
cd $SC
NTR=$(wc -l < training/train.jsonl); NTE=$(wc -l < training/test.jsonl)
say "JT LoRA fine-tune: $NTR training forms, $NTE test forms, $EP epochs$([ "$MAXS" != 0 ] && echo ", time limit ${MAXS}s")"
rm -rf /tmp/jt-lora-smoke
if $V train_lora.py --train training/train.jsonl --out /tmp/jt-lora-smoke --limit 8 --max-steps 2 >/tmp/jt-lora-smoke.log 2>&1 \
   && [ -f /tmp/jt-lora-smoke/adapter_config.json ]; then
    say "smoke test passed in the training venv ($(python3 -c "import json;print(json.load(open('/tmp/jt-lora-smoke/train_info.json')).get('secs_per_step'))") s a step)"
    MODE=venv
else
    say "smoke test in the training venv failed ($(grep -E 'Error|error' /tmp/jt-lora-smoke.log | tail -1 | cut -c1-200)); trying the Unsloth docker image"
    RUN=(docker run --rm --gpus all --ipc=host -v /srv/ocr:/srv/ocr -w /srv/ocr/scans -e HF_HOME=/srv/ocr/models/hf --entrypoint bash unsloth/unsloth:latest -lc)
    rm -rf /tmp/jt-lora-smoke
    if "${RUN[@]}" "python train_lora.py --train training/train.jsonl --out /tmp/jt-lora-smoke --epochs 0.05" >/tmp/jt-lora-smoke-docker.log 2>&1 \
       && [ -f /tmp/jt-lora-smoke/adapter_config.json ]; then
        say "smoke test passed in the docker image"; MODE=docker
    else
        say "smoke test failed in the docker image too: JT fine-tune skipped ($(grep -E 'Error|error' /tmp/jt-lora-smoke-docker.log | tail -1 | cut -c1-200))"
        exit 1
    fi
fi
rm -rf /tmp/jt-lora-smoke
t0=$(date +%s)
if [ $MODE = venv ]; then
    $V train_lora.py --train training/train.jsonl --out $OUT --epochs $EP $([ "$MAXS" != 0 ] && echo "--max-seconds $MAXS") || { say "fine-tuning failed"; exit 1; }
    say "fine-tuned in $(( ($(date +%s) - t0) / 60 )) min; scoring on $NTE test forms"
    $V eval_lora.py --adapter $OUT > $OUT/eval.txt 2>&1 || { say "scoring failed"; tail -3 $OUT/eval.txt; exit 1; }
else
    "${RUN[@]}" "python train_lora.py --train training/train.jsonl --out $OUT --epochs $EP" || { say "fine-tuning failed"; exit 1; }
    say "fine-tuned in $(( ($(date +%s) - t0) / 60 )) min; scoring on $NTE test forms"
    "${RUN[@]}" "python eval_lora.py --adapter $OUT" > $OUT/eval.txt 2>&1 || { say "scoring failed"; tail -3 $OUT/eval.txt; exit 1; }
fi
sed -n '/^Test forms/,$p' $OUT/eval.txt
python3 - "$OUT" "$R/training/reports/jt-lora-$STAMP-${EP}ep.json" "$MODE" <<'PY'
import json, re, sys
from datetime import datetime
from pathlib import Path
out, rep, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
txt = (out / "eval.txt").read_text(encoding="utf-8", errors="replace")
fields = {m.group(1): {"before": float(m.group(2)), "after": float(m.group(3))}
          for m in re.finditer(r"^\s+(\S+)\s+([0-9.]+)%\s+([0-9.]+)%\s*$", txt, re.M)}
writers = {m.group(1): {"forms": int(m.group(2)), "before": float(m.group(3)), "after": float(m.group(4))}
           for m in re.finditer(r"^\s+(\S+)\s+(\d+) forms\s+([0-9.]+)% -> ([0-9.]+)%", txt, re.M)}
m = re.search(r"Test forms: (\d+)\s+unparseable outputs: (\d+)", txt)
info = json.loads((out / "train_info.json").read_text()) if (out / "train_info.json").exists() else {}
r = {"id": out.name, "kind": "jt-lora", "at": datetime.now().isoformat(timespec="seconds"), "adapter": str(out), "mode": mode,
     "train": info, "tests": int(m.group(1)) if m else None, "unparseable": int(m.group(2)) if m else None,
     "fields": fields, "writers_quantity": writers, "installed": False}
q = fields.get("quantity") or {}
r["why"] = (f"scored only, not live: {info.get('forms')} forms x {info.get('epochs_done', info.get('epochs'))} epochs on {info.get('base', '')}; "
            + ", ".join(f"{k} {v['before']:.1f} -> {v['after']:.1f} %" for k, v in fields.items()))
json.dump(r, open(rep, "w"), indent=1)
print("report:", rep, "|", r["why"])
PY
say "done: $OUT"

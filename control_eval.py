#!/usr/bin/env python3
"""Control for the 23 Oct fine-tune: the STOCK Qwen2.5-VL-7B (no fine-tuning) converted to GGUF exactly like the fine-tuned
model (llama.cpp, Q4_K_M + f16 projector, llamacpp runner), scored on the same 30 test orders. Tells apart "the training
made it worse" from "the conversion path makes any model worse". Report: training/reports/control-<stamp>.json."""
import glob, json, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, "/srv/ocr/pipeline")
import training as T, po_reader

cfg, t = T.load_cfg()
job = T.Job(cfg, "control")
rid = f"control-{datetime.now():%Y%m%d-%H%M%S}"
try:
    work = T.tdir(cfg) / "control" / "base-gguf"
    merged = work / "merged"
    if not (merged / "config.json").exists():
        snaps = sorted(glob.glob("/srv/ml/hf-cache/hub/models--unsloth--Qwen2.5-VL-7B-Instruct/snapshots/*"))
        if not snaps:
            job.finish("failed", "stock model not in /srv/ml/hf-cache"); sys.exit(1)
        job.step(f"copying the stock model from {snaps[-1]}")
        merged.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-rL", snaps[-1] + "/.", str(merged)], check=True)
    # the cache holds only the stock weights: tokenizer, chat template and image preprocessor settings are the same as the
    # fine-tune's (same base model), so take them from there
    ft = T.tdir(cfg) / "finetune" / "finetune-20261023-222709" / "merged"
    for f in ft.iterdir():
        if f.is_file() and not f.name.startswith("model") and f.name != "config.json" and not (merged / f.name).exists():
            shutil.copy2(f, merged / f.name)
            job.log(f"  {f.name} copied from the fine-tune's merged folder")
    name = "qwen25vl-stock-gguf"
    cmd, pmap = T.ollama_cli(t)
    job.step(f"converting the stock model to GGUF and importing as {name}")
    if not T.gguf_import(t, job, name, work, cmd, pmap):
        job.finish("failed", "conversion / import failed; see the log"); sys.exit(1)
    pos = T.po_examples(cfg)
    tests = T.test_set(t, pos)
    cur = T.po_settings(cfg)
    job.pause_worker("control scoring")
    a = T.evaluate(job, cfg, tests, cur["model"], cur["fewshot_dir"], cur["px"], "current setup (Ollama engine)")
    po_reader._FEW.clear()
    b = T.evaluate(job, cfg, tests, name, cur["fewshot_dir"], cur["px"], "same model + examples, converted via llama.cpp")
    po_reader._FEW.clear()
    c = T.evaluate(job, cfg, tests, name, cur["fewshot_dir"], int(t["ft_px"]), f"converted, at {t['ft_px']} px (as the fine-tune was scored)")
    rep = {"id": rid, "kind": "control", "at": datetime.now().isoformat(timespec="seconds"), "tests": len(tests),
           "current": a, "candidate": b, "candidate_alt": c, "installed": False,
           "why": f"control only: Ollama engine {a['overall']} vs llama.cpp-converted {b['overall']} (1600 px) / {c['overall']} ({t['ft_px']} px)"}
    T.write_json(T.tdir(cfg) / "reports" / f"{rid}.json", rep)
    job.finish("done", rep["why"])
except Exception as ex:
    job.finish("failed", f"{type(ex).__name__}: {ex}")
    raise

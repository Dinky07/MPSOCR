#!/usr/bin/env python3
"""Year accuracy of the purchase-order reader, old prompt vs a prompt with the "202L = 2024" example.
Takes N orders from out/PurchaseOrders whose scan folder names the year (the truth), reads each with both prompts
(same model, worked examples and image size the worker uses) and counts the years read right. Nothing is written
to the queue; the worker should be idle (PAUSE is created while this runs and removed after).

  python3 eval_dates.py [--root /srv/ocr/scans/pipeline] [--n 40] [--out report.json]
"""
import argparse, glob, json, os, random, re, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import po_reader, mps_jobs

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--n", type=int, default=40)
ap.add_argument("--out", default=None)
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--models", default=None, help="comma-separated Ollama models to compare with the current prompt (instead of old/new prompt)")
a = ap.parse_args()
cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
act = json.loads((Path(a.root) / "training" / "active.json").read_text(encoding="utf-8")) if (Path(a.root) / "training" / "active.json").exists() else {}
pc = ((cfg.get("doc_types") or {}).get("PurchaseOrders") or {}).get("po_reader") or {}
model = act.get("model") or pc.get("model", "qwen2.5vl:7b")
fewshot = act.get("fewshot_dir") or pc.get("fewshot_dir", str(HERE / "po_fewshot"))
px = int(act.get("px") or pc.get("px", 1600))

OLD = po_reader.PROMPT
NEW = OLD.replace(
    "  L.09.2025 = 4 September 2025). A real letter L (e.g. in \"EMAIL\") has a flat bottom with no flick.",
    "  L.09.2025 = 4 September 2025). The same in a year: \"202L\" is 2024, never 2025; \"201L\" is 2014. Her 5 has a flat\n"
    "  top stroke and an open loop. Read the year digit by digit; do not assume a year.\n"
    "  A real letter L (e.g. in \"EMAIL\") has a flat bottom with no flick.")
assert NEW != OLD, "prompt anchor not found"

docs = []
for j in sorted(glob.glob(f"{a.root}/out/PurchaseOrders/*.json")):
    side = json.loads(Path(j).read_text(encoding="utf-8"))
    y = mps_jobs.folder_year(side.get("source_folder"), side.get("source_file"))
    f = side.get("fields") or {}
    pdf = Path(j).with_suffix(".pdf")
    if y and pdf.exists() and f.get("form") == "mps_order" and f.get("date_as_written"):
        docs.append({"pdf": str(pdf), "year": y, "read_before": f.get("date"), "written": f.get("date_as_written")})
random.Random(a.seed).shuffle(docs)
docs = docs[:a.n]
print(f"{len(docs)} orders, truth = scan folder year; model {model}, {fewshot}, {px}px")

pause = Path(a.root) / "PAUSE"
made = not pause.exists()
if made:
    pause.write_text("eval_dates.py\n")
res = {"model": model, "n": len(docs), "variants": {}}
try:
    variants = [(m, OLD, m) for m in a.models.split(",")] if a.models else [("old", OLD, model), ("new", NEW, model)]
    for name, prompt, model in variants:
        po_reader.PROMPT = prompt
        po_reader._FEW.clear()
        right = 0; rows = []; t0 = time.time()
        for d in docs:
            try:
                data, _, _ = po_reader.read(d["pdf"], cfg["ollama_url"], model, fewshot, 2, px, int(cfg.get("ollama_timeout", 1200)), "4h")
                got = data.get("date_iso") or ""
            except Exception as ex:
                got = f"ERR {ex}"[:60]
            ok = got[:4] == str(d["year"])
            right += ok
            rows.append({"pdf": os.path.basename(d["pdf"]), "truth_year": d["year"], "got": got, "written": data.get("date_as_written") if isinstance(data, dict) else None, "ok": ok})
            print(f"  [{name}] {os.path.basename(d['pdf'])[:50]:<50} {got:<12} {'ok' if ok else 'WRONG'}", flush=True)
        res["variants"][name] = {"year_right": right, "of": len(docs), "acc": round(right / max(1, len(docs)), 3), "secs": round(time.time() - t0), "rows": rows}
        print(f"{name}: year right {right}/{len(docs)} = {res['variants'][name]['acc']}  ({res['variants'][name]['secs']} s)")
finally:
    if made:
        pause.unlink(missing_ok=True)
if a.out:
    Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")

#!/usr/bin/env python3
"""Numbers pass, shadow run (Dino 26 Oct: "go ahead with step one"): the fine-tuned numbers model reads the line
quantities and unit prices of a day's purchase orders WITHOUT changing anything the pipeline uses. Per order it stores
what it read under "numbers_pass" in the sidecar (fields untouched) and writes a report comparing it with the current
reader and - where the order has been checked (the day's checking file, imported at 19:30) - with the checked values.
Also records the GPU memory the model needs, to decide whether it can sit beside Ollama or must run as a batch.
Runs in the training venv (numbers_shadow_run.sh pauses the worker and unloads Ollama around it).

    numbers_shadow.py --run /srv/ml/numbers/run-<stamp> --date 2026-10-26 [--limit N]
"""
import argparse, csv, json, os, re, subprocess, tempfile, time
from datetime import datetime
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--run", required=True, help="the trained run folder (its lora/)")
ap.add_argument("--data", default="/srv/ml/numbers/data", help="for the prompt the model was trained with")
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="orders read on this day (processed_at)")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--px", type=int, default=1600)
a = ap.parse_args()

from unsloth import FastVisionModel                              # noqa: E402
from PIL import Image                                            # noqa: E402
import torch                                                     # noqa: E402

root, run = Path(a.root), Path(a.run)
PROMPT = json.loads(open(Path(a.data) / "test.jsonl", encoding="utf-8").readline())["prompt"]


def num(v):
    try:
        return None if v in (None, "") else round(float(str(v).replace(",", "")), 2)
    except ValueError:
        return None


def parse(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return [l for l in (json.loads(m.group(0)).get("lines") or []) if isinstance(l, dict)] if m else None
    except ValueError:
        return None


# the day's orders: read that day, anywhere in the pipeline (queue, hold, synced)
orders = []
for pat in ("out/PurchaseOrders/*.json", "out/PurchaseOrders/hold/*.json", "synced/PurchaseOrders/*/*.json"):
    for p in sorted(root.glob(pat)):
        try:
            sc = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not str(sc.get("processed_at") or "").startswith(a.date) or sc.get("doc_type") != "po":
            continue
        pdf = p.with_name(str(sc.get("file") or ""))
        if pdf.is_file():
            orders.append((p, pdf))
if a.limit:
    orders = orders[: a.limit]
print(f"{len(orders)} purchase orders read on {a.date}", flush=True)

torch.cuda.reset_peak_memory_stats()
t0 = time.time()
model, tok = FastVisionModel.from_pretrained(str(run / "lora"), load_in_4bit=True, max_seq_length=8192)
FastVisionModel.for_inference(model)
mem_model = torch.cuda.memory_allocated() / 2**30
print(f"model loaded in {time.time() - t0:.0f}s, {mem_model:.2f} GB on the GPU", flush=True)

rows, tot = [], {"orders": 0, "same_lines": 0, "agree_qty": [0, 0], "agree_price": [0, 0],
                 "checked": 0, "main": {"qty": [0, 0], "price": [0, 0]}, "combined": {"qty": [0, 0], "price": [0, 0]}}
td = tempfile.mkdtemp()
for n, (js, pdf) in enumerate(orders, 1):
    s = time.time()
    png = Path(td) / "p.png"
    try:
        subprocess.run(["pdftoppm", "-png", "-scale-to", str(a.px), "-f", "1", "-l", "1", "-singlefile", str(pdf), str(png.with_suffix(""))],
                       check=True, capture_output=True, timeout=120)
        msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": PROMPT}]}]
        text = tok.apply_chat_template(msgs, add_generation_prompt=True)
        inp = tok(Image.open(png).convert("RGB"), text, add_special_tokens=False, truncation=False, return_tensors="pt").to("cuda")
        with torch.no_grad():
            o = model.generate(**inp, max_new_tokens=1024, do_sample=False)
        raw = tok.decode(o[0][inp["input_ids"].shape[1]:], skip_special_tokens=True)
    except Exception as ex:
        raw = f"ERROR {ex}"
    nums = parse(raw)
    secs = round(time.time() - s, 1)
    sc = json.load(open(js, encoding="utf-8"))                   # read again just before writing (dashboard edits)
    f = sc.get("fields") or {}
    main = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    if sc.get("fields_read"):
        main = [i for i in (sc["fields_read"].get("items") or []) if isinstance(i, dict)]   # what the reader read
    sc["numbers_pass"] = {"run": run.name, "at": datetime.now().isoformat(timespec="seconds"), "secs": secs,
                          "lines": nums, "raw": None if nums is not None else raw[:500]}
    tmp = js.with_name(js.name + ".np.tmp")
    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, js)
    tot["orders"] += 1
    same = nums is not None and len(nums) == len(main)
    tot["same_lines"] += same
    if same:
        for x, y in zip(main, nums):
            tot["agree_qty"][1] += 1; tot["agree_price"][1] += 1
            tot["agree_qty"][0] += num(x.get("quantity")) == num(y.get("qty"))
            tot["agree_price"][0] += num(x.get("unit_price")) == num(y.get("unit_price"))
    ver = [i for i in (f.get("items") or []) if isinstance(i, dict)] if f.get("checked") else None
    if ver:
        tot["checked"] += 1
        comb = [dict(x) for x in main]
        if same:
            for c, y in zip(comb, nums):
                c["quantity"], c["unit_price"] = y.get("qty"), y.get("unit_price")
        for k, got in (("main", main), ("combined", comb)):
            for j, v in enumerate(ver):
                g = got[j] if j < len(got) else {}
                tot[k]["qty"][1] += 1; tot[k]["price"][1] += 1
                tot[k]["qty"][0] += num(g.get("quantity")) == num(v.get("quantity"))
                tot[k]["price"][0] += num(g.get("unit_price")) == num(v.get("unit_price"))
    rows.append({"file": sc.get("file"), "order": f.get("order_number"), "checked": bool(f.get("checked")), "secs": secs,
                 "lines_reader": len(main), "lines_numbers": None if nums is None else len(nums),
                 "reader": "; ".join(f"{i.get('quantity')} @ {i.get('unit_price')}" for i in main),
                 "numbers": "" if nums is None else "; ".join(f"{i.get('qty')} @ {i.get('unit_price')}" for i in nums),
                 "checked_values": "" if not ver else "; ".join(f"{i.get('quantity')} @ {i.get('unit_price')}" for i in ver)})
    if n % 25 == 0 or n == len(orders):
        print(f"  {n}/{len(orders)}  {secs}s  peak {torch.cuda.max_memory_allocated() / 2**30:.2f} GB", flush=True)

pc = lambda x: round(x[0] / x[1], 4) if x[1] else None
summary = {"date": a.date, "run": run.name, "orders": tot["orders"], "same_line_count": tot["same_lines"],
           "agree_qty": pc(tot["agree_qty"]), "agree_price": pc(tot["agree_price"]), "checked_orders": tot["checked"],
           "checked": {k: {"qty": pc(tot[k]["qty"]), "price": pc(tot[k]["price"]), "lines": tot[k]["qty"][1]} for k in ("main", "combined")},
           "secs_per_order": round((time.time() - t0) / max(1, tot["orders"]), 1),
           "gpu_gb_model": round(mem_model, 2), "gpu_gb_peak": round(torch.cuda.max_memory_allocated() / 2**30, 2)}
out = root / "reports" / f"numbers shadow {a.date}"
with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
json.dump(summary, open(f"{out}.json", "w"), indent=1)
print(json.dumps(summary, indent=1))

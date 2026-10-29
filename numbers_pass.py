#!/usr/bin/env python3
"""Numbers pass, LIVE as a batch (Dino 27 Oct: "yes please set up the batch pass"). After the day's purchase-order batch
has been read and before its checking file is written (po_daily watch calls numbers_pass_run.sh), the fine-tuned
numbers model reads each order's line quantities and unit prices again. Where it reads the same number of lines as the
main reader, its quantity and unit price replace the main reader's (a value it could not read keeps the main reader's),
and its description too when the run's install.json allows it (335: only once it scored better than the reader);
amounts, order value, lines text and value note are rebuilt the way the reader builds them. Per order:
  * sidecar["numbers_pass"] = {run, at, secs, lines, applied, changed: [{line, qty: [old, new], price: [old, new]}]}
  * sidecar["fields_read"] = the main reader's own reading, kept before anything changes (training compares the main
    reader against the checked values; po_daily import keeps an existing fields_read)
Skips checked orders and orders already passed. Evidence (25 Oct A/B, 60 checked test orders): qty 80.5 -> 90.3 %,
price 78.6 -> 88.3 %. Runs in the training venv; numbers_pass_run.sh pauses the worker and unloads Ollama around it.

    numbers_pass.py --day 2026-10-27 [--run /srv/ml/numbers/active] [--dry-run] [--count] [--limit N] [--all-unchecked]
"""
import argparse, csv, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"), help="the batch fed that day (work/po_daily/<day>/batch.csv)")
ap.add_argument("--run", default="/srv/ml/numbers/active", help="trained run folder (its lora/); 'active' is a symlink")
ap.add_argument("--data", default="/srv/ml/numbers/data", help="for the prompt the model was trained with")
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--dry-run", action="store_true", help="read and report, change nothing")
ap.add_argument("--all-unchecked", action="store_true", help="every unchecked order in the queues, not just the day's batch")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--count", action="store_true", help="only print how many orders would be passed (no GPU)")
ap.add_argument("--px", type=int, default=1600)
ap.add_argument("--all-orders", action="store_true", help="also orders that have a buying-register row (default: skip them - "
                "the checking file takes quantity and price from the register where it has the row; review F6, 28 Oct)")
a = ap.parse_args()

root, run = Path(a.root), Path(a.run).resolve()
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sidecar_lock                                              # noqa: E402  (review F10: one lock per sidecar for every writer)
# 335: the prompt the model was trained with (its run folder), so rebuilding the dataset never changes the live reads;
# install.json (numbers_run.sh) says which fields the run may change - descriptions only once it has beaten the reader
PROMPT = (run / "prompt.txt").read_text(encoding="utf-8") if (run / "prompt.txt").exists() else \
    json.loads(open(Path(a.data) / "test.jsonl", encoding="utf-8").readline())["prompt"]
try:
    APPLY = dict({"qty": True, "price": True, "description": False}, **json.load(open(run / "install.json"))["apply"])
except (OSError, ValueError, KeyError):
    APPLY = {"qty": True, "price": True, "description": False}


def num(v):
    try:
        return None if v in (None, "") else round(float(str(v).replace(",", "").replace(" ", "")), 4)
    except ValueError:
        return None


def parse(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return [l for l in (json.loads(m.group(0)).get("lines") or []) if isinstance(l, dict)] if m else None
    except ValueError:
        return None


def rebuild(f):
    """Amounts and order totals after the quantities / prices change (as po_reader.to_fields and the dashboard)."""
    items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    for i in items:
        q, p = num(i.get("quantity")), num(i.get("unit_price"))
        i["amount"] = round(q * p, 2) if q is not None and p is not None else None
    priced = [i for i in items if i.get("unit_price") is not None]
    amounts = [i["amount"] for i in items if i.get("amount") is not None]
    total = round(sum(amounts), 2) if priced and len(amounts) == len(priced) else None

    def lt(i):
        q = "" if i.get("quantity") is None else f"{i['quantity']:g} x " if isinstance(i["quantity"], (int, float)) else f"{i['quantity']} x "
        pr = "" if i.get("unit_price") is None else f" @ R{i['unit_price']:,.2f}" + (f" per {i['price_unit']}" if i.get("price_unit") not in (None, "each", "null") else "")
        return f"{q}{i.get('description') or ''}{pr}"
    f.update(items=items, line_count=len(items), lines_text="\n".join(lt(i) for i in items) or None, order_value=total,
             total=total, value_note=f"{len(priced)} of {len(items)} lines priced" if items else None)


# the orders: the day's batch (by source SHA-256), read, unchecked, not passed yet
shas = None
if not a.all_unchecked:
    bp = root / "work" / "po_daily" / a.day / "batch.csv"
    if not bp.exists():
        print(f"no batch list for {a.day} ({bp}): nothing to do")
        raise SystemExit(0)
    shas = {r["sha256"].lower() for r in csv.DictReader(open(bp, encoding="utf-8"))}
# buying-register rows (work/po_daily/registers.json, kept fresh by po_daily): an order with a row gets its FINAL
# quantities and prices from the register, so the numbers model can only change the outcome where there is no row
regs = {}
if not a.all_orders:
    try:
        import po_daily                                   # noqa: E402  (same folder; registers() rebuilds the cache if stale)
        regs = po_daily.registers()
    except Exception as e:                                # no registers: pass everything, as before
        print(f"registers not read ({str(e)[:120]}): passing every order", flush=True)
skipped_reg = 0
orders = []
for pat in ("out/PurchaseOrders/*.json", "out/PurchaseOrders/hold/*.json", "out/PolymerOrders/*.json", "out/PolymerOrders/hold/*.json"):
    for p in sorted(root.glob(pat)):
        try:
            sc = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        f = sc.get("fields") or {}
        if sc.get("doc_type") != "po" or f.get("checked") or (sc.get("numbers_pass") or {}).get("applied") is not None:
            continue
        if shas is not None and str(sc.get("source_sha256") or "").lower() not in shas:
            continue
        if not [i for i in (f.get("items") or []) if isinstance(i, dict)]:
            continue                                   # no lines read: nothing to improve
        if regs and po_daily.order_key(f, sc.get("source_file") or sc.get("file")) in regs:
            skipped_reg += 1                           # the register has this order: its numbers come from there
            continue
        pdf = p.with_name(str(sc.get("file") or ""))
        if pdf.is_file():
            orders.append((p, pdf))
if a.limit:
    orders = orders[: a.limit]
print(f"{len(orders)} order(s) with lines to pass ({'all unchecked' if a.all_unchecked else 'batch ' + a.day}), model {run.name}"
      + (f"; {skipped_reg} left to the buying register (it has their rows)" if regs else ""), flush=True)
if not orders or a.count:
    raise SystemExit(0)

from unsloth import FastVisionModel                              # noqa: E402
from PIL import Image                                            # noqa: E402
import torch                                                     # noqa: E402

torch.cuda.reset_peak_memory_stats()
t0 = time.time()
model, tok = FastVisionModel.from_pretrained(str(run / "lora"), load_in_4bit=True, max_seq_length=8192)
FastVisionModel.for_inference(model)
print(f"model loaded in {time.time() - t0:.0f}s, {torch.cuda.memory_allocated() / 2**30:.2f} GB on the GPU", flush=True)

tot = {"orders": 0, "applied": 0, "line_count_differs": 0, "unreadable": 0, "lines_changed": 0, "qty_changed": 0, "price_changed": 0,
       "description_changed": 0}
rows = []
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
            o = model.generate(**inp, max_new_tokens=2048, do_sample=False)
        raw = tok.decode(o[0][inp["input_ids"].shape[1]:], skip_special_tokens=True)
    except Exception as ex:
        raw = f"ERROR {ex}"
    nums = parse(raw)
    secs = round(time.time() - s, 1)
    if not js.exists():                                          # renamed by a dashboard edit while the model ran
        rows.append({"file": js.name, "order": "", "secs": secs, "applied": False, "why": "sidecar renamed/moved during the read; skipped", "lines": 0, "changed": ""})
        continue
    try:
        lk = sidecar_lock.locked(js, "numbers_pass")
        lk.__enter__()
    except sidecar_lock.Locked as ex:
        rows.append({"file": js.name, "order": "", "secs": secs, "applied": False, "why": f"skipped: {ex}", "lines": 0, "changed": ""})
        continue
    try:
        sc = json.load(open(js, encoding="utf-8"))                   # read again just before writing (dashboard edits)
        f = sc.setdefault("fields", {})
        if f.get("checked"):
            continue
        items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
        tot["orders"] += 1
        rec = {"run": run.name, "at": datetime.now().isoformat(timespec="seconds"), "secs": secs, "lines": nums,
               "raw": None if nums is not None else raw[:500], "applied": False, "changed": []}
        if nums is None:
            tot["unreadable"] += 1
            rec["why"] = "numbers model answer not readable"
        elif len(nums) != len(items):
            tot["line_count_differs"] += 1
            rec["why"] = f"line count differs: main reader {len(items)}, numbers model {len(nums)}"
        else:
            rec["applied"] = True
            tot["applied"] += 1
            if "fields_read" not in sc:
                sc["fields_read"] = json.loads(json.dumps(f))       # the main reader's own reading, for training
            for j, (i, y) in enumerate(zip(items, nums), 1):
                ch = {"line": j}
                q, p = num(y.get("qty")), num(y.get("unit_price"))
                if APPLY["qty"] and q is not None and q != num(i.get("quantity")):
                    ch["qty"] = [i.get("quantity"), q]
                    i["quantity"] = int(q) if float(q).is_integer() else q
                    tot["qty_changed"] += 1
                if APPLY["price"] and p is not None and p != num(i.get("unit_price")):
                    ch["price"] = [i.get("unit_price"), p]
                    i["unit_price"] = round(p, 2)
                    tot["price_changed"] += 1
                d = " ".join(str(y.get("item") or "").split())
                if APPLY["description"] and d and d.upper() != " ".join(str(i.get("description") or "").split()).upper():
                    ch["description"] = [i.get("description"), d]
                    i["description"] = d
                    tot["description_changed"] += 1
                if len(ch) > 1:
                    rec["changed"].append(ch)
            tot["lines_changed"] += len(rec["changed"])
            if rec["changed"]:
                rebuild(f)
        sc["numbers_pass"] = rec
        if not a.dry_run:
            tmp = js.with_name(js.name + ".np.tmp")
            tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, js)
    finally:
        lk.__exit__(None, None, None)
    rows.append({"file": sc.get("file"), "order": f.get("order_number"), "secs": secs, "applied": rec["applied"],
                 "why": rec.get("why", ""), "lines": len(items), "changed": "; ".join(
                     f"L{c['line']}" + (f" qty {c['qty'][0]}->{c['qty'][1]}" if "qty" in c else "") +
                     (f" price {c['price'][0]}->{c['price'][1]}" if "price" in c else "") +
                     (f" desc {c['description'][0]!r}->{c['description'][1]!r}" if "description" in c else "") for c in rec["changed"])})
    if n % 50 == 0 or n == len(orders):
        print(f"  {n}/{len(orders)}  {secs}s  applied {tot['applied']}, lines changed {tot['lines_changed']}", flush=True)

summary = {"day": a.day, "run": run.name, "dry_run": a.dry_run, **tot,
           "secs_per_order": round((time.time() - t0) / max(1, tot["orders"]), 1),
           "gpu_gb_peak": round(torch.cuda.max_memory_allocated() / 2**30, 2), "finished": datetime.now().isoformat(timespec="seconds")}
out = root / "reports" / f"numbers pass {a.day}{' dry run' if a.dry_run else ''}"
with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
json.dump(summary, open(f"{out}.json", "w"), indent=1)
print(json.dumps(summary, indent=1))

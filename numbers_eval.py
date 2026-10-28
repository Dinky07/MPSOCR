#!/usr/bin/env python3
"""Numbers pass, step 3: score the fine-tuned numbers model on the test orders against the checked (spreadsheet) values,
next to the current reader's reading of the same orders (test.jsonl "main"). Three ways are scored per line:
  main      the current reader alone
  numbers   the numbers model alone
  combined  the current reader's lines, with quantity and price taken from the numbers model when both read the same
            number of lines (what the pipeline would do)
Runs in the training venv. Writes <run>/eval.json and prints the table.

    numbers_eval.py --data /srv/ml/numbers/data --run /srv/ml/numbers/run-<stamp>
"""
import argparse, json, re, time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True)
ap.add_argument("--run", required=True)
ap.add_argument("--base", default="unsloth/Qwen2.5-VL-3B-Instruct-bnb-4bit")
ap.add_argument("--max-new", type=int, default=1024)
a = ap.parse_args()

from unsloth import FastVisionModel                              # noqa: E402
from PIL import Image                                            # noqa: E402
import torch                                                     # noqa: E402

data, run = Path(a.data), Path(a.run)
tests = [json.loads(l) for l in open(data / "test.jsonl", encoding="utf-8") if l.strip()]
model, tokenizer = FastVisionModel.from_pretrained(str(run / "lora"), load_in_4bit=True, max_seq_length=8192)
FastVisionModel.for_inference(model)


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


def read(img):
    msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": tests[0]["prompt"]}]}]
    text = tokenizer.apply_chat_template(msgs, add_generation_prompt=True)
    inp = tokenizer(Image.open(img).convert("RGB"), text, add_special_tokens=False, truncation=False, return_tensors="pt").to("cuda")
    with torch.no_grad():
        o = model.generate(**inp, max_new_tokens=a.max_new, do_sample=False)
    return tokenizer.decode(o[0][inp["input_ids"].shape[1]:], skip_special_tokens=True)


tot = {k: {"qty": [0, 0], "price": [0, 0]} for k in ("main", "numbers", "combined")}
docs, t0 = [], time.time()
for i, r in enumerate(tests, 1):
    s = time.time()
    raw = read(data / r["image"])
    nums = parse(raw)
    ver, main = r["verified"], r.get("main") or []
    comb = [dict(m) for m in main]
    used = bool(nums) and len(nums) == len(main)
    if used:
        for c, n in zip(comb, nums):
            c["qty"], c["unit_price"] = n.get("qty"), n.get("unit_price")
    for k, got in (("main", main), ("numbers", nums or []), ("combined", comb)):
        for j, v in enumerate(ver):
            g = got[j] if j < len(got) else {}
            tot[k]["qty"][1] += 1; tot[k]["price"][1] += 1
            tot[k]["qty"][0] += num(g.get("qty")) == num(v.get("qty"))
            tot[k]["price"][0] += num(g.get("unit_price")) == num(v.get("unit_price"))
    docs.append({"file": r["file"], "secs": round(time.time() - s, 1), "raw": raw, "numbers": nums, "main": main,
                 "verified": ver, "numbers_used": used})
    print(f"  {i}/{len(tests)} {r['file'][:50]:50} {time.time() - s:5.1f}s lines {len(ver)} numbers {len(nums or [])} main {len(main)}", flush=True)
res = {k: {f: {"right": v[f][0], "counted": v[f][1], "acc": round(v[f][0] / v[f][1], 4) if v[f][1] else None} for f in v}
       for k, v in tot.items()}
res["secs_per_order"] = round((time.time() - t0) / max(1, len(tests)), 1)
res["numbers_used_on"] = sum(d["numbers_used"] for d in docs)
json.dump({"results": res, "docs": docs}, open(run / "eval.json", "w"), ensure_ascii=False, indent=1)
print(f"\n{len(tests)} test orders, {tot['main']['qty'][1]} lines; numbers model used on {res['numbers_used_on']} orders "
      f"(same line count as the current reader); {res['secs_per_order']} s per order")
print(f"{'':10}{'line qty':>14}{'line price':>14}")
for k in ("main", "numbers", "combined"):
    print(f"{k:10}{res[k]['qty']['acc'] * 100:13.1f}%{res[k]['price']['acc'] * 100:13.1f}%")

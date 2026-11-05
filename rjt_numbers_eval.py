#!/usr/bin/env python3
"""Ronin JT numbers model, step 3: score a fine-tuned JT model on the test JTs against the RoninStock capture, beside the
current reader's lines ("main", from the sheet). Per line: qty exact; stock code exact, and "code near" (equal after the
look-alike letters O/0, S/5, I/1, B/8, Z/2, G/6 are folded); description 90 % alike. Codes are also checked against the
stock list (work/registers/ronin-stock-codes.psv): how many produced codes exist in RoninStock. Runs in the training venv.
    rjt_numbers_eval.py --data /srv/ml/rjt/data --run /srv/ml/rjt/run-<stamp> [--out-name eval.json] [--limit N]
"""
import argparse, difflib, json, re, time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True)
ap.add_argument("--run", required=True)
ap.add_argument("--out-name", default="eval.json")
ap.add_argument("--codes", default="/srv/ocr/scans/pipeline/work/registers/ronin-stock-codes.psv")
ap.add_argument("--max-new", type=int, default=1536)
ap.add_argument("--limit", type=int, default=0)
a = ap.parse_args()

from unsloth import FastVisionModel                              # noqa: E402
from PIL import Image                                            # noqa: E402
import torch                                                     # noqa: E402

data, run = Path(a.data), Path(a.run).resolve()
tests = [json.loads(l) for l in open(data / "test.jsonl", encoding="utf-8") if l.strip()]
if a.limit:
    tests = tests[: a.limit]
prompt = (run / "prompt.txt").read_text(encoding="utf-8") if (run / "prompt.txt").exists() else tests[0]["prompt"]
model, tokenizer = FastVisionModel.from_pretrained(str(run / "lora"), load_in_4bit=True, max_seq_length=8192)
FastVisionModel.for_inference(model)
stock = set()
try:
    for l in open(a.codes, encoding="utf-8"):
        c = l.split("|")[0].strip().upper()
        if c and c != "CODE":
            stock.add(c)
except OSError:
    pass
LOOK = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6", "Q": "0", "D": "0"})


def norm(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def num(v):
    try:
        return None if v in (None, "") else round(float(re.sub(r"[^\d.]", "", str(v)) or "x"), 2)
    except ValueError:
        return None


def parse(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return [l for l in (json.loads(m.group(0)).get("lines") or []) if isinstance(l, dict)] if m else None
    except ValueError:
        return None


def read(img):
    msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]
    text = tokenizer.apply_chat_template(msgs, add_generation_prompt=True)
    inp = tokenizer(Image.open(img).convert("RGB"), text, add_special_tokens=False, truncation=False, return_tensors="pt").to("cuda")
    with torch.no_grad():
        o = model.generate(**inp, max_new_tokens=a.max_new, do_sample=False)
    return tokenizer.decode(o[0][inp["input_ids"].shape[1]:], skip_special_tokens=True)


KINDS = ("main", "model")
F = ("qty", "code", "code_near", "description", "code_in_stock", "lines_count")
tot = {k: {f: [0, 0] for f in F} for k in KINDS}
sub = {k: {"written": [0, 0], "built": [0, 0]} for k in KINDS}       # code exact, split by whether a code was on the page
docs, t0 = [], time.time()
for i, r in enumerate(tests, 1):
    s = time.time()
    raw = read(data / r["image"])
    got = parse(raw) or []
    ver, main = r["verified"], r.get("main") or []
    cw = r.get("code_written") or [True] * len(ver)
    for k, lines in (("main", main), ("model", got)):
        tot[k]["lines_count"][1] += 1
        tot[k]["lines_count"][0] += len(lines) == len(ver)
        for j, v in enumerate(ver):
            g = lines[j] if j < len(lines) else {}
            gc, vc = norm(g.get("item_no")), norm(v.get("item_no"))
            for f in ("qty", "code", "code_near", "description"):
                tot[k][f][1] += 1
            tot[k]["qty"][0] += num(g.get("qty")) == num(v.get("qty"))
            tot[k]["code"][0] += bool(vc) and gc == vc
            tot[k]["code_near"][0] += bool(vc) and gc.translate(LOOK) == vc.translate(LOOK)
            tot[k]["description"][0] += bool(norm(g.get("item") or g.get("description"))) and difflib.SequenceMatcher(None, norm(g.get("item") or g.get("description")), norm(v.get("description"))).ratio() >= 0.9
            w = "written" if (j < len(cw) and cw[j]) else "built"
            sub[k][w][1] += 1
            sub[k][w][0] += bool(vc) and gc == vc
            if gc:
                tot[k]["code_in_stock"][1] += 1
                tot[k]["code_in_stock"][0] += gc in stock
    docs.append({"file": r["file"], "jt": r.get("jt"), "secs": round(time.time() - s, 1), "raw": raw, "model": got, "main": main, "verified": ver})
    print(f"  {i}/{len(tests)} {r.get('jt') or r['file'][:30]:12} {time.time() - s:5.1f}s lines {len(ver)} model {len(got)} main {len(main)}", flush=True)
res = {k: {f: {"right": v[f][0], "counted": v[f][1], "acc": round(v[f][0] / v[f][1], 4) if v[f][1] else None} for f in v} for k, v in tot.items()}
for k in KINDS:
    res[k]["code_written"] = {"right": sub[k]["written"][0], "counted": sub[k]["written"][1], "acc": round(sub[k]["written"][0] / sub[k]["written"][1], 4) if sub[k]["written"][1] else None}
    res[k]["code_built"] = {"right": sub[k]["built"][0], "counted": sub[k]["built"][1], "acc": round(sub[k]["built"][0] / sub[k]["built"][1], 4) if sub[k]["built"][1] else None}
res["secs_per_jt"] = round((time.time() - t0) / max(1, len(tests)), 1)
res["tests"] = len(tests)
res["stock_codes"] = len(stock)
json.dump({"results": res, "docs": docs}, open(run / a.out_name, "w"), ensure_ascii=False, indent=1)
pc = lambda v: f"{v * 100:6.1f}%" if v is not None else "     -"
print(f"\n{len(tests)} test JTs, {tot['main']['qty'][1]} lines; {res['secs_per_jt']} s per JT; stock list {len(stock)} codes")
print(f"{'':8}{'qty':>8}{'code':>8}{'near':>8}{'written':>9}{'built':>8}{'descr':>8}{'in stock':>10}{'lines':>8}")
for k in KINDS:
    print(f"{k:8}" + "".join(pc(res[k][f]["acc"]) for f in ("qty", "code", "code_near")) + f" {pc(res[k]['code_written']['acc'])}" + pc(res[k]["code_built"]["acc"])
          + pc(res[k]["description"]["acc"]) + "  " + pc(res[k]["code_in_stock"]["acc"]) + pc(res[k]["lines_count"]["acc"]))

#!/usr/bin/env python3
"""Ronin JT shadow pass (Dino 10 Nov 2026 "Yes, build it"). The trained Ronin JT model reads JTs it never trained on; each
stock code gets a green / amber / red status (rjt_codes.py) and is scored against the RoninStock capture. Nobody sees these
codes and no document changes: everything is written to work/rjt_shadow only, sidecars are only read.
  reads/<DF>.json    the model's reading, once per JT
  report.json, lines.csv, history.jsonl    rebuilt from all reads on every run, so a new capture or rule rescoring is free
    rjt_shadow.py [--count] [--score-only] [--limit N] [--max-seconds S]
Runs in the training venv; rjt_shadow_run.sh takes the GPU for it."""
import argparse, csv, glob, json, os, re, subprocess, sys, tempfile, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rjt_codes as K                                            # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--run", default="/srv/ml/rjt/run-20261110-0556")
ap.add_argument("--data", default="/srv/ml/rjt/data", help="its train and test lists: those JTs are not shadow-read")
ap.add_argument("--out", default=None, help="default <root>/work/rjt_shadow")
ap.add_argument("--export", default=None)
ap.add_argument("--codes", default=None)
ap.add_argument("--px", type=int, default=1600)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--max-seconds", type=int, default=0)
ap.add_argument("--max-new", type=int, default=1024)
ap.add_argument("--count", action="store_true", help="only say how many JTs would be read (no GPU)")
ap.add_argument("--score-only", action="store_true")
a = ap.parse_args()

root, run = Path(a.root), Path(a.run).resolve()
out = Path(a.out) if a.out else root / "work" / "rjt_shadow"
reads = out / "reads"
EXPORT = a.export or str(root / "work" / "registers" / "ronin-jt-export.psv")
CODES = a.codes or str(root / "work" / "registers" / "ronin-stock-codes.psv")
PASS_LINES, PASS_RIGHT = 200, 0.95


def write_json(path, obj):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def captures():
    cap = defaultdict(list)
    with open(EXPORT, encoding="utf-8", errors="replace") as fh:
        for r in csv.reader(fh, delimiter="|"):
            if len(r) >= 14 and r[0] != "AutoIndex" and r[2].upper().startswith("DF"):
                cap[r[2].upper().replace(" ", "")].append({"line": int(r[10] or 0), "code": K.norm(r[11]), "qty": K.num(r[12]), "desc": r[13]})
    for v in cap.values():
        v.sort(key=lambda x: x["line"])
    return cap


def parse(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return [x for x in (json.loads(m.group(0)).get("lines") or []) if isinstance(x, dict)] if m else None
    except ValueError:
        return None


def to_read(cap):
    used = set()
    for name in ("train.jsonl", "test.jsonl"):
        with open(Path(a.data) / name, encoding="utf-8") as fh:
            used |= {str(json.loads(x).get("jt") or "").upper() for x in fh if x.strip()}
    todo, seen, cnt = [], set(), Counter()
    for pat in ("out/RoninJobTransfers/*.json", "out/RoninJobTransfers/hold/*.json", "synced/RoninJobTransfers/*/*.json"):
        for js in sorted(glob.glob(str(root / pat))):
            if js.endswith((".deleted.json", ".rerouted.json")):
                continue
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            f = sc.get("fields") or {}
            jt = str(f.get("jt_number") or "").upper().replace(" ", "")
            if not re.fullmatch(r"DF\d{5}", jt) or jt in seen:
                continue
            seen.add(jt)
            if jt in used:
                cnt["used in the model's training or test"] += 1
            elif (reads / f"{jt}.json").exists():
                cnt["already read"] += 1
            else:
                pdf = Path(js).with_name(str(sc.get("file") or ""))
                if not pdf.is_file():
                    cnt["no PDF beside the reading"] += 1
                    continue
                rd = sc.get("fields_read") or f
                todo.append((jt, pdf, sc.get("source_sha256"), [i for i in (rd.get("items") or []) if isinstance(i, dict)]))
    todo.sort(key=lambda t: (t[0] not in cap, -int(t[0][2:])))           # captured first: those can be scored at once
    cnt["to read"] = len(todo)
    cnt["to read, already captured"] = sum(1 for t in todo if t[0] in cap)
    return todo, cnt


def read_all(todo):
    from unsloth import FastVisionModel
    from PIL import Image
    import torch
    t0 = time.time()
    prompt = (run / "prompt.txt").read_text(encoding="utf-8")
    model, tok = FastVisionModel.from_pretrained(str(run / "lora"), load_in_4bit=True, max_seq_length=8192)
    FastVisionModel.for_inference(model)
    print(f"model loaded in {time.time() - t0:.0f}s, {torch.cuda.memory_allocated() / 2**30:.2f} GB on the GPU", flush=True)
    n = 0
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "p.png"
        for jt, pdf, sha, reader in todo:
            if a.max_seconds and time.time() - t0 > a.max_seconds:
                print(f"time limit reached after {n} JTs", flush=True)
                break
            s = time.time()
            try:
                subprocess.run(["pdftoppm", "-png", "-scale-to", str(a.px), "-f", "1", "-l", "1", "-singlefile", str(pdf), str(png.with_suffix(""))],
                               check=True, capture_output=True, timeout=120)
                msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]
                text = tok.apply_chat_template(msgs, add_generation_prompt=True)
                inp = tok(Image.open(png).convert("RGB"), text, add_special_tokens=False, truncation=False, return_tensors="pt").to("cuda")
                with torch.no_grad():
                    o = model.generate(**inp, max_new_tokens=a.max_new, do_sample=False)
                raw = tok.decode(o[0][inp["input_ids"].shape[1]:], skip_special_tokens=True)
            except Exception as ex:
                raw = f"ERROR {ex}"
            lines = parse(raw)
            write_json(reads / f"{jt}.json", {"jt": jt, "file": pdf.name, "source_sha256": sha, "run": run.name, "at": datetime.now().isoformat(timespec="seconds"),
                                              "secs": round(time.time() - s, 1), "model": lines or [], "unreadable": raw[:400] if lines is None else None,
                                              "reader": [{"quantity": i.get("quantity"), "item_no": i.get("item_no"), "description": i.get("description")} for i in reader]})
            n += 1
            if n % 25 == 0:
                print(f"  {n}/{len(todo)} read, {time.time() - t0:.0f}s", flush=True)
    print(f"{n} JTs read", flush=True)


def score(cap):
    stock = K.stock_list(CODES)
    by = {s: Counter() for s in ("green", "amber", "red")}
    t, rows = Counter(), []
    for p in sorted(reads.glob("DF*.json")):
        d = json.load(open(p, encoding="utf-8"))
        jt, m, rd = d["jt"], d["model"], d["reader"]
        t["JTs read"] += 1
        st = K.judge_jt(m, rd, stock)
        gi = cap.get(jt)
        if not gi:
            t["JTs waiting for a capture"] += 1
            continue
        t["JTs scored"] += 1
        t["captured lines"] += len(gi)
        t["no answer from the model"] += d.get("unreadable") is not None or not m
        t["JTs with the right number of lines"] += len(m) == len(gi)
        pool, rpool = list(gi), [K.norm(i.get("item_no")) for i in rd]
        for c in gi:
            if c["code"] in rpool:
                rpool.remove(c["code"])
                t["reader codes right"] += 1
        same = len(m) == len(rd)
        allg, allr = bool(m), len(m) == len(gi)
        for j, (x, (s, why)) in enumerate(zip(m, st), 1):
            code = K.norm(x.get("item_no"))
            hit = next((c for c in pool if c["code"] == code), None) if code else None
            if hit:
                pool.remove(hit)
            q = bool(hit) and K.num(x.get("qty")) == hit["qty"]
            t["model lines"] += 1
            t["codes right"] += bool(hit)
            t["qty right on those"] += q
            by[s]["lines"] += 1
            by[s]["right"] += bool(hit)
            by[s]["right with qty"] += q
            allg &= s == "green"
            allr &= q
            r = rd[j - 1] if same else {}
            rows.append({"JT": jt, "Line": j, "Status": s, "Why": "; ".join(why), "Code right": "Y" if hit else "", "Model code": code, "Captured codes": " ".join(c["code"] for c in gi),
                         "Model qty": x.get("qty"), "Reader qty": r.get("quantity"), "Model read": x.get("item"), "Reader read": r.get("description"), "Stock description": stock.get(code, "")})
        t["JTs all green"] += allg
        t["JTs all green and fully right"] += allg and allr
    pct = lambda r, n: round(100 * r / n, 1) if n else None
    g = by["green"]
    rep = {"at": datetime.now().isoformat(timespec="seconds"), "run": run.name, **t, "codes right %": pct(t["codes right"], t["model lines"]),
           "by status": {s: {**c, "right %": pct(c["right"], c["lines"])} for s, c in by.items()},
           "pass mark": f"{PASS_LINES}+ green lines, {PASS_RIGHT:.0%} right", "passes": g["lines"] >= PASS_LINES and g["right"] / max(1, g["lines"]) >= PASS_RIGHT}
    write_json(out / "report.json", rep)
    with open(out / "lines.csv.tmp", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["JT"])
        w.writeheader()
        w.writerows(rows)
    os.replace(out / "lines.csv.tmp", out / "lines.csv")
    with open(out / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rep, ensure_ascii=False, default=str) + "\n")
    print(json.dumps(rep, indent=1, ensure_ascii=False, default=str), flush=True)


reads.mkdir(parents=True, exist_ok=True)
cap = captures()
if not a.score_only:
    todo, cnt = to_read(cap)
    print(dict(cnt), flush=True)
    if a.count:
        raise SystemExit(0)
    if todo:
        read_all(todo[: a.limit or None])
score(cap)

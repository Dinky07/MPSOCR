#!/usr/bin/env python3
"""A/B for job transfers: the pipeline's current reader (generic prompt, the model in config.json) against jt_reader
on qwen2.5vl / qwen3-vl, with and without worked examples, scored on the corrected review set
(<root>/training/test.jsonl: image + target answer per form).

  python3 eval_jt.py --make-fewshot [--n 4]       # jt_fewshot/ from train.jsonl (one form per writer, 2-4 lines)
  python3 eval_jt.py [--limit N] [--out eval_jt.json]
"""
import argparse, difflib, json, os, re, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jt_reader, worker

HERE = os.path.dirname(os.path.abspath(__file__))
# Scored fields = the ones corrected in Label Studio. customer, transferred_by and receiving_stamp are carried from the
# reader unverified, so they are not scored.
FIELDS = ["jt_number", "date", "mps_job_no", "transferred_from", "transferred_to", "received_by", "remarks"]
_DATE = re.compile(r"^\s*(\d{1,4})[./\-\s](\d{1,2})[./\-\s](\d{1,4})\s*$")


def norm(v):
    s = str(v or "").strip()
    if s.lower() in ("[signature]", "[illegible]", "null", "none"):      # the worker blanks these too
        return ""
    m = _DATE.match(s)
    if m:                                                                   # 9.09.2026 == 09-09-2026 == 2026/09/09
        a, b, c = m.groups()
        if len(a) == 4:
            y, mo, d = a, b, c
        else:
            d, mo, y = a, b, c
        if len(y) == 2:
            y = "20" + y
        return f"{int(y):04d}{int(mo):02d}{int(d):02d}"
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def score(ans, target):
    s = {}
    for k in FIELDS:
        a, b = ans.get(k), target.get(k)
        if norm(a) == "" and norm(b) == "":
            continue
        s[k] = int(norm(a) == norm(b))
    ri, ti = ans.get("line_items") or [], target.get("line_items") or []
    s["line_count"] = int(len(ri) == len(ti))
    n = max(len(ti), 1)
    s["line_qty"] = sum(1 for x, y in zip(ri, ti) if norm(x.get("quantity")) == norm(y.get("quantity"))) / n
    s["line_desc"] = sum(1 for x, y in zip(ri, ti)
                         if difflib.SequenceMatcher(None, norm(x.get("description")), norm(y.get("description"))).ratio() >= 0.9) / n
    s["whole_form"] = int(all(v == 1 for v in s.values()))
    return s


def from_worker(w):
    """The worker's JT schema back into the answer schema, for scoring the current path."""
    return {"jt_number": w.get("jt_number"), "date": w.get("date"), "mps_job_no": w.get("job_number"),
            "transferred_from": w.get("from"), "transferred_to": w.get("to"), "transferred_by": w.get("written_by"),
            "received_by": w.get("received_by"), "remarks": w.get("remarks"),
            "line_items": [{"quantity": (f"{i.get('quantity') or ''} {i.get('unit') or ''}").strip() or None,
                            "item_no": i.get("item_no"), "description": i.get("description")} for i in w.get("items") or []]}


def make_fewshot(root, n):
    rows = [json.loads(l) for l in open(os.path.join(root, "training", "train.jsonl"), encoding="utf-8")]
    rows = [r for r in rows if r.get("status") in ("Corrected", "Correct as filled in") and 2 <= len(r["target"].get("line_items") or []) <= 4
            and os.path.exists(r["image"])]
    pick, writers = [], set()
    for want in ("DERRICK", "DAVID", "SANET", "JOANNE", "WHITY", "DICAN"):
        for r in rows:
            if r["writer"] == want and want not in writers:
                pick.append(r); writers.add(want); break
        if len(pick) >= n:
            break
    d = os.path.join(HERE, "jt_fewshot")
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    for i, r in enumerate(pick, 1):
        stem = os.path.join(d, f"{i}_{r['writer'].lower()}")
        shutil.copy2(r["image"], stem + ".png")
        json.dump(r["target"], open(stem + ".json", "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    json.dump([r["id"] for r in pick], open(os.path.join(d, "sources.json"), "w"))
    print(f"{len(pick)} worked examples in {d}: " + ", ".join(f"{r['id']} ({r['writer']})" for r in pick))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
    ap.add_argument("--make-fewshot", action="store_true"); ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--out", default="eval_jt.json")
    ap.add_argument("--variants", default="current,q25-0,q25-few,q3-0,q3-few")
    a = ap.parse_args()
    if a.make_fewshot:
        make_fewshot(a.root, a.n); return
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    tcfg = cfg["doc_types"]["JobTransfers"]
    tests = [json.loads(l) for l in open(os.path.join(a.root, "training", "test.jsonl"), encoding="utf-8")]
    tests = [t for t in tests if os.path.exists(t["image"])][: a.limit or None]
    few = os.path.join(HERE, "jt_fewshot")
    variants = {
        "current": lambda p: from_worker(worker.extract_builtin("jt", worker.Path(p), cfg, tcfg)[0]),
        "q25-0": lambda p: jt_reader.read(p, cfg["ollama_url"], "qwen2.5vl:7b", None, 1600)[0],
        "q25-few": lambda p: jt_reader.read(p, cfg["ollama_url"], "qwen2.5vl:7b", few, 1600)[0],
        "q3-0": lambda p: jt_reader.read(p, cfg["ollama_url"], "qwen3-vl:8b-instruct", None, 1600)[0],
        "q3-few": lambda p: jt_reader.read(p, cfg["ollama_url"], "qwen3-vl:8b-instruct", few, 1600)[0],
    }
    res = {v: {"docs": [], "secs": 0.0} for v in a.variants.split(",")}
    for v in res:
        for t in tests:
            t0 = time.time()
            try:
                ans = variants[v](t["image"])
                err = None
            except Exception as e:
                ans, err = {}, str(e)[:200]
            res[v]["secs"] += time.time() - t0
            res[v]["docs"].append({"id": t["id"], "writer": t["writer"], "score": score(ans, t["target"]), "error": err, "answer": ans})
            print(f"{v:8} {t['id']:18} {time.time() - t0:5.1f}s  whole={res[v]['docs'][-1]['score']['whole_form']}" + (f"  ERROR {err}" if err else ""), flush=True)
    summary = {}
    for v, r in res.items():
        keys = sorted({k for d in r["docs"] for k in d["score"]})
        summary[v] = {k: round(sum(d["score"].get(k, 0) for d in r["docs"]) / max(1, sum(1 for d in r["docs"] if k in d["score"])), 3) for k in keys}
        summary[v]["secs_per_doc"] = round(r["secs"] / max(1, len(r["docs"])), 1)
        summary[v]["errors"] = sum(1 for d in r["docs"] if d["error"])
    json.dump({"summary": summary, "results": res, "tests": len(tests)}, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    cols = ["whole_form", "jt_number", "date", "mps_job_no", "transferred_from", "transferred_to", "received_by", "remarks",
            "line_count", "line_qty", "line_desc", "secs_per_doc", "errors"]
    print(f"\n{len(tests)} test forms\n{'variant':9}" + "".join(f"{c[:10]:>11}" for c in cols))
    for v, s in summary.items():
        print(f"{v:9}" + "".join(f"{s.get(c, ''):>11}" for c in cols))


if __name__ == "__main__":
    main()

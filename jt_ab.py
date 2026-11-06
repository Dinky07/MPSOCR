#!/usr/bin/env python3
"""531 test: the patched JT reader (/tmp/jt531/jt_reader.py: upright pages, a second ask without worked examples, salvage)
against the live one, with the worker's settings (jt_settings: model, worked examples, px):
  1. the 25 corrected test forms (training/test.jsonl, eval_jt scoring) - the controls;
  2. every page of the JT scans the live reader failed on (failed/JobTransfers, failed/RoninJobTransfers): answers
     that are usable, salvaged, still failing, and pages turned upright.
PASS: the patched reader is not lower than the live one on any control field or on whole forms, and reads at least half
of the failed pages. Report: reports/jt reader fix test 2026-11-06.json (+ the printed table)."""
import glob, importlib.util, json, os, subprocess, sys, tempfile
from collections import Counter
from pathlib import Path

P = "/srv/ocr/pipeline"
sys.path.insert(0, P)
import eval_jt as E                                     # noqa: E402  score()
import jt_reader as OLD                                 # noqa: E402

spec = importlib.util.spec_from_file_location("jt_reader_new", "/tmp/jt531/jt_reader.py")
NEW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NEW)
ROOT = Path("/srv/ocr/scans/pipeline")
cfg = json.load(open(f"{P}/config.json"))
t = cfg["doc_types"]["JobTransfers"]
jc = dict(t.get("jt_reader") or {})
jc.setdefault("model", "qwen2.5vl:7b")
jc.setdefault("fewshot_dir", f"{P}/jt_fewshot")
jc.setdefault("px", 1600)
OLL = cfg["ollama_url"]
print("settings:", jc, flush=True)

tests = [json.loads(l) for l in open("/srv/ocr/scans/training/test.jsonl", encoding="utf-8")]     # 542: the JT review set
tests = [x for x in tests if os.path.exists(x["image"])]
side = {}
for name, M in (("live", OLD), ("patched", NEW)):
    docs = []
    for x in tests:
        try:
            ans, err = M.read(x["image"], OLL, jc["model"], jc["fewshot_dir"], int(jc["px"]))[0], None
        except Exception as e:
            ans, err = {}, str(e)[:160]
        docs.append({"id": x["id"], "score": E.score(ans, x["target"]), "error": err, "turned": ans.get("_turned_upright")})
    keys = sorted({k for d in docs for k in d["score"]})
    side[name] = {k: round(sum(d["score"].get(k, 0) for d in docs) / max(1, sum(1 for d in docs if k in d["score"])), 3) for k in keys}
    side[name]["errors"] = sum(1 for d in docs if d["error"])
    side[name]["turned"] = sum(1 for d in docs if d["turned"])
    print(name, side[name], flush=True)

pages = Counter()
examples = []
for t_ in ("JobTransfers", "RoninJobTransfers"):
    for pdf in sorted(glob.glob(str(ROOT / "failed" / t_ / "*.pdf"))):
        jcp = dict(jc)
        if t_ == "RoninJobTransfers":
            jcp.update((cfg["doc_types"][t_].get("jt_reader") or {}))
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["pdfseparate", pdf, os.path.join(tmp, "p%03d.pdf")], check=True, capture_output=True)
            for pg in sorted(glob.glob(os.path.join(tmp, "p*.pdf"))):
                try:
                    ans = NEW.read(pg, OLL, jcp["model"], jcp["fewshot_dir"], int(jcp["px"]), prompt=jcp.get("prompt"))[0]
                    pages["read" + (" (salvaged)" if ans.get("_salvaged") else "")] += 1
                    pages["turned upright"] += bool(ans.get("_turned_upright"))
                    if len(examples) < 12:
                        examples.append(f"{Path(pdf).name} {Path(pg).stem}: JT {ans.get('jt_number')}, {len(ans.get('line_items') or [])} lines"
                                        + (", turned" if ans.get("_turned_upright") else "") + (", salvaged" if ans.get("_salvaged") else ""))
                except Exception as e:
                    pages["still failing"] += 1
                    if len(examples) < 12:
                        examples.append(f"{Path(pdf).name} {Path(pg).stem}: FAILED {str(e)[:100]}")
print("failed scans, page by page:", dict(pages), flush=True)
for x in examples:
    print("  ", x)
drops = [k for k in side["live"] if k not in ("errors", "turned") and side["patched"].get(k, 0) < side["live"][k]]
n_pages = pages["read"] + pages["read (salvaged)"] + pages["still failing"]
ok = not drops and side["patched"]["errors"] <= side["live"]["errors"] and n_pages and (pages["read"] + pages["read (salvaged)"]) >= n_pages / 2
json.dump({"controls": side, "failed_pages": pages, "examples": examples, "drops": drops, "pass": bool(ok)},
          open(ROOT / "reports" / "jt reader fix test 2026-11-06.json", "w"), indent=1)
print("VERDICT", "PASS" if ok else "FAIL", f"(control drops {drops}; failed pages read {pages['read'] + pages['read (salvaged)']}/{n_pages})", flush=True)

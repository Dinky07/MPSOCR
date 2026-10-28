#!/usr/bin/env python3
"""Training for the job-transfer (JT) reader (jt_reader.py), on the same engine as the order and workshop-sheet training.

Checked JT forms come from two places:
  * the Label Studio review set (<jt_review_dir>/train.jsonl and test.jsonl: 134 + 25 corrected forms, Oct 2026), and
  * JTs ticked Checked on the dashboard (synced, in the out queue or on hold); 1 in 5 of these is a test form.
The 25 review test forms (plus the dashboard test forms) are only ever scored, never shown to the reader; a form whose
JT number is in the test set is never used as a worked example.

Quick training adds worked examples the current reader has not seen: writers it has no example of first (Joanne,
Dican, ...), then the forms the reader got most wrong, 1-8 lines each. The new setup is installed only if it scores
better on the test forms, with no key field (JT number, job number, date, quantities) worse by more than one form.
Full training (fine-tuning) is parked for JTs: the 24 Oct purchase-order test showed the fine-tune path losing
accuracy in Ollama 0.40 (see the project review).

    python3 jt_training.py status
    python3 jt_training.py quick  [--force]
    python3 jt_training.py revert

Lives in <pipeline root>/training/jt/ (active.json, installed/<job>/, history/, reports/, candidates/); the running
job, its log and the GPU pause are shared with the other training (training/job.json), so one job runs at a time.
Each installed setup gets its own folder, so the worker picks up a new one without a restart.
"""
import argparse, difflib, json, re, shutil, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import training as T  # noqa: E402
import jt_reader  # noqa: E402

# Scored = the fields corrected in Label Studio (customer, transferred_by and the receiving stamp were not checked there)
FIELDS = ["jt_number", "date", "mps_job_no", "transferred_from", "transferred_to", "received_by", "remarks"]
LINE_FIELDS = ["lines_count", "line_qty", "line_description"]
KEY_FIELDS = ("jt_number", "mps_job_no", "date", "line_qty")
OK_STATUS = ("Corrected", "Correct as filled in")
DEFAULTS = {"jt_review_dir": "/srv/ocr/scans/training", "jt_quick_min_scored": 10, "jt_add_examples": 2,
            "jt_max_examples": 8, "jt_max_example_lines": 8, "jt_key_drop": 0.045, "jt_min_gain": 0.005}
_DATE = re.compile(r"^\s*(\d{1,4})[./\-\s](\d{1,2})[./\-\s](\d{1,4})\s*$")


def load_cfg():
    cfg, t = T.load_cfg()
    for k, v in DEFAULTS.items():
        t.setdefault(k, v)
    return cfg, t


def wdir(cfg):
    d = T.tdir(cfg) / "jt"
    for sub in ("reports", "history", "candidates", "installed"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def settings(cfg):
    """What the worker uses for JTs: config doc_types.JobTransfers.jt_reader, overridden by training/jt/active.json."""
    tc = (cfg.get("doc_types") or {}).get("JobTransfers") or {}
    c = tc.get("jt_reader") if isinstance(tc.get("jt_reader"), dict) else {}
    s = {"model": c.get("model", "qwen2.5vl:7b"), "fewshot_dir": c.get("fewshot_dir", str(HERE / "jt_fewshot")), "px": int(c.get("px", 1600))}
    act = T.read_json(wdir(cfg) / "active.json", {}) or {}
    for k in ("model", "fewshot_dir", "px"):
        if act.get(k):
            s[k] = act[k]
    return s


# ------------------------------------------------------------------------------------------------ scoring
def norm(v):
    s = str(v or "").strip()
    if s.lower() in ("[signature]", "[illegible]", "null", "none"):
        return ""
    m = _DATE.match(s)
    if m:                                               # 9.09.2026 == 09-09-2026 == 2026/09/09
        a, b, c = m.groups()
        y, mo, d = (a, b, c) if len(a) == 4 else (c, b, a)
        if len(y) == 2:
            y = "20" + y
        try:
            return f"{int(y):04d}{int(mo):02d}{int(d):02d}"
        except ValueError:
            pass
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _items(a):
    return [li for li in (a.get("line_items") or []) if isinstance(li, dict)]


def compare(read, ver):
    res = {}
    for k in FIELDS:
        x, y = norm(read.get(k)), norm(ver.get(k))
        if x == "" and y == "":
            continue
        res[k] = (int(x == y), 1)
    ri, vi = _items(read), _items(ver)
    if ri or vi:
        n = max(len(vi), 1)
        res["lines_count"] = (int(len(ri) == len(vi)), 1)
        res["line_qty"] = (sum(1 for x, y in zip(ri, vi) if norm(x.get("quantity")) == norm(y.get("quantity"))), n)
        res["line_description"] = (sum(1 for x, y in zip(ri, vi) if difflib.SequenceMatcher(
            None, norm(x.get("description")), norm(y.get("description"))).ratio() >= 0.9), n)
    return res


def score(pairs):
    tot, whole = {}, 0
    for read, ver in pairs:
        c = compare(read, ver)
        whole += int(bool(c) and all(r == n for r, n in c.values()))
        for k, (r, n) in c.items():
            t = tot.setdefault(k, [0, 0])
            t[0] += r
            t[1] += n
    fields = {k: {"right": r, "counted": c, "acc": round(r / c, 4) if c else None} for k, (r, c) in tot.items()}
    accs = [v["acc"] for v in fields.values() if v["acc"] is not None]
    return {"fields": fields, "overall": round(sum(accs) / len(accs), 4) if accs else None, "docs": len(pairs),
            "whole_form": round(whole / len(pairs), 4) if pairs else None}


def difficulty(e):
    """0 = the old reading was right everywhere, 1 = wrong everywhere (no reading known: 0.5)."""
    if not e.get("read"):
        return 0.5
    s = score([(e["read"], e["target"])])
    return 1 - (s["overall"] if s["overall"] is not None else 0.5)


# ------------------------------------------------------------------------------------------------ checked forms
def from_worker(w):
    """The worker's JT fields (dashboard) in the reader's answer schema."""
    return {"jt_number": w.get("jt_number"), "date": w.get("date"), "mps_job_no": w.get("job_number"), "customer": w.get("customer"),
            "transferred_from": w.get("from"), "transferred_to": w.get("to"), "transferred_by": w.get("written_by"),
            "received_by": w.get("received_by"), "remarks": w.get("remarks"), "receiving_stamp": w.get("receiving_stamp"),
            "line_items": [{"quantity": (f"{i.get('quantity') or ''} {i.get('unit') or ''}").strip() or None,
                            "item_no": i.get("item_no"), "description": i.get("description")} for i in w.get("items") or []]}


def _review(t, name, test):
    p = Path(t["jt_review_dir"]) / name
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("status") not in OK_STATUS or not Path(str(r.get("image"))).exists():
            continue
        out.append({"id": r["id"], "image": r["image"], "pdf": None, "target": r["target"], "read": r.get("baseline"),
                    "writer": (r.get("writer") or "UNKNOWN").upper(), "test": test, "from": "review",
                    "corrected": r.get("status") == "Corrected"})
    return out


def forms(cfg, t):
    """(train pool, test forms) over the review set and the dashboard-checked JTs."""
    tr, te = _review(t, "train.jsonl", False), _review(t, "test.jsonl", True)
    for e in T.examples(cfg):
        if e["doc_type"] != "jt" or not e["pdf"]:
            continue
        tgt, rd = from_worker(e["verified"]), from_worker(e["read"])
        x = {"id": e["hash"], "image": None, "pdf": e["pdf"], "target": tgt, "read": rd,
             "writer": str(e["verified"].get("written_by") or "UNKNOWN").upper(), "test": e["test"], "from": "dashboard",
             "corrected": e["edits"] > 0}
        (te if e["test"] else tr).append(x)
    test_jts = {norm(e["target"].get("jt_number")) for e in te} - {""}
    tr = [e for e in tr if norm(e["target"].get("jt_number")) not in test_jts]       # never learn from a test form
    return tr, te


# ------------------------------------------------------------------------------------------------ jobs
def evaluate(job, cfg, tests, model, fewshot_dir, px, label):
    job.step(f"scoring {label} on {len(tests)} test forms", len(tests))
    pairs, fails, docs = [], 0, []
    for e in tests:
        try:
            ans, _, _ = jt_reader.read(e["image"] or e["pdf"], cfg["ollama_url"], model, fewshot_dir, px,
                                       int(cfg.get("ollama_timeout", 1200)), cfg.get("keep_alive", "15m"))
        except Exception as ex:
            fails += 1
            job.log(f"  read failed for {e['id']}: {ex}")
            ans = {}
        pairs.append((ans, e["target"]))
        wrong = [k for k, (r, n) in compare(ans, e["target"]).items() if r < n]
        docs.append({"id": e["id"], "writer": e["writer"], "wrong": wrong})
        job.tick()
    s = score(pairs)
    s.update(model=model, fewshot_dir=str(fewshot_dir) if fewshot_dir else None, px=px, failed_reads=fails, docs_detail=docs)
    job.log(f"  {label}: overall {s['overall']}  whole forms {s['whole_form']}  " +
            ", ".join(f"{k} {v['acc']}" for k, v in sorted(s["fields"].items())))
    return s


def better(b, a, t):
    if b["overall"] is None or a["overall"] is None:
        return False, "nothing to compare"
    if b["overall"] < a["overall"] + float(t["jt_min_gain"]):
        return False, f"overall {b['overall']:.3f} vs {a['overall']:.3f}: not better by {t['jt_min_gain']}"
    for k in KEY_FIELDS:
        fa, fb = a["fields"].get(k, {}).get("acc"), b["fields"].get(k, {}).get("acc")
        if fa is not None and fb is not None and fb < fa - float(t["jt_key_drop"]):
            return False, f"{k} got worse ({fb:.3f} vs {fa:.3f})"
    return True, f"overall {b['overall']:.3f} vs {a['overall']:.3f}"


def last_scored(cfg):
    """The latest measured accuracy of the setup in use (from the last quick job), or None."""
    reps = sorted((wdir(cfg) / "reports").glob("jt-quick-*.json"))
    for r in reversed(reps):
        x = T.read_json(r) or {}
        if x.get("valid") is False:
            continue
        s = x.get("candidate") if x.get("installed") else x.get("current")
        if s and s.get("overall") is not None:
            return s, x.get("at")
    return None, None


def cmd_status(cfg, t, quiet=False):
    tr, te = forms(cfg, t)
    allx = tr + te
    cur = settings(cfg)
    used = set(T.read_json(Path(cur["fewshot_dir"]) / "sources.json") or [])
    rejected = T.rejected_examples(wdir(cfg))
    fresh = [e for e in tr if e["id"] not in used and e["id"] not in rejected and 1 <= len(_items(e["target"])) <= int(t["jt_max_example_lines"])]
    measured, at = last_scored(cfg)
    if measured:
        acc, note = measured, f"Measured on the {measured.get('docs')} test forms by the last quick training ({str(at or '').replace('T', ' ')[:16]})."
    else:
        acc = score([(e["read"], e["target"]) for e in allx if e.get("read")])
        note = "The original reading of the Label Studio forms (the reader before jt_reader): run quick training to measure the current reader."
    st = {"at": datetime.now().isoformat(timespec="seconds"), "reader": "jt",
          "jt": {"verified": len(allx), "corrected": sum(1 for e in allx if e["corrected"]), "train": len(tr), "test": len(te),
                 "corrected_train": sum(1 for e in tr if e["corrected"]), "from_review": sum(1 for e in allx if e["from"] == "review"),
                 "from_dashboard": sum(1 for e in allx if e["from"] == "dashboard"), "examples_in_use": len(used),
                 "untried": len(fresh), "rejected": len(rejected)},
          "accuracy": acc, "accuracy_note": note,
          "thresholds": {"quick_min_corrected": 1, "finetune_min_verified": None},
          "quick_ready": bool(fresh) and len(te) >= int(t["jt_quick_min_scored"]),
          "finetune_ready": False, "finetune_env": True, "finetune_parked": True, "active": cur}
    if not quiet:
        print(json.dumps({k: v for k, v in st.items() if k != "accuracy"} | {"accuracy": {k: v for k, v in acc.items() if k != "docs_detail"}}, indent=2))
    return st


def example_answer(tgt):
    a = json.loads(json.dumps(tgt))
    rs = a.get("receiving_stamp")
    if isinstance(rs, dict):              # Label Studio kept the stamp as fields; the reader answers it as text
        a["receiving_stamp"] = ", ".join(str(v) for v in rs.values() if v) or None
    for k in jt_reader.ANSWER_SCHEMA["required"]:
        a.setdefault(k, None if k != "line_items" else [])
    return a


def cmd_quick(cfg, t, force=False):
    job = T.Job(cfg, "jt-quick")
    rid = f"jt-quick-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        tr, tests = forms(cfg, t)
        job.log(f"{len(tr) + len(tests)} checked job transfers: {len(tr)} to learn from, {len(tests)} test forms")
        if len(tests) < int(t["jt_quick_min_scored"]):
            return job.finish("skipped", f"needs at least {t['jt_quick_min_scored']} test forms (has {len(tests)})")
        cur = settings(cfg)
        cur_dir = Path(cur["fewshot_dir"])
        cand = wdir(cfg) / "candidates" / rid
        cand.mkdir(parents=True, exist_ok=True)
        used = set(T.read_json(cur_dir / "sources.json") or [])
        rejected = T.rejected_examples(wdir(cfg))
        if rejected:
            job.log(f"  {len(rejected)} form(s) already tried as worked examples and not better: skipped")
        by_id = {e["id"]: e for e in tr + tests}
        covered = {by_id[i]["writer"] for i in used if i in by_id}
        for f in sorted(cur_dir.glob("*.json")):
            if f.name != "sources.json" and f.with_suffix(".png").exists():
                shutil.copy2(f, cand / f.name)
                shutil.copy2(f.with_suffix(".png"), cand / f.with_suffix(".png").name)
                covered.add(f.stem.split("_", 1)[-1].upper())
        have = len([f for f in cand.glob("*.json")])
        room = max(0, min(int(t["jt_add_examples"]), int(t["jt_max_examples"]) - have))
        if room == 0:
            return job.finish("skipped", f"already {have} worked examples (the most this reader takes is {t['jt_max_examples']})")
        pool = [e for e in tr if e["id"] not in used and e["id"] not in rejected and e["target"].get("jt_number")
                and 1 <= len(_items(e["target"])) <= int(t["jt_max_example_lines"])]
        ranked = sorted(pool, key=lambda e: (-round(difficulty(e), 2), -len(_items(e["target"])), e["id"]))
        job.log(f"  writers with worked examples: {', '.join(sorted(covered)) or 'none'}")
        picked, writers = [], set()
        for first_pass in (True, False):            # 1st: writers with no example yet, one each; 2nd: hardest forms
            for e in ranked:
                if len(picked) >= room:
                    break
                w = e["writer"]
                if e in picked or (first_pass and (w in covered or w in writers or "/" in w or w == "UNKNOWN")):
                    continue
                picked.append(e)
                writers.add(w)
        for i, e in enumerate(picked, 1):
            stem = cand / f"{have + i}_{re.sub(r'[^a-z0-9]', '', e['writer'].lower()) or 'form'}"
            e["_stem"] = stem
            if e["image"]:
                shutil.copy2(e["image"], stem.with_suffix(".png"))
            else:
                T.render_png(e["pdf"], stem.with_suffix(".png"), cur["px"])
            T.write_json(stem.with_suffix(".json"), example_answer(e["target"]))
            job.log(f"  + worked example: {e['id']} ({e['writer']}, {len(_items(e['target']))} line(s), "
                    f"old reading {round(100 * (1 - difficulty(e)))}% right)")
        num_ctx = int(getattr(jt_reader, "NUM_CTX", 32768))
        while picked and not T.fits(T.est_tokens(cand, jt_reader.PROMPT, cur["px"], 1), num_ctx, jt_reader.NUM_PREDICT):
            e = picked.pop()
            for f in (e["_stem"].with_suffix(".png"), e["_stem"].with_suffix(".json")):
                f.unlink(missing_ok=True)
            job.log(f"  - {e['id']}: left out, the request would be too long for the model's {num_ctx}-token context")
        if not picked:
            return job.finish("skipped", "no new forms to add as worked examples")
        job.log(f"  request size with the new examples: ~{T.est_tokens(cand, jt_reader.PROMPT, cur['px'], 1)} of {num_ctx} tokens")
        T.write_json(cand / "sources.json", sorted(used | {e["id"] for e in picked}))
        job.pause_worker("A/B scoring")
        a = evaluate(job, cfg, tests, cur["model"], str(cur_dir), cur["px"], "current setup")
        b = evaluate(job, cfg, tests, cur["model"], str(cand), cur["px"], "with new worked examples")
        valid, invalid_why = T.test_valid(a, b, len(tests))
        ok, why = better(b, a, t) if valid else (False, invalid_why)
        if valid and not ok:
            T.add_rejected(wdir(cfg), [e["id"] for e in picked], job, why)
        report = {"id": rid, "kind": "jt-quick", "at": datetime.now().isoformat(timespec="seconds"), "tests": len(tests),
                  "added": [f"{e['id']} ({e['writer']})" for e in picked], "added_ids": [e["id"] for e in picked],
                  "current": a, "candidate": b, "installed": ok, "valid": valid, "why": why}
        if ok:
            dest = wdir(cfg) / "installed" / rid           # own folder per install: the worker sees the new path at once
            shutil.copytree(cand, dest)
            old = T.read_json(wdir(cfg) / "active.json")
            if old:
                T.write_json(wdir(cfg) / "history" / f"active-{datetime.now():%Y%m%d-%H%M%S}.json", old)
            T.write_json(wdir(cfg) / "active.json", {"model": cur["model"], "fewshot_dir": str(dest), "px": cur["px"],
                                                     "installed_at": datetime.now().isoformat(timespec="seconds"), "job": rid})
            job.log(f"installed for job transfers: {dest}")
        T.write_json(wdir(cfg) / "reports" / f"{rid}.json", report)
        job.finish("done", ("installed: " if ok else "kept the current setup: ") + why)
    except Exception as ex:
        job.finish("failed", f"{type(ex).__name__}: {ex}")
        raise


def cmd_revert(cfg):
    d = wdir(cfg)
    hist = sorted((d / "history").glob("active-*.json"))
    if hist:
        T.write_json(d / "active.json", T.read_json(hist[-1]))
        hist[-1].unlink()
        print("reverted to", json.dumps(T.read_json(d / "active.json")))
    else:
        (d / "active.json").unlink(missing_ok=True)
        print("reverted to the settings in config.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "quick", "revert", "finetune"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg, t = load_cfg()
    if a.command == "finetune":
        sys.exit("Full training is parked for job transfers (fine-tuned Qwen2.5-VL loses accuracy through Ollama 0.40); use quick.")
    {"status": lambda: cmd_status(cfg, t), "quick": lambda: cmd_quick(cfg, t, a.force), "revert": lambda: cmd_revert(cfg)}[a.command]()


if __name__ == "__main__":
    main()

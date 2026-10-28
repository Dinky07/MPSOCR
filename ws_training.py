#!/usr/bin/env python3
"""Training for the workshop-sheet reader, from the sheets you have checked on the dashboard (Workshop Sheets tab).

Same rules as the purchase-order training (training.py, whose job plumbing this uses): a sheet counts once "Checked" is
ticked; 1 in 5 checked sheets is a TEST sheet (only scored, never trained on); a new setup is installed only if it
scores better on the test sheets than the current one. CAD-printed sheets are read from their text layer by rules,
not by the model, so they are left out of training and scoring.

    python3 ws_training.py status              # counts + how accurate the current reader is, per field
    python3 ws_training.py quick  [--force]    # new worked examples; installed only if it scores better
    python3 ws_training.py finetune [--force]  # fine-tune a workshop-sheet model (hours; needs setup_training.sh)
    python3 ws_training.py revert              # back to the previous setup

Lives in <pipeline root>/training/ws/ (active.json, fewshot_active/, history/, reports/, candidates/, finetune/);
the running job, its log and the GPU pause are shared with the order training (training/job.json, training/logs/), so
only one training job runs at a time.
"""
import argparse, difflib, json, os, re, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import training as T  # noqa: E402
import ws_reader  # noqa: E402

FIELDS = ["date", "sheet_no", "sheet_of", "project", "corrosion_int", "corrosion_ext", "destination", "job_number", "spec",
          "customer_dwg_no", "rev", "mps_dwg_no", "batch_no", "stamped", "ifc_signed", "stamp_date"]
BOOLS = {"stamped", "ifc_signed"}
LINE_FIELDS = ["lines_count", "line_item_no", "line_qty", "line_size", "line_dwg_no", "line_dims"]
DEFAULTS = {"ws_quick_min_corrected": 10, "ws_quick_min_scored": 6, "ws_finetune_min_verified": 200, "ws_add_examples": 2,
            "ws_max_examples": 6}


def load_cfg():
    cfg, t = T.load_cfg()
    for k, v in DEFAULTS.items():
        t.setdefault(k, v)
    return cfg, t


def wdir(cfg):
    d = T.tdir(cfg) / "ws"
    for sub in ("reports", "history", "candidates"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def settings(cfg):
    tc = (cfg.get("doc_types") or {}).get("WorkshopSheets") or {}
    c = tc.get("ws_reader") if isinstance(tc.get("ws_reader"), dict) else {}
    s = {"model": c.get("model", "qwen2.5vl:7b"), "fewshot_dir": c.get("fewshot_dir", str(HERE / "ws_fewshot")), "px": int(c.get("px", 1600))}
    act = T.read_json(wdir(cfg) / "active.json", {}) or {}
    for k in ("model", "fewshot_dir", "px"):
        if act.get(k):
            s[k] = act[k]
    return s


def ws_examples(cfg, model_only=True):
    """Checked workshop sheets (synced, in the out queue or on hold). model_only: leave out CAD sheets read by rules."""
    out = []
    for e in T.examples(cfg):
        if e["doc_type"] != "ws" or not e["pdf"]:
            continue
        sc = T.read_json(e["json"], {}) or {}
        e["how"] = sc.get("ws_how")
        e["layout"] = (e["verified"].get("layout") or "")
        e["job"] = e["verified"].get("job_number") or ""
        if model_only and e["how"] == "text_layer":
            continue
        out.append(e)
    return out


def _n(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def compare(read, ver):
    res = {}
    for k in FIELDS:
        a, b = read.get(k), ver.get(k)
        if k in BOOLS:
            if k not in ver:                     # checked before the stamp fields existed: not scored
                continue
            res[k] = (1 if bool(a) == bool(b) else 0, 1)
            continue
        if a in (None, "") and b in (None, ""):
            continue
        res[k] = (1 if _n(a) == _n(b) else 0, 1)
    ri, vi = read.get("items") or [], ver.get("items") or []
    if ri or vi:
        res["lines_count"] = (1 if len(ri) == len(vi) else 0, 1)
        c = {k: 0 for k in ("item", "qty", "size", "dwg", "dims")}
        for x, y in zip(ri, vi):
            c["item"] += 1 if _n(x.get("item_no")) == _n(y.get("item_no")) else 0
            c["qty"] += 1 if T._num(x.get("qty")) == T._num(y.get("qty")) else 0
            c["size"] += 1 if difflib.SequenceMatcher(None, _n(x.get("size")), _n(y.get("size"))).ratio() >= 0.9 else 0
            c["dwg"] += 1 if _n(x.get("dwg_no")) == _n(y.get("dwg_no")) else 0
            c["dims"] += 1 if {k: _n(v) for k, v in (x.get("dims") or {}).items()} == {k: _n(v) for k, v in (y.get("dims") or {}).items()} else 0
        n = max(len(vi), 1)
        res.update(line_item_no=(c["item"], n), line_qty=(c["qty"], n), line_size=(c["size"], n), line_dwg_no=(c["dwg"], n),
                   line_dims=(c["dims"], n))
    return res


def score(pairs):
    tot = {}
    for read, ver in pairs:
        for k, (r, c) in compare(read, ver).items():
            t = tot.setdefault(k, [0, 0])
            t[0] += r
            t[1] += c
    fields = {k: {"right": r, "counted": c, "acc": round(r / c, 4) if c else None} for k, (r, c) in tot.items()}
    accs = [v["acc"] for v in fields.values() if v["acc"] is not None]
    return {"fields": fields, "overall": round(sum(accs) / len(accs), 4) if accs else None, "docs": len(pairs)}


def evaluate(job, cfg, tests, model, fewshot_dir, px, label):
    job.step(f"scoring {label} on {len(tests)} test sheets", len(tests))
    pairs, fails = [], 0
    for e in tests:
        try:
            ans = ws_reader.read_model(e["pdf"], cfg["ollama_url"], model, fewshot_dir, px, int(cfg.get("ollama_timeout", 1200)),
                                       cfg.get("keep_alive", "15m"))
            got, _ = ws_reader.to_fields(ans, Path(e["pdf"]).name, cfg["root"])
        except Exception as ex:
            fails += 1
            job.log(f"  read failed for {Path(e['pdf']).name}: {ex}")
            got = {}
        pairs.append((got, e["verified"]))
        job.tick()
    s = score(pairs)
    s.update(model=model, fewshot_dir=str(fewshot_dir) if fewshot_dir else None, px=px, failed_reads=fails)
    job.log(f"  {label}: overall {s['overall']}  " + ", ".join(f"{k} {v['acc']}" for k, v in sorted(s["fields"].items())))
    return s


def better(b, a, min_gain):
    if b["overall"] is None or a["overall"] is None:
        return False, "nothing to compare"
    if b["overall"] < a["overall"] + min_gain:
        return False, f"overall {b['overall']:.3f} vs {a['overall']:.3f}: not better by {min_gain}"
    for k in ("job_number", "mps_dwg_no", "date", "line_qty"):
        fa, fb = a["fields"].get(k, {}).get("acc"), b["fields"].get(k, {}).get("acc")
        if fa is not None and fb is not None and fb < fa - 0.02:
            return False, f"{k} got worse ({fb:.3f} vs {fa:.3f})"
    return True, f"overall {b['overall']:.3f} vs {a['overall']:.3f}"


def install(cfg, job, new_active, report):
    d = wdir(cfg)
    old = T.read_json(d / "active.json")
    if old:
        T.write_json(d / "history" / f"active-{datetime.now():%Y%m%d-%H%M%S}.json", old)
    new_active.update(installed_at=datetime.now().isoformat(timespec="seconds"), job=report["id"])
    T.write_json(d / "active.json", new_active)
    job.log(f"installed for workshop sheets: {json.dumps(new_active)}")


def test_set(t, ex):
    return sorted([e for e in ex if e["test"]], key=lambda e: e["hash"])[: int(t["test_max"])]


def cmd_status(cfg, t, quiet=False):
    allx = ws_examples(cfg, model_only=False)
    ex = [e for e in allx if e["how"] != "text_layer"]
    acc = score([(e["read"], e["verified"]) for e in ex])
    acc_all = score([(e["read"], e["verified"]) for e in allx])
    corrected_train = sum(1 for e in ex if e["edits"] and not e["test"])
    venv_ok = Path(t["venv"], "bin", "python").exists()
    st = {"at": datetime.now().isoformat(timespec="seconds"), "reader": "ws",
          "ws": {"verified": len(ex), "verified_cad": len(allx) - len(ex), "corrected": sum(1 for e in ex if e["edits"]),
                 "train": sum(1 for e in ex if not e["test"]), "test": sum(1 for e in ex if e["test"]), "corrected_train": corrected_train},
          "accuracy": acc, "accuracy_all": acc_all,
          "thresholds": {"quick_min_corrected": t["ws_quick_min_corrected"], "finetune_min_verified": t["ws_finetune_min_verified"]},
          "quick_ready": corrected_train >= t["ws_quick_min_corrected"] and len(ex) - int(t["ws_add_examples"]) >= int(t["ws_quick_min_scored"]),
          "finetune_ready": len(ex) >= t["ws_finetune_min_verified"] and venv_ok, "finetune_env": venv_ok, "active": settings(cfg)}
    if not quiet:
        print(json.dumps(st, indent=2))
    return st


def cmd_quick(cfg, t, force=False):
    job = T.Job(cfg, "ws-quick")
    rid = f"ws-quick-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        ex = ws_examples(cfg)
        corrected = [e for e in ex if e["edits"] and not e["test"]]
        tests = test_set(t, ex)
        job.log(f"{len(ex)} checked workshop sheets (scans): {len(corrected)} corrected training sheets, {len(tests)} test sheets")
        if len(corrected) < t["ws_quick_min_corrected"] and not force:
            return job.finish("skipped", f"needs {t['ws_quick_min_corrected']} corrected sheets to learn from (has {len(corrected)})")
        cur = settings(cfg)
        cand = wdir(cfg) / "candidates" / rid
        cand.mkdir(parents=True, exist_ok=True)
        cur_dir = Path(cur["fewshot_dir"])
        used = set(T.read_json(cur_dir / "sources.json") or [])
        rejected = T.rejected_examples(wdir(cfg))                     # tried as examples before and scored worse
        if rejected:
            job.log(f"  {len(rejected)} sheet(s) already tried as worked examples and not better: skipped")
        for f in sorted(cur_dir.glob("*.json")):
            if f.name != "sources.json" and f.with_suffix(".png").exists():
                shutil.copy2(f, cand / f.name)
                shutil.copy2(f.with_suffix(".png"), cand / f.with_suffix(".png").name)
        have = len([f for f in cand.glob("*.json")])
        room = max(0, min(int(t["ws_add_examples"]), int(t["ws_max_examples"]) - have))
        ranked = sorted(corrected, key=lambda e: (-e["edits"], e["hash"]))
        picked, layouts, jobs = [], set(), set()
        for first_pass in (True, False):
            for e in ranked:
                if len(picked) >= room:
                    break
                if e in picked or e["hash"] in used or e["hash"] in rejected or _n(e["job"]) in jobs or (first_pass and e["layout"] in layouts):
                    continue
                picked.append(e)
                layouts.add(e["layout"])
                jobs.add(_n(e["job"]))
        for i, e in enumerate(picked, 1):
            stem = cand / f"{have + i}_verified_{_n(e['job'])[:12] or 'sheet'}"
            e["_stem"] = stem
            T.render_png(e["pdf"], stem.with_suffix(".png"), cur["px"])
            T.write_json(stem.with_suffix(".json"), ws_reader.to_answer(e["verified"]))
            job.log(f"  + worked example: {Path(e['pdf']).name} ({e['edits']} corrections)")
        num_ctx = int(getattr(ws_reader, "NUM_CTX", 32768))
        while picked and not T.fits(T.est_tokens(cand, ws_reader.PROMPT, cur["px"], 1), num_ctx, ws_reader.NUM_PREDICT):
            e = picked.pop()
            for f in (e["_stem"].with_suffix(".png"), e["_stem"].with_suffix(".json")):
                f.unlink(missing_ok=True)
            job.log(f"  - {Path(e['pdf']).name}: left out, the request would be too long for the model's {num_ctx}-token context")
        if not picked:
            return job.finish("skipped", "no new sheets to add as worked examples")
        job.log(f"  request size with the new examples: ~{T.est_tokens(cand, ws_reader.PROMPT, cur['px'], 1)} of {num_ctx} tokens")
        T.write_json(cand / "sources.json", sorted(used | {e["hash"] for e in picked}))
        excl = used | {e["hash"] for e in picked}
        if len(tests) < int(t["ws_quick_min_scored"]):
            extra = sorted((e for e in ex if not e["test"] and e["hash"] not in excl), key=lambda e: e["hash"])
            tests = tests + extra[: int(t["ws_quick_min_scored"]) * 2 - len(tests)]
            job.log(f"  scoring on {len(tests)} sheets (test sheets + checked sheets not used as worked examples)")
        if len(tests) < int(t["ws_quick_min_scored"]):
            return job.finish("skipped", f"needs at least {t['ws_quick_min_scored']} checked sheets to score against, besides the worked examples (has {len(tests)})")
        job.pause_worker("A/B scoring")
        a = evaluate(job, cfg, tests, cur["model"], str(cur_dir), cur["px"], "current setup")
        ws_reader._FEW.clear()
        b = evaluate(job, cfg, tests, cur["model"], str(cand), cur["px"], "with new worked examples")
        valid, invalid_why = T.test_valid(a, b, len(tests))
        ok, why = better(b, a, t["min_gain"]) if valid else (False, invalid_why)
        if valid and not ok:
            T.add_rejected(wdir(cfg), [e["hash"] for e in picked], job, why)
        report = {"id": rid, "kind": "ws-quick", "at": datetime.now().isoformat(timespec="seconds"), "tests": len(tests),
                  "added": [Path(e["pdf"]).name for e in picked], "added_hashes": [e["hash"] for e in picked],
                  "current": a, "candidate": b, "installed": ok, "valid": valid, "why": why}
        if ok:
            active = wdir(cfg) / "fewshot_active"
            if active.exists():
                shutil.move(str(active), str(wdir(cfg) / "history" / f"fewshot-{datetime.now():%Y%m%d-%H%M%S}"))
            shutil.copytree(cand, active)
            install(cfg, job, {"model": cur["model"], "fewshot_dir": str(active), "px": cur["px"]}, report)
        T.write_json(wdir(cfg) / "reports" / f"{rid}.json", report)
        job.finish("done", ("installed: " if ok else "kept the current setup: ") + why)
    except Exception as ex:
        job.finish("failed", f"{type(ex).__name__}: {ex}")
        raise


def cmd_finetune(cfg, t, force=False):
    job = T.Job(cfg, "ws-finetune")
    rid = f"ws-finetune-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        py = Path(t["venv"], "bin", "python")
        if not py.exists():
            return job.finish("skipped", f"training environment not installed ({py}): run setup_training.sh first")
        ex = ws_examples(cfg)
        train = [e for e in ex if not e["test"]]
        tests = test_set(t, ex)
        job.log(f"{len(ex)} checked workshop sheets: {len(train)} to train on, {len(tests)} to test")
        if len(ex) < t["ws_finetune_min_verified"] and not force:
            return job.finish("skipped", f"needs {t['ws_finetune_min_verified']} checked sheets (has {len(ex)})")
        if len(tests) < 6:
            return job.finish("skipped", f"needs at least 6 checked test sheets (has {len(tests)})")
        work = wdir(cfg) / "finetune" / rid
        data = work / "data"
        (data / "images").mkdir(parents=True, exist_ok=True)
        px = int(t["ft_px"])
        job.step("building the training set", len(train))
        with open(data / "train.jsonl", "w", encoding="utf-8") as fh:
            for e in train:
                img = data / "images" / f"{e['hash'][:16]}.png"
                if not img.exists():
                    T.render_png(e["pdf"], img, px)
                fh.write(json.dumps({"image": f"images/{img.name}", "prompt": ws_reader.PROMPT,
                                     "answer": json.dumps(ws_reader.to_answer(e["verified"]), ensure_ascii=False)}, ensure_ascii=False) + "\n")
                job.tick()
        cur = settings(cfg)
        job.pause_worker("fine-tuning on the GPU")
        T.unload_ollama(cfg, job)
        job.step("fine-tuning (this takes hours; progress in the log)")
        cmd = [str(py), str(HERE / "finetune_unsloth.py"), "--data", str(data), "--out", str(work), "--base", t["base_model"],
               "--epochs", str(t["ft_epochs"]), "--rank", str(t["ft_rank"])]
        with open(job.log_path, "a", encoding="utf-8") as fh:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=dict(os.environ, HF_HOME=t.get("hf_home", "/srv/ocr/hf-cache")))
        if r.returncode != 0 or not (work / "merged").exists():
            return job.finish("failed", f"fine-tuning stopped (exit {r.returncode}); see the log")
        name = f"ws-reader-ft-{datetime.now():%Y%m%d}"
        job.step(f"importing into Ollama as {name}")
        if not T.ollama_import(t, job, name, work):     # ollama on the PATH, or inside its Docker container
            return job.finish("failed", "Ollama could not import the fine-tuned model (see the log); the current setup is unchanged")
        a = evaluate(job, cfg, tests, cur["model"], cur["fewshot_dir"], cur["px"], "current setup")
        b1 = evaluate(job, cfg, tests, name, None, px, "fine-tuned model")
        ws_reader._FEW.clear()
        b2 = evaluate(job, cfg, tests, name, cur["fewshot_dir"], px, "fine-tuned model + worked examples")
        b = b2 if (b2["overall"] or 0) > (b1["overall"] or 0) else b1
        ok, why = better(b, a, t["min_gain"])
        report = {"id": rid, "kind": "ws-finetune", "at": datetime.now().isoformat(timespec="seconds"), "trained_on": len(train),
                  "tests": len(tests), "model": name, "current": a, "candidate": b, "installed": ok, "why": why}
        if ok:
            (wdir(cfg) / "no_examples").mkdir(exist_ok=True)
            install(cfg, job, {"model": name, "fewshot_dir": b["fewshot_dir"] or str(wdir(cfg) / "no_examples"), "px": px}, report)
        T.write_json(wdir(cfg) / "reports" / f"{rid}.json", report)
        job.finish("done", ("installed " + name + ": " if ok else "kept the current setup: ") + why)
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
    ap.add_argument("command", choices=["status", "quick", "finetune", "revert"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg, t = load_cfg()
    {"status": lambda: cmd_status(cfg, t), "quick": lambda: cmd_quick(cfg, t, a.force),
     "finetune": lambda: cmd_finetune(cfg, t, a.force), "revert": lambda: cmd_revert(cfg)}[a.command]()


if __name__ == "__main__":
    main()

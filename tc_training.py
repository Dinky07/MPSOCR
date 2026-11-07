#!/usr/bin/env python3
"""Training for the test-certificate reader (tc_reader.py), on the same engine as the order, workshop-sheet and JT
training (Dino 26 Oct: a Test Certificates card on the Training tab).

What the reader is scored on:
  * heat numbers against the old site's (the old library's Heat No column is right - Dino 25 Oct): every cert from the
    backfill carries them, so hundreds of certs can be scored without checking each one by hand. The reader's OWN
    reading is scored (heat_no_read before the old-site correction);
  * on certs ticked Checked on the dashboard: cert no, date, mill, orders, family, grade and the item lines
    (line count, sizes, quantities, heat numbers).
1 in 5 certs (by file hash) is a test cert: only ever scored, never used as a worked example.

Quick training tries worked examples for the reader (one per family: pipe, flanges, fittings): one-page certs whose
reading is known good - ticked Checked, or every heat right against the old site with no review checks - with the
page's answer corrected where it was checked. The new setup is installed only if it scores better on the test certs
(heat numbers first) with no key field worse by more than the allowed drop. Full training is parked for certs.

    python3 tc_training.py status
    python3 tc_training.py quick [--force]
    python3 tc_training.py revert

Lives in <pipeline root>/training/tc/ (active.json, installed/<job>/, history/, reports/, candidates/); the running job,
its log and the GPU pause are shared with the other training (training/job.json), so one job runs at a time.
"""
import argparse, glob, json, re, shutil, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import training as T  # noqa: E402
import tc_reader  # noqa: E402

FIELDS = ["cert_no", "date", "mill", "mps_po", "ronin_po", "family", "grade"]
KEY_FIELDS = ("heat_old_site", "line_heat", "line_size_nb", "line_qty")
FAMS = ("Pipe", "Flanges", "Fittings")
DEFAULTS = {"tc_quick_min_scored": 8, "tc_test_max": 100, "tc_test_max_pages": 3, "tc_key_drop": 0.03, "tc_min_gain": 0.005}
_HK = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def load_cfg():
    cfg, t = T.load_cfg()
    for k, v in DEFAULTS.items():
        t.setdefault(k, v)
    return cfg, t


def wdir(cfg):
    d = T.tdir(cfg) / "tc"
    for sub in ("reports", "history", "candidates", "installed"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def settings(cfg):
    """What the worker uses for certs: config doc_types.TestCerts.tc_reader, overridden by training/tc/active.json."""
    tc = (cfg.get("doc_types") or {}).get("TestCerts") or {}
    c = dict(tc.get("tc_reader")) if isinstance(tc.get("tc_reader"), dict) else {}
    s = {"model": c.get("model", "qwen3-vl:8b-instruct"), "fewshot_dir": c.get("fewshot_dir", ""), "px": int(c.get("px", 1600)),
         "chem_px": int(c.get("chem_px", 2000)), "max_pages": int(c.get("max_pages", 30))}
    act = T.read_json(wdir(cfg) / "active.json", {}) or {}
    for k in ("model", "fewshot_dir", "px"):
        if act.get(k) not in (None, ""):
            s[k] = act[k]
    return s


# ------------------------------------------------------------------------------------------------ certs
def certs(cfg):
    """Every cert the pipeline has read (out, hold, synced) with its labels: checked fields and / or old-site heats."""
    root = Path(cfg["root"])
    out, seen = [], set()
    for pat in ("out/TestCerts/*.json", "out/TestCerts/hold/*.json", "synced/TestCerts/*/*.json"):
        for p in sorted(glob.glob(str(root / pat))):
            sc = T.read_json(p)
            if not isinstance(sc, dict) or sc.get("doc_type") != "test_cert":
                continue
            f = sc.get("fields") or {}
            h = (f.get("content_hash") or sc.get("source_sha256") or Path(p).name).upper()
            if h in seen:
                continue
            seen.add(h)
            pdf = Path(p).with_name(str(sc.get("file") or ""))
            gold = tc_reader.old_heats(f.get("old_columns"))
            out.append({"id": h, "json": p, "pdf": str(pdf) if pdf.is_file() else None, "fields": f, "sc": sc,
                        "read": sc.get("fields_read") or None, "checked": bool(f.get("checked")), "gold_heats": gold,
                        "edits": sum(len(e.get("changes") or []) for e in (sc.get("edits") or [])),
                        "pages": int(f.get("pages") or 0), "family": f.get("family"), "test": T.is_test(h),
                        "file": sc.get("file")})
    return out


def read_heats(f):
    """The heat numbers as the READER read them (before the old-site correction)."""
    ks = []
    for x in [h for h in (f.get("heats") or []) if isinstance(h, dict)] + [i for i in (f.get("items") or []) if isinstance(i, dict)]:
        k = _HK(x.get("heat_no_read") or x.get("heat_no"))
        if k and k not in ks:
            ks.append(k)
    return ks


# ------------------------------------------------------------------------------------------------ scoring
def _n(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def _num(v):
    try:
        return None if v in (None, "") else round(float(v), 3)
    except (TypeError, ValueError):
        return None


def compare(read, e):
    """read = reader fields for one cert; e = the cert (labels). -> {metric: (right, counted)}"""
    res = {}
    if e["gold_heats"]:
        got = set(read_heats(read))
        res["heat_old_site"] = (sum(1 for g in e["gold_heats"] if g in got), len(e["gold_heats"]))
    if e["checked"]:
        ver = e["fields"]
        for k in FIELDS:
            a, b = read.get(k), ver.get(k)
            if a in (None, "") and b in (None, ""):
                continue
            if k in ("mps_po", "ronin_po"):
                ok = set(_n(x) for x in str(a or "").split(";") if _n(x)) == set(_n(x) for x in str(b or "").split(";") if _n(x))
            else:
                ok = _n(a) == _n(b)
            res[k] = (int(ok), 1)
        ri = [i for i in (read.get("items") or []) if isinstance(i, dict)]
        vi = [i for i in (ver.get("items") or []) if isinstance(i, dict)]
        if ri or vi:
            n = max(len(vi), 1)
            res["lines_count"] = (int(len(ri) == len(vi)), 1)
            res["line_size_nb"] = (sum(1 for x, y in zip(ri, vi) if _num(x.get("size_nb")) == _num(y.get("size_nb"))), n)
            res["line_qty"] = (sum(1 for x, y in zip(ri, vi) if _num(x.get("qty")) == _num(y.get("qty"))), n)
            res["line_heat"] = (sum(1 for x, y in zip(ri, vi) if _HK(x.get("heat_no_read") or x.get("heat_no")) == _HK(y.get("heat_no"))), n)
    return res


def score(pairs):
    tot = {}
    for read, e in pairs:
        for k, (r, c) in compare(read, e).items():
            t = tot.setdefault(k, [0, 0])
            t[0] += r
            t[1] += c
    fields = {k: {"right": r, "counted": c, "acc": round(r / c, 4) if c else None} for k, (r, c) in tot.items()}
    accs = [v["acc"] for v in fields.values() if v["acc"] is not None]
    return {"fields": fields, "overall": round(sum(accs) / len(accs), 4) if accs else None, "docs": len(pairs)}


def labelled(e):
    return e["pdf"] and (e["checked"] or e["gold_heats"])


def test_certs(t, allc):
    """The test certs scored by a quick job: labelled, at most tc_test_max_pages pages (a job stays about an hour)."""
    te = [e for e in allc if e["test"] and labelled(e) and 0 < e["pages"] <= int(t["tc_test_max_pages"])]
    te.sort(key=lambda e: (not e["checked"], e["id"]))          # checked certs first, then by hash
    return te[: int(t["tc_test_max"])]


# ------------------------------------------------------------------------------------------------ jobs
def read_cert(cfg, e, s, fewshot_dir):
    pages, _ = tc_reader.read(e["pdf"], cfg["ollama_url"], s["model"], fewshot_dir or None,
                              e["fields"].get("family") if e["fields"].get("family") in FAMS else None,
                              int(s["max_pages"]), int(s["px"]), int(s["chem_px"]), int(cfg.get("ollama_timeout", 1200)),
                              cfg.get("keep_alive", "15m"))
    fam = e["fields"].get("family") if e["fields"].get("family") in FAMS else None
    f, _ = tc_reader.to_fields(tc_reader.merge(pages, e["file"] or "", fam), e["file"] or "", fam)
    return f


def evaluate(job, cfg, tests, s, fewshot_dir, label):
    job.step(f"scoring {label} on {len(tests)} test certificates", len(tests))
    pairs, fails = [], 0
    for e in tests:
        try:
            f = read_cert(cfg, e, s, fewshot_dir)
        except Exception as ex:
            fails += 1
            job.log(f"  read failed for {e['file']}: {ex}")
            f = {}
        pairs.append((f, e))
        job.tick()
    sc = score(pairs)
    sc.update(model=s["model"], fewshot_dir=fewshot_dir or None, px=s["px"], failed_reads=fails)
    job.log(f"  {label}: overall {sc['overall']}  " + ", ".join(f"{k} {v['acc']}" for k, v in sorted(sc["fields"].items())))
    return sc


def better(b, a, t):
    if b["overall"] is None or a["overall"] is None:
        return False, "nothing to compare"
    fa, fb = (a["fields"].get("heat_old_site") or {}).get("acc"), (b["fields"].get("heat_old_site") or {}).get("acc")
    if b["overall"] < a["overall"] + float(t["tc_min_gain"]):
        return False, f"overall {b['overall']:.3f} vs {a['overall']:.3f}: not better by {t['tc_min_gain']}"
    for k in KEY_FIELDS:
        x, y = (a["fields"].get(k) or {}).get("acc"), (b["fields"].get(k) or {}).get("acc")
        if x is not None and y is not None and y < x - float(t["tc_key_drop"]):
            return False, f"{k} got worse ({y:.3f} vs {x:.3f})"
    return True, f"overall {b['overall']:.3f} vs {a['overall']:.3f}" + (f"; heats vs old site {fb:.3f} vs {fa:.3f}" if fa is not None and fb is not None else "")


def last_scored(cfg):
    for r in reversed(sorted((wdir(cfg) / "reports").glob("tc-quick-*.json"))):
        x = T.read_json(r) or {}
        if x.get("valid") is False:
            continue
        s = x.get("candidate") if x.get("installed") else x.get("current")
        if s and s.get("overall") is not None:
            return s, x.get("at")
    return None, None


def good_example(e):
    """A one-page cert whose reading can teach: checked, or every old-site heat right as read with no review checks."""
    if not e["pdf"] or e["test"] or e["pages"] != 1 or e["family"] not in FAMS:
        return False
    tp = e["sc"].get("tc_pages") or []
    if not tp or not isinstance((tp[0] or {}).get("answer"), dict) or (tp[0]["answer"].get("page_type") != "mtc"):
        return False
    if e["checked"]:
        return True
    hc = e["fields"].get("heat_check") or {}
    return bool(hc.get("old_site")) and hc.get("exact") == hc.get("old_site") and not hc.get("not_on_old_site") and not e["fields"].get("review")


def example_answer(e):
    """The page's own answer, with what was checked written back (items, header) and old-site heat corrections."""
    a = json.loads(json.dumps(e["sc"]["tc_pages"][0]["answer"]))
    f = e["fields"]
    if e["checked"]:
        for k, src in (("cert_no", "cert_no"), ("mill", "mill"), ("supplier", "supplier"), ("customer", "customer"),
                       ("standard", "standard"), ("grade", "grade"), ("date_iso", "date")):
            if f.get(src) not in (None, ""):
                a[k] = f[src]
        items = []
        for i in [x for x in (f.get("items") or []) if isinstance(x, dict)]:
            items.append({"line": i.get("#"), "product": i.get("product"), "description": i.get("description"),
                          "size_text": i.get("size_text"), "size2_text": None, "od_mm": i.get("od_mm"), "wt_text": i.get("schedule"),
                          "class": i.get("class"), "ends": i.get("ends"), "length_m": i.get("length_m"), "qty": i.get("qty"),
                          "unit": i.get("unit"), "total_m": i.get("total_m"), "weight_kg": i.get("weight_kg"),
                          "heat_no": i.get("heat_no"), "batch": i.get("batch")})
        if items:
            a["items"] = items
    # the orders and the credit found on the cert's own text (tc_orders) belong in order_numbers; a cert number that is
    # really an order number ("SC No. DF0940/DF0941" read as the cert number) is dropped
    have = {_HK(o.get("value")) for o in (a.get("order_numbers") or []) if isinstance(o, dict)}
    orders = [x.strip() for k in ("ronin_po", "mps_po", "contract") for x in str(f.get(k) or "").split(";") if x.strip()]
    for v in orders:
        if _HK(v) not in have:
            a.setdefault("order_numbers", []).append({"label": "Order No", "value": v})
    for v in [x.strip() for x in str(f.get("lc_number") or "").split(";") if x.strip()]:
        if _HK(v) not in have:
            a.setdefault("order_numbers", []).append({"label": "Documentary Credit Number", "value": v})
    if a.get("cert_no") and any(_HK(o) in _HK(a["cert_no"]) for o in orders) and not (e["checked"] and f.get("cert_no") == a["cert_no"]):
        a["cert_no"] = None
    fixes = {_HK(x.get("heat_no_read")): x.get("heat_no") for x in (f.get("heats") or []) + (f.get("items") or [])
             if isinstance(x, dict) and x.get("heat_no_read")}
    for row in (a.get("heats") or []) + (a.get("items") or []):
        if isinstance(row, dict) and _HK(row.get("heat_no")) in fixes:
            row["heat_no"] = fixes[_HK(row["heat_no"])]
    return a


def cmd_status(cfg, t, quiet=False):
    allc = certs(cfg)
    lab = [e for e in allc if labelled(e)]
    tests = test_certs(t, allc)
    cur = settings(cfg)
    used = set(T.read_json(Path(cur["fewshot_dir"]) / "sources.json") or []) if cur["fewshot_dir"] else set()
    rejected = T.rejected_examples(wdir(cfg))
    fresh = [e for e in allc if good_example(e) and e["id"] not in used and e["id"] not in rejected]
    measured, at = last_scored(cfg)
    if measured:
        acc, note = measured, f"Measured on the {measured.get('docs')} test certificates by the last quick training ({str(at or '').replace('T', ' ')[:16]})."
    else:
        acc = score([(e["read"] or e["fields"], e) for e in lab])        # checked certs: what was read before the edits
        note = "The reader's own reading of every labelled cert (heat numbers before the old-site correction; checked certs' fields as read)."
    st = {"at": datetime.now().isoformat(timespec="seconds"), "reader": "tc",
          "tc": {"verified": len(lab), "checked": sum(1 for e in allc if e["checked"]), "old_site": sum(1 for e in allc if e["gold_heats"]),
                 "read": len(allc), "corrected": sum(1 for e in allc if e["edits"]), "train": sum(1 for e in lab if not e["test"]),
                 "test": len(tests), "test_all": sum(1 for e in lab if e["test"]), "examples_in_use": len(used),
                 "untried": len(fresh), "untried_by_family": {f: sum(1 for e in fresh if e["family"] == f) for f in FAMS},
                 "rejected": len(rejected)},
          "accuracy": acc, "accuracy_note": note,
          "thresholds": {"quick_min_corrected": 1, "finetune_min_verified": None},
          "quick_ready": bool(fresh) and len(tests) >= int(t["tc_quick_min_scored"]),
          "finetune_ready": False, "finetune_env": True, "finetune_parked": True, "active": cur}
    if not quiet:
        print(json.dumps(st, indent=2, default=str))
    return st


def cmd_quick(cfg, t, force=False):
    job = T.Job(cfg, "tc-quick")
    rid = f"tc-quick-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        allc = certs(cfg)
        tests = test_certs(t, allc)
        job.log(f"{len(allc)} certificates read; {sum(1 for e in allc if labelled(e))} labelled "
                f"({sum(1 for e in allc if e['checked'])} checked, {sum(1 for e in allc if e['gold_heats'])} with old-site heats); {len(tests)} test certs scored")
        if len(tests) < int(t["tc_quick_min_scored"]) and not force:
            return job.finish("skipped", f"needs at least {t['tc_quick_min_scored']} test certificates (has {len(tests)})")
        cur = settings(cfg)
        cur_dir = Path(cur["fewshot_dir"]) if cur["fewshot_dir"] else None
        cand = wdir(cfg) / "candidates" / rid
        cand.mkdir(parents=True, exist_ok=True)
        used = set(T.read_json(cur_dir / "sources.json") or []) if cur_dir else set()
        rejected = T.rejected_examples(wdir(cfg))
        have_fams = set()
        if cur_dir and cur_dir.is_dir():
            for f in sorted(cur_dir.glob("*.json")):
                if f.name != "sources.json" and f.with_suffix(".png").exists():
                    shutil.copy2(f, cand / f.name)
                    shutil.copy2(f.with_suffix(".png"), cand / f.with_suffix(".png").name)
                    m = re.match(r"\d+_([a-z]+)_", f.name)
                    if m:
                        have_fams.add(m.group(1))
        pool = [e for e in allc if good_example(e) and e["id"] not in used and e["id"] not in rejected]
        # one per family, the family without an example first; prefer checked certs, then the most heats
        picked = []
        for fam in sorted(FAMS, key=lambda x: x.lower() in have_fams):
            c = sorted([e for e in pool if e["family"] == fam], key=lambda e: (not e["checked"], -len(e["gold_heats"]),
                                                                              -len(e["fields"].get("items") or []), e["id"]))
            if c:
                picked.append(c[0])
            if len(picked) >= 2:                        # two new examples per job keeps the A/B readable
                break
        if not picked:
            return job.finish("skipped", "no new one-page certificates good enough to use as worked examples")
        n0 = len(list(cand.glob("*.json")))
        for k, e in enumerate(picked, 1):
            stem = cand / f"{n0 + k}_{e['family'].lower()}_{e['id'][:8].lower()}"
            for old in cand.glob(f"*_{e['family'].lower()}_*"):        # one example per family: the new one replaces
                old.unlink()
            T.render_png(e["pdf"], stem.with_suffix(".png"), cur["px"])
            T.write_json(stem.with_suffix(".json"), example_answer(e))
            job.log(f"  + worked example: {e['file']} ({e['family']}, {'checked' if e['checked'] else 'heats right vs old site'}, "
                    f"{len(e['fields'].get('items') or [])} item(s), {len(e['gold_heats'])} heat(s))")
        T.write_json(cand / "sources.json", sorted(used | {e["id"] for e in picked}))
        job.pause_worker("A/B scoring")
        a = evaluate(job, cfg, tests, cur, str(cur_dir) if cur_dir else "", "current setup")
        b = evaluate(job, cfg, tests, cur, str(cand), "with worked examples")
        valid, invalid_why = T.test_valid(a, b, len(tests))
        ok, why = better(b, a, t) if valid else (False, invalid_why)
        if valid and not ok:
            T.add_rejected(wdir(cfg), [e["id"] for e in picked], job, why)
        report = {"id": rid, "kind": "tc-quick", "at": datetime.now().isoformat(timespec="seconds"), "tests": len(tests),
                  "added": [f"{e['file']} ({e['family']})" for e in picked], "added_ids": [e["id"] for e in picked],
                  "current": a, "candidate": b, "installed": ok, "valid": valid, "why": why}
        if ok:
            dest = wdir(cfg) / "installed" / rid
            shutil.copytree(cand, dest)
            old = T.read_json(wdir(cfg) / "active.json")
            if old:
                T.write_json(wdir(cfg) / "history" / f"active-{datetime.now():%Y%m%d-%H%M%S}.json", old)
            T.write_json(wdir(cfg) / "active.json", {"model": cur["model"], "fewshot_dir": str(dest), "px": cur["px"],
                                                     "installed_at": datetime.now().isoformat(timespec="seconds"), "job": rid})
            job.log(f"installed for test certificates: {dest}")
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
        sys.exit("Full training is parked for test certificates; use quick.")
    {"status": lambda: cmd_status(cfg, t), "quick": lambda: cmd_quick(cfg, t, a.force), "revert": lambda: cmd_revert(cfg)}[a.command]()


if __name__ == "__main__":
    main()

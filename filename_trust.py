#!/usr/bin/env python3
"""503 (Dino 5 Nov): supplier and job from the original file name, checked against MPSBusiness (the Sage vendor list
suppliers.csv / verified suppliers, the Sage job list jobs_sage.csv).
  test   made-up readings through the patched reader (/tmp/po503/po_reader.py), and every stored order flagged
         "file name says supplier X" / "job: file name says ...": how many the lists confirm, and how often the buying
         spreadsheet agrees with the file name vs with the reading. PASS: the made-up cases behave and, where the
         spreadsheet has the order, it sides with the file name at least 9 times in 10.
  fix    (--apply) the same orders in the Purchase Order queue and hold: a confirmed file-name supplier becomes the
         supplier (the reading kept as supplier_read), a confirmed job drops its check; hold reason recounted; the
         change is in edits. Checked orders are left alone.
    filename_trust.py test | fix [--apply]"""
import csv, difflib, glob, importlib.util, json, os, re, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
ROOT = Path("/srv/ocr/scans/pipeline")
SUP_RX = re.compile(r"^check: file name says supplier (.+)$")
JOB_RX = re.compile(r"^check: job: file name says (.+?), form reads (.+)$")
n = lambda v: re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def lists():
    sup = {n(r.get("Name")) for r in csv.DictReader(open(ROOT / "suppliers.csv", encoding="utf-8-sig"))}
    try:
        sup |= {n(x if isinstance(x, str) else x.get("name")) for x in json.load(open(ROOT / "training" / "suppliers_verified.json")).get("all") or []}
    except Exception:
        pass
    sup = {x for x in sup if len(x) >= 4}
    jobs = {n(r.get("JobNumber")) for r in csv.DictReader(open(ROOT / "jobs_sage.csv", encoding="utf-8-sig"))}
    return sup, jobs


def known_sup(name, sup):
    k = n(name)
    return len(k) >= 4 and any(k in x or x in k for x in sup)


def same(a, b):
    x, y = n(a), n(b)
    return bool(x and y) and (x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= 0.7)


def test():
    spec = importlib.util.spec_from_file_location("po503", "/tmp/po503/po_reader.py")
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    P.JOBS_DIR = str(ROOT)
    base = {"doc_type": "purchase_order", "form": "mps_order", "order_number": "A81157", "supplier": "BOSROBAC",
            "date_iso": "2025-09-29", "job_number": "31333E", "lines": [{"qty": 1, "description": "X", "unit_price": 1}]}
    ok = True
    for label, d, name, want_sup, want_check in [
            ("BESTOBELL file name, misread supplier", base, "BESTOBELL-A81157-31337E.pdf", "BESTOBELL", False),
            ("unknown supplier in the file name", base, "ZZQX TRADING-A81157-31337E.pdf", "BOSROBAC", True),
            ("job not in the Sage list", dict(base, supplier="BESTOBELL"), "BESTOBELL-A81157-99991E.pdf", "BESTOBELL", True),
            ("Kusasa file name (KUSASA-FORCEFLOW)", dict(base, supplier="FORCEFLOW", job_number=None), "PO2303-A78079-KUSASA-FORCEFLOW.pdf", "FORCEFLOW", False)]:
        d = json.loads(json.dumps(d))
        d2 = P.tidy(d, "PurchaseOrders__" + name) or d
        d2 = d2 if isinstance(d2, dict) else d
        flags = [str(x) for x in (d2.get("_checks") or d2.get("checks") or [])]
        chk_txt = json.dumps(d2, default=str)
        has_check = ("file name says supplier" in chk_txt) or ("job: file name says" in chk_txt)
        good = d2.get("supplier") == want_sup and has_check == want_check
        print(f"  {label:40} supplier {d2.get('supplier')!s:12} job {d2.get('job_number')!s:8} check {has_check}  {'ok' if good else 'WRONG'}")
        ok &= good
    sup, jobs = lists()
    regs = json.load(open(ROOT / "work" / "po_daily" / "registers.json", encoding="utf-8"))
    c, seen, ex = Counter(), set(), []
    for p in glob.glob(str(ROOT / "out/PurchaseOrders/*.json")) + glob.glob(str(ROOT / "out/PurchaseOrders/hold/*.json")) + glob.glob(str(ROOT / "synced/PurchaseOrders/*/*.json")):
        try:
            s = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        f = s.get("fields_read") or s.get("fields") or {}
        h = f.get("content_hash") or p
        if h in seen:
            continue
        seen.add(h)
        fs = P.file_supplier(os.path.basename(str(s.get("source_file") or "")))
        read = f.get("supplier_read") or f.get("supplier")
        if fs and read and not same(fs, read) and f.get("form") != "internal_group_transfer":
            c["supplier: file name differs from the reading"] += 1
            if known_sup(fs, sup):
                c["  the lists know the file-name supplier -> taken"] += 1
                rows = regs.get(str(f.get("order_number") or "")) or []
                if rows:
                    fn_ok = any(same(fs, r.get("supplier")) for r in rows)
                    rd_ok = any(same(read, r.get("supplier")) for r in rows)
                    c["    spreadsheet sides with the file name"] += fn_ok and not rd_ok
                    c["    spreadsheet sides with the reading"] += rd_ok and not fn_ok
                    c["    spreadsheet: both / neither"] += fn_ok == rd_ok
                    if rd_ok and not fn_ok and len(ex) < 12:
                        ex.append(f"{s.get('source_file')} | file {fs} | read {read} | sheet {rows[0].get('supplier')}")
        for fl in s.get("flags") or []:
            m = JOB_RX.match(str(fl))
            if m:
                c["job: file name differs from the form"] += 1
                js = [n(x) for x in m.group(1).split(",") if x.strip()]
                if js and all(j in jobs for j in js):
                    c["  every file-name job is in the Sage job list -> no check"] += 1
    for k, v in c.items():
        print(f"  {k:58} {v}")
    for e in ex:
        print("    reading right:", e)
    fn, rd = c["    spreadsheet sides with the file name"], c["    spreadsheet sides with the reading"]
    rate = fn / (fn + rd) if fn + rd else 1
    ok &= rate >= 0.9
    print(f"VERDICT {'PASS' if ok else 'FAIL'} (spreadsheet sides with the file name {rate:.0%} of {fn + rd} disagreements)")


def fix(apply):
    import sidecar_lock
    import po_reader as P                                   # the installed (503) parser
    sup, jobs = lists()
    stamp = datetime.now().isoformat(timespec="seconds")
    c = Counter()
    for p in glob.glob(str(ROOT / "out/PurchaseOrders/*.json")) + glob.glob(str(ROOT / "out/PurchaseOrders/hold/*.json")):
        js = Path(p)
        try:
            s = json.load(open(js, encoding="utf-8"))
        except Exception:
            continue
        if not any(SUP_RX.match(str(x)) or JOB_RX.match(str(x)) for x in s.get("flags") or []):
            continue
        if (s.get("fields") or {}).get("checked") or Path(str(js) + ".uploaded").exists():
            c["skipped: checked / uploaded"] += 1
            continue
        try:
            with sidecar_lock.locked(js, "filename_trust", wait=5):
                s = json.load(open(js, encoding="utf-8"))
                f, changes, keep = s.setdefault("fields", {}), [], []
                for fl in s.get("flags") or []:
                    m, mj = SUP_RX.match(str(fl)), JOB_RX.match(str(fl))
                    fs = P.file_supplier(os.path.basename(str(s.get("source_file") or ""))) if m else None
                    if m and (not fs or same(fs, f.get("supplier"))):
                        c["false file-name supplier check dropped (KUSASA / PAGE)"] += 1
                        continue
                    if m and known_sup(fs, sup):
                        new = fs.strip()
                        changes.append({"field": "supplier", "old": f.get("supplier"), "new": new})
                        f.setdefault("supplier_read", f.get("supplier"))
                        f["supplier"], f["supplier_name"] = new, new
                        c["supplier taken from the file name"] += 1
                        continue
                    if mj and all(n(x) in jobs for x in mj.group(1).split(",") if x.strip()):
                        c["job check dropped (file-name job in the Sage list)"] += 1
                        continue
                    keep.append(fl)
                if keep == (s.get("flags") or []):
                    continue
                checks = [x for x in keep if str(x).startswith("check: ")]
                f["review"] = "; ".join(str(x)[7:] for x in checks) or None
                f["checks"] = [str(x)[7:] for x in checks]
                held = js.parent.name == "hold"
                keep = [x for x in keep if not str(x).startswith("held: ")]
                if held:
                    reason = f"review checks ({len(checks)})" if checks else "review checks (0)"
                    s["hold_reason"] = reason
                    keep.append("held: " + reason)
                s["flags"], s["needs_review"] = keep, bool(checks)
                s.setdefault("edits", []).append({"at": stamp, "by": "filename_trust (Dino 5 Nov: the file name's supplier / job, "
                                                  "confirmed by MPSBusiness)", "changes": changes or [{"field": "checks", "old": "file name job", "new": None}]})
                c["orders corrected" + ("" if apply else " (dry run)")] += 1
                if apply:
                    tmp = js.with_name(js.name + ".ft.tmp")
                    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                    os.replace(tmp, js)
        except sidecar_lock.Locked:
            c["locked (left)"] += 1
    print(json.dumps(c, indent=1))


if __name__ == "__main__":
    {"test": test, "fix": lambda: fix("--apply" in sys.argv)}[sys.argv[1]]()

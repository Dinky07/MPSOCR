#!/usr/bin/env python3
"""Fill in job numbers on orders already read (before the reader learned to complete them): "6939" -> 6939D, or the
job from the scan's file name ("...-A53308-6939D-...pdf") when none was read. Same rules as po_reader.find_job.

  python3 fix_jobs.py [--root /srv/ocr/scans/pipeline] [--apply] [--synced]

--synced also writes <root>/job_fixes.csv (ContentHash, Name, Old, POJobNumber) for the ones already in SharePoint:
"01-Build-PurchaseOrders.ps1 -Phase JobLetters -Apply" sets those job numbers in the Purchase Orders library.

Default: waiting documents (out/PurchaseOrders, out/PolymerOrders and their hold/), dry run. --synced also lists the
ones already in SharePoint (synced/...): their sidecars are updated with --apply, but SharePoint itself is not - the
list says which library items to correct.
"""
import argparse, glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import po_reader

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--synced", action="store_true")
a = ap.parse_args()
po_reader.JOBS_DIR = a.root
pats = [f"{a.root}/out/{t}/{h}*.json" for t in ("PurchaseOrders", "PolymerOrders") for h in ("", "hold/")]
if a.synced:
    pats += [f"{a.root}/synced/{t}/*/*.json" for t in ("PurchaseOrders", "PolymerOrders")]
n = 0
fixes = []
for p in sorted(set(x for pat in pats for x in glob.glob(pat))):
    try:
        with open(p, encoding="utf-8-sig") as fh:
            side = json.load(fh)
    except (OSError, ValueError):
        continue
    if side.get("doc_type") != "po":
        continue
    f = side.get("fields") or {}
    old = f.get("job_number")
    full = lambda v: set(re.findall(r"\d{3,5}[A-E]", str(v or "").upper().replace(" ", "")))
    d = {"job_number": old, "allocation": f.get("allocation"), "notes": f.get("notes"), "date_iso": f.get("date"),
         "order_number": f.get("order_number"), "polymer_po": f.get("polymer_po"), "other_po_ref": f.get("other_po_ref")}
    base = re.sub(r"^[A-Z]+__", "", os.path.basename(str(side.get("source_file") or side.get("file") or "")).upper())
    chk = []
    job = po_reader.find_job(d, base, chk)
    if not job or job == old:
        continue
    if full(old) and not full(old) < full(job):
        continue        # has its letter(s) already; only add jobs (the file name's second job), never change one
    n += 1
    where = "in SharePoint: job_fixes.csv" if "/synced/" in p else "waiting"
    if "/synced/" in p and "/PurchaseOrders/" in p and f.get("content_hash"):
        fixes.append({"ContentHash": f["content_hash"], "Name": side.get("file") or os.path.basename(p), "Old": old or "",
                      "POJobNumber": job, "Note": "; ".join(chk)})
    print(f"{os.path.relpath(p, a.root)}: {old or '-'} -> {job}   [{where}]  {'; '.join(chk)}")
    if a.apply:
        f["job_number"] = job
        for k in ("job_number_read", "job_customer", "job_name"):
            if d.get(k):
                f[k] = d[k]
        f["checks"] = [c for c in (f.get("checks") or []) if not str(c).startswith(("job ", "job:", "file name names more than one job"))] + chk
        f["review"] = "; ".join(f["checks"]) or None
        side["flags"] = [x for x in (side.get("flags") or []) if not str(x).startswith(("check: job ", "check: job:", "check: file name names more than one job"))] + ["check: " + c for c in chk]
        side.setdefault("edits", []).append({"at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                                             "by": "fix_jobs.py", "changes": [{"field": "job_number", "old": old, "new": job}]})
        with open(p, "w", encoding="utf-8") as fh:        # in place: keeps the file's owner
            json.dump(side, fh, indent=2, ensure_ascii=False)
if a.synced and a.apply:      # the list for SharePoint is only written with --apply (a dry run changes nothing)
    import csv
    out = os.path.join(a.root, "job_fixes.csv")
    keep = {}
    if os.path.exists(out):                     # earlier corrections stay until SharePoint has them (re-runs add to it)
        with open(out, encoding="utf-8-sig") as fh:
            keep = {r["ContentHash"]: r for r in csv.DictReader(fh) if r.get("ContentHash")}
    for r in fixes:
        keep[r["ContentHash"]] = r
    fixes = list(keep.values())
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, ["ContentHash", "Name", "Old", "POJobNumber", "Note"], quoting=csv.QUOTE_ALL)
        w.writeheader(); w.writerows(fixes)
    print(f"{len(fixes)} SharePoint correction(s) in {out}")
print(f"{n} document(s) {'updated' if a.apply else 'to update (dry run: add --apply)'}")

#!/usr/bin/env python3
"""jt_copy_backfill: the JT copy field for the MPS job transfers read before 17 Nov 2026 (runner 752).

For every sidecar in out/JobTransfers (and hold/) and synced/JobTransfers: classify its PDF with jt_copy.classify and
write fields.jt_copy / fields.jt_copy_how, under the sidecar lock (sidecar_lock.py, the same lock as the dashboard
and the PC sync), with an atomic replace. Sidecars that already have jt_copy are left alone. A copy of every sidecar
changed goes to work/jtcopy/backup-<time>/ first.
Writes work/jtcopy/backfill.csv: content_hash, doc_id, file, jt_copy, jt_copy_how (for MPSDocs and the check).
  python3 jt_copy_backfill.py            dry run: counts only
  python3 jt_copy_backfill.py --apply
"""
import csv, json, os, shutil, sys, time
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import jt_copy
import sidecar_lock

R = Path("/srv/ocr/scans/pipeline")
W = R / "work" / "jtcopy"
APPLY = "--apply" in sys.argv


def sidecars():
    for base in (R / "out" / "JobTransfers", R / "synced" / "JobTransfers"):
        for p in sorted(base.rglob("*.json")):
            if p.name.endswith((".tmp", ".lock")) or "_meta" in p.parts:
                continue
            yield p


def main():
    W.mkdir(parents=True, exist_ok=True)
    bak = W / time.strftime("backup-%Y%m%d-%H%M%S")
    rows, n_done, n_have, n_nopdf = [], 0, 0, 0
    for sc in sidecars():
        try:
            d = json.load(open(sc, encoding="utf-8"))
        except Exception:
            continue
        f = d.get("fields") or {}
        pdf = sc.with_name(d.get("file") or (sc.stem + ".pdf"))
        if not pdf.exists():
            pdf = sc.with_suffix(".pdf")
        if f.get("jt_copy"):
            n_have += 1
            rows.append([f.get("content_hash"), d.get("doc_id"), str(sc.relative_to(R)), f["jt_copy"], f.get("jt_copy_how")])
            continue
        if not pdf.exists():
            n_nopdf += 1
            continue
        c = jt_copy.classify(pdf)
        rows.append([f.get("content_hash"), d.get("doc_id"), str(sc.relative_to(R)), c["jt_copy"], c["jt_copy_how"]])
        if APPLY:
            with sidecar_lock.locked(str(sc), who="jt_copy_backfill"):
                d = json.load(open(sc, encoding="utf-8"))
                dst = bak / sc.relative_to(R)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sc, dst)
                d.setdefault("fields", {}).update(c)
                tmp = sc.with_name(sc.name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(d, fh, ensure_ascii=False, indent=2)
                    fh.flush(); os.fsync(fh.fileno())
                os.replace(tmp, sc)
        n_done += 1
    with open(W / "backfill.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["content_hash", "doc_id", "sidecar", "jt_copy", "jt_copy_how"])
        w.writerows(rows)
    from collections import Counter
    print(("written" if APPLY else "would write"), n_done, "sidecars; already had it", n_have, "; no PDF beside", n_nopdf)
    print("copies:", dict(Counter(r[3] for r in rows)))
    if APPLY:
        print("backups in", bak)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Send documents that are waiting in out/<Type> back through the worker (e.g. after the reader changed).

For each sidecar in out/<Type> that has NOT been synced yet:
  - the original scan is moved from done/<Type>/<yyyy-mm>/ back to in/<Type>/
  - its 'processed' row is removed from state.db (otherwise the worker skips it as a duplicate)
  - the old PDF + JSON are moved to out/<Type>/_replaced/<timestamp>/ (kept, not deleted)
Dry run by default; add --apply.   Run on the box:  python3 reprocess.py PurchaseOrders [--apply] [names...]
"""
import argparse, hashlib, json, shutil, sqlite3, sys
from datetime import datetime
from pathlib import Path

def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


ap = argparse.ArgumentParser()
ap.add_argument("type", help="e.g. PurchaseOrders")
ap.add_argument("names", nargs="*", help="only these out/ files (e.g. POA85994.json); default all waiting")
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--db", default="/srv/ocr/pipeline/state.db")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

root = Path(a.root)
out, done, inq = root / "out" / a.type, root / "done" / a.type, root / "in" / a.type
keep = out / "_replaced" / datetime.now().strftime("%Y%m%d-%H%M%S")
db = sqlite3.connect(a.db, timeout=30)
jsons = [out / n if n.endswith(".json") else out / (Path(n).stem + ".json") for n in a.names] if a.names else sorted(out.glob("*.json"))
n = 0
for j in jsons:
    if not j.exists():
        print(f"skip {j.name}: not in out/{a.type}"); continue
    if Path(str(j) + ".uploaded").exists():
        print(f"skip {j.name}: already uploaded to SharePoint (sync it or remove it first)"); continue
    sc = json.loads(j.read_text(encoding="utf-8"))
    qtype = sc.get("queued_as") or a.type                  # PL orders / proofs of payment were queued as PurchaseOrders
    done, inq = root / "done" / qtype, root / "in" / qtype
    names = {sc["source_file"]}
    srcs = sorted((p for p in done.glob("*/*") if p.is_file() and (p.name in names or p.name.startswith(Path(sc["source_file"]).stem))
                   and _sha(p) == sc.get("source_sha256")), key=lambda p: p.stat().st_mtime, reverse=True)
    if not srcs:
        print(f"skip {j.name}: original {sc['source_file']} not found under done/{a.type}"); continue
    src = srcs[0]
    pdf = out / sc["file"]
    print(f"{j.name}: {src.relative_to(root)} -> in/{qtype}/   (was read by {sc.get('extractor')} / {sc.get('model')})")
    if not a.apply:
        continue
    keep.mkdir(parents=True, exist_ok=True)
    for p in (pdf, j, Path(str(j) + ".attempts")):
        if p.exists():
            shutil.move(str(p), str(keep / p.name))
    db.execute("DELETE FROM processed WHERE sha256=?", (sc["source_sha256"],))
    db.commit()
    inq.mkdir(parents=True, exist_ok=True)
    if sc.get("source_folder") or src.name != sc["source_file"]:
        (inq / "_meta").mkdir(parents=True, exist_ok=True)       # keep the original folder and name
        (inq / "_meta" / (src.name + ".json")).write_text(json.dumps({"source_folder": sc.get("source_folder"),
                                                                     "original_name": sc["source_file"], "queued_from": "reprocess"}), encoding="utf-8")
    shutil.move(str(src), str(inq / src.name))
    n += 1
print(f"{n} sent back to in/{a.type}" if a.apply else "dry run - add --apply to do it")

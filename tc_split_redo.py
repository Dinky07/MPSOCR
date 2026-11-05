#!/usr/bin/env python3
"""506 (Dino 5 Nov): a batch the splitter cut wrongly is cut again - its parts leave the queue (to deleted/), and the
original scan (deleted/TestCerts/<yyyy-mm>/) is fed again with the page readings kept in work/tc_split/ (no second read).
    tc_split_redo.py <name stem, e.g. DOC031126-03112026170647>"""
import glob, json, os, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402
import sidecar_lock                                     # noqa: E402

R = Path("/srv/ocr/scans/pipeline")
stem = sys.argv[1]
kept = R / "work" / "tc_split" / f"{stem}.json"
sc0 = json.load(open(kept, encoding="utf-8"))
name = Path(str(sc0.get("source_file") or sc0["file"])).name
orig = [Path(p) for p in glob.glob(str(R / "deleted" / "TestCerts" / "*" / glob.escape(sc0["file"])))]
orig = [p for p in orig if p.stat().st_size and __import__("hashlib").sha256(p.read_bytes()).hexdigest() == sc0["source_sha256"]]
if not orig:
    sys.exit(f"original scan of {stem} not found in deleted/TestCerts")
parts = [Path(p) for p in glob.glob(str(R / "out/TestCerts" / f"{glob.escape(stem)} *.json")) + glob.glob(str(R / "out/TestCerts/hold" / f"{glob.escape(stem)} *.json"))]
stamp = datetime.now().isoformat(timespec="seconds")
for js in parts:
    with sidecar_lock.locked(js, "tc_split_redo", wait=15):
        sc = json.load(open(js, encoding="utf-8"))
        if (sc.get("fields") or {}).get("checked") or Path(str(js) + ".uploaded").exists():
            sys.exit(f"{js.name} is checked or uploaded: not redone")
        route_sort.remove(js, sc, "TestCerts", "batch cut again (Dino 5 Nov: order number read as the cert number)", stamp)
IN, md = R / "in" / "TestCerts", R / "in" / "TestCerts" / "_meta"
note = {"original_name": name, "reread_of": str(kept.relative_to(R)), "reread_at": stamp, "why": "batch cut again (Dino 5 Nov)",
        "reuse_tc_pages": str(kept.relative_to(R))}
for k in ("source_folder", "source_modified"):
    if sc0.get(k):
        note[k] = sc0[k]
(md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
shutil.copy2(orig[0], IN / (name + ".part"))
os.replace(IN / (name + ".part"), IN / name)
print(json.dumps({"parts_removed": [p.name for p in parts], "fed": f"in/TestCerts/{name}"}))

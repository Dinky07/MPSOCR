#!/usr/bin/env python3
"""500 (Dino 5 Nov): today's scanned batch, read as one test cert, goes back to the worker to be split by test
certificate number - with its page readings reused (no second read). Its sidecar is kept in work/tc_split/ (the
reading the worker takes), the one-document copy goes to deleted/TestCerts/<yyyy-mm>/.
    tc_split_refeed.py <name stem, e.g. DOC051126-05112026094800>"""
import glob, json, os, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402
import sidecar_lock                                     # noqa: E402

R = Path("/srv/ocr/scans/pipeline")
stem = sys.argv[1]
hits = [Path(p) for p in glob.glob(str(R / "out/TestCerts" / f"{glob.escape(stem)}*.json")) + glob.glob(str(R / "out/TestCerts/hold" / f"{glob.escape(stem)}*.json"))]
if len(hits) != 1:
    sys.exit(f"expected one sidecar for {stem}, found {[str(h) for h in hits]}")
js = hits[0]
stamp = datetime.now().isoformat(timespec="seconds")
keep = R / "work" / "tc_split" / js.name
keep.parent.mkdir(parents=True, exist_ok=True)
IN, md = R / "in" / "TestCerts", R / "in" / "TestCerts" / "_meta"
md.mkdir(parents=True, exist_ok=True)
with sidecar_lock.locked(js, "tc_split_refeed", wait=15):
    sc = json.load(open(js, encoding="utf-8"))
    pdf = js.parent / str(sc["file"])
    name = Path(str(sc.get("source_file") or pdf.name)).name
    shutil.copy2(js, keep)
    note = {"original_name": name, "reread_of": str(js.relative_to(R)), "reread_at": stamp,
            "why": "split by test certificate number (Dino 5 Nov)", "reuse_tc_pages": str(keep.relative_to(R))}
    for k in ("source_folder", "source_modified"):
        if sc.get(k):
            note[k] = sc[k]
    (md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
    shutil.copy2(pdf, IN / (name + ".part"))
    route_sort.remove(js, sc, "TestCerts", f"split by test certificate number (Dino 5 Nov); fed again as in/TestCerts/{name}", stamp)
    os.replace(IN / (name + ".part"), IN / name)
print(json.dumps({"fed": f"in/TestCerts/{name}", "reading_kept": str(keep.relative_to(R)), "pages": len(sc.get("tc_pages") or [])}))

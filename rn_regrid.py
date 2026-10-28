#!/usr/bin/env python3
"""Re-read the tick boxes of release notes whose inspections came from the whole-page read (rn_reader v4 finds the
strip when the hole punch eats the 'I' of INSPECTION). Sidecars in out/ReleaseNotes only; takes the sidecar lock.
    python3 rn_regrid.py [--apply]"""
import glob, json, os, sys
from pathlib import Path
sys.path.insert(0, "/srv/ocr/pipeline")
import rn_reader as R, sidecar_lock
apply = "--apply" in sys.argv
root = Path("/srv/ocr/scans/pipeline/out/ReleaseNotes")
cfg = json.load(open("/srv/ocr/pipeline/config.json"))
c = dict(R.DEFAULTS, **((cfg["doc_types"].get("ReleaseNotes") or {}).get("rn_reader") or {}))
for js in sorted(root.glob("*.json")):
    sc = json.load(open(js, encoding="utf-8"))
    f = sc.get("fields") or {}
    if f.get("inspections_from") != "whole page":
        continue
    pdf = js.with_suffix(".pdf")
    W, H, ws = R.page_words(pdf, 1)
    g = R.read_grid(pdf, 1, c, ws, H) if ws else None
    marks = (g or {}).get("marks") or {}
    print(js.name, "strip names:", len(marks), "ticked:", [k for k, v in marks.items() if v])
    if len(marks) < 10 or not apply:
        continue
    with sidecar_lock.locked(js, "rn_regrid"):
        sc = json.load(open(js, encoding="utf-8")); f = sc["fields"]
        old = f.get("inspections_text")
        insp = [R.norm_inspection(n) for n in R.INSPECTIONS if marks.get(n)]
        f.update(inspections=insp, inspections_text="; ".join(insp) or None, inspections_from="tick-box strip",
                 inspections_strip_text=(g.get("raw") or "")[:600])
        sc["flags"] = [x for x in sc.get("flags") or [] if not str(x).startswith("check: inspections read from the whole page")]
        f["review"] = "; ".join(x for x in sc["flags"]) or None
        sc["needs_review"] = bool(sc["flags"])
        sc.setdefault("edits", []).append({"at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                                           "by": "rn_regrid (tick-box strip)", "changes": [{"field": "inspections_text", "old": old, "new": f["inspections_text"]}]})
        tmp = js.with_name(js.name + ".rg.tmp"); tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8"); os.replace(tmp, js)
        print("  ->", f["inspections_text"])

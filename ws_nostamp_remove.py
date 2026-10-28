#!/usr/bin/env python3
"""Dino 28 Oct 18:17: delete the workshop sheets in the queues that have no stamp. Uses the stamp pass (ws_stamp_pass.py
dry run 28 Oct 17:34, qwen3-vl): status no_stamp. Left alone: sheets whose page reading said stamped (the two readings
disagree: for Dino), checked or edited sheets, uploaded ones, and sheets not in the queue / hold any more. Each removed
sheet goes to deleted/WorkshopSheets/<yyyy-mm>/ with a reason file (recoverable).

    python3 ws_nostamp_remove.py [--csv <stamp pass csv>] [--apply]
"""
import argparse, csv, glob, json, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402  remove()
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
T = "WorkshopSheets"
ap = argparse.ArgumentParser()
ap.add_argument("--csv", default="")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
src = a.csv or sorted(glob.glob(str(ROOT / "reports" / "ws stamp pass * dry run.csv")))[-1]
rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
stamp = datetime.now().isoformat(timespec="seconds")
cnt, out = Counter(), []
for r in rows:
    if r["status"] != "no_stamp":
        continue
    js = ROOT / "out" / T / ("hold" if r["where"] == "hold" else "") / r["file"]
    res = None
    if str(r.get("page_stamped")) == "True":
        res = "kept: the page reading said stamped (check)"
    elif not js.exists():
        res = "gone meanwhile"
    elif Path(str(js) + ".uploaded").exists():
        res = "kept: already uploaded"
    else:
        try:
            with sidecar_lock.locked(js, "ws_nostamp_remove"):
                sc = json.load(open(js, encoding="utf-8"))
                f = sc.get("fields") or {}
                if f.get("checked"):
                    res = "kept: checked"
                elif sc.get("edits"):
                    res = "kept: edited on the dashboard"
                elif a.apply:
                    route_sort.remove(js, sc, T, "no stamp (stamp pass 28 Oct 17:34: " + (r.get("why") or "no stamp")
                                      + "); Dino 28 Oct 18:17: delete workshop sheets without a stamp", stamp)
                    res = "deleted"
                else:
                    res = "would delete"
        except sidecar_lock.Locked:
            res = "locked (not done)"
    cnt[f"{r['where']}: {res}"] += 1
    out.append(dict(r, result=res))
p = ROOT / "reports" / f"ws no stamp removed {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}.csv"
with open(p, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0].keys()) if out else ["file"])
    w.writeheader()
    w.writerows(out)
print(json.dumps({"stamp_pass": Path(src).name, "applied": a.apply, "no_stamp_rows": len(out), "results": dict(cnt.most_common()),
                  "report": str(p)}, indent=1))

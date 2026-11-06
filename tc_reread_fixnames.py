#!/usr/bin/env python3
"""529: the 5 Nov 23:22 re-read of the Test Certs hold named each split part after its combined file and dropped its
link to the combined SharePoint item. Each such reading (in out/TestCerts or its hold) gets its part name back (the
hold copy's) and fields.replaces from the hold copy in deleted/TestCerts. Not touched: synced, checked or edited ones,
or one whose old name is taken.
    tc_reread_fixnames.py [--apply]"""
import csv, json, os, sys
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                          # noqa: E402

R = Path("/srv/ocr/scans/pipeline")
OUT, HOLD, DEL = R / "out/TestCerts", R / "out/TestCerts/hold", R / "deleted/TestCerts/2026-11"
apply = "--apply" in sys.argv
rows = list(csv.DictReader(open(R / "reports/tc reread 2026-11-05 2322.csv", encoding="utf-8-sig")))
cnt = {}
for r in rows:
    old, res = r["document"][:-5], r["result"]
    if not res.startswith("in/"):
        continue
    new = Path(res).stem
    if new == old:
        continue
    old_sc = DEL / (old + ".json")
    try:
        rep = (json.load(open(old_sc, encoding="utf-8")).get("fields") or {}).get("replaces")
    except (OSError, ValueError):
        rep = None
    d = OUT if (OUT / (new + ".json")).exists() else HOLD if (HOLD / (new + ".json")).exists() else None
    why = ("not split (no replaces)" if not rep else "not in out/ or hold" if not d else
           "old name taken" if (OUT / (old + ".json")).exists() or (HOLD / (old + ".json")).exists() else None)
    js = d / (new + ".json") if d else None
    if not why:
        sc = json.load(open(js, encoding="utf-8"))
        f = sc.get("fields") or {}
        why = ("uploaded" if Path(str(js) + ".uploaded").exists() else "checked" if f.get("checked") else
               "edited by a person" if [e for e in sc.get("edits") or [] if "by" not in e] else
               "PDF missing" if not (d / str(sc.get("file") or "")).is_file() else None)
    if why:
        cnt[why] = cnt.get(why, 0) + 1
        print(f"  skip {new}: {why}")
        continue
    where = "out" if d == OUT else "hold"
    cnt[f"fixed ({where})"] = cnt.get(f"fixed ({where})", 0) + 1
    print(f"  {where}: {new} -> {old}")
    if not apply:
        continue
    with sidecar_lock.locked(js, "tc_reread_fixnames", wait=10):
        sc = json.load(open(js, encoding="utf-8"))
        pdf = d / str(sc["file"])
        sc["file"] = old + ".pdf"
        sc.setdefault("fields", {})["replaces"] = rep
        sc["name_fixed"] = f"529, 6 Nov: was {new} (re-read under the combined file's name); replaces restored"
        tmp = d / (old + ".json.part")
        tmp.write_text(json.dumps(sc, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(pdf, d / (old + ".pdf"))
        os.replace(tmp, d / (old + ".json"))
        js.unlink()
print(json.dumps({"applied": apply, **cnt}))

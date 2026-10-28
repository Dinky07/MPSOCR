#!/usr/bin/env python3
"""Apply the old-site heat check (tc_reader.check_old_heats) to test certificates already read: out/TestCerts and its
hold folder, skipping documents that are checked, edited, uploaded or already carry heat_check. The model's own reading
is kept as fields_read (training). Prints the heat score of the model against the old site, before and after.

    python3 reapply_tc_heats.py [--apply]
"""
import argparse, copy, glob, json, os, sys
sys.path.insert(0, "/srv/ocr/pipeline")
import tc_reader as T

ap = argparse.ArgumentParser(); ap.add_argument("--root", default="/srv/ocr/scans/pipeline"); ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
tot = dict(docs=0, skipped=0, old_site=0, exact=0, corrected=0, not_read=0, not_on_old=0, all_exact=0, all_after=0)
for js in sorted(glob.glob(f"{a.root}/out/TestCerts/*.json") + glob.glob(f"{a.root}/out/TestCerts/hold/*.json")):
    side = json.load(open(js, encoding="utf-8"))
    f = side.get("fields") or {}
    if f.get("checked") or side.get("edits") or os.path.exists(js + ".uploaded") or "heat_check" in f or not T.old_heats(f.get("old_columns")):
        tot["skipped"] += 1
        continue
    before = copy.deepcopy(f)
    flags = list(side.get("flags") or [])
    T.check_old_heats(f, flags)
    hc = f["heat_check"]
    tot["docs"] += 1; tot["old_site"] += hc["old_site"]; tot["exact"] += hc["exact"]; tot["corrected"] += hc["corrected"]
    tot["not_read"] += len(hc["not_read"]); tot["not_on_old"] += len(hc["not_on_old_site"])
    tot["all_exact"] += hc["exact"] == hc["old_site"]; tot["all_after"] += hc["exact"] + hc["corrected"] == hc["old_site"]
    if a.apply:
        if hc["corrected"]:
            side.setdefault("fields_read", before)
        side["flags"], side["needs_review"] = flags, bool(flags)
        side.setdefault("rule_fixes", []).append({"rule": "old-site heat numbers", "corrected": hc["corrected"]})
        tmp = js + ".tmp"
        json.dump(side, open(tmp, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        os.replace(tmp, js)
o = max(1, tot["old_site"])
print(("APPLIED" if a.apply else "DRY RUN") + f": {tot['docs']} certificates ({tot['skipped']} skipped)")
print(f"  old-site heats {tot['old_site']}: read exactly {tot['exact']} ({tot['exact'] / o:.1%}), corrected {tot['corrected']}, "
      f"right after the rule {tot['exact'] + tot['corrected']} ({(tot['exact'] + tot['corrected']) / o:.1%}); not read {tot['not_read']}; "
      f"read but not on the old site {tot['not_on_old']}")
print(f"  certificates with every old-site heat right: {tot['all_exact']} as read, {tot['all_after']} after the rule")

#!/usr/bin/env python3
"""Review F14 (Dino 28 Oct): test certificates held only for "review checks (N)" go back to the queue; their checks stay
in fields.review (the Check column). Certs held for a structural reason (no cert page, no items / heats, family Other)
stay. DRY RUN unless --apply. Reports what it did per cert.

    python3 tc_release_held.py [--apply]
"""
import argparse, glob, json, os, shutil, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                   # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
q, hold = ROOT / "out" / "TestCerts", ROOT / "out" / "TestCerts" / "hold"
c = Counter()
for js in sorted(glob.glob(str(hold / "*.json"))):
    js = Path(js)
    try:
        sc = json.load(open(js, encoding="utf-8"))
    except Exception:
        c["unreadable"] += 1
        continue
    reason = str(sc.get("hold_reason") or "")
    f = sc.get("fields") or {}
    structural = (not reason.startswith("review checks") or "not_a_test_cert" in (sc.get("flags") or []) or not f.get("item_count")
                  or f.get("family") in (None, "", "Other") or ("no_heats" in (sc.get("flags") or []) and not f.get("heat_count")))
    if structural:
        c["stays: " + (reason[:40] or "?")] += 1
        continue
    pdf = hold / str(sc.get("file") or "")
    if not pdf.is_file() or (q / pdf.name).exists() or (q / js.name).exists():
        c["stays: name clash or no PDF"] += 1
        continue
    c["released"] += 1
    if not a.apply:
        continue
    with sidecar_lock.locked(js, "tc_release"):
        sc.pop("hold_reason", None)
        sc["flags"] = [x for x in (sc.get("flags") or []) if not str(x).startswith("held: ")]
        sc["needs_review"] = bool(sc["flags"])
        sc.setdefault("edits", []).append({"at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                                           "by": "tc_release_held (review F14)", "changes": [{"field": "hold_reason", "old": reason, "new": None}]})
        tmp = js.with_name(js.name + ".tr.tmp")
        tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, js)
        shutil.move(str(pdf), str(q / pdf.name))          # PDF first, JSON last: the PC never sees half a pair
        for m in (".uploaded", ".attempts"):
            if (hold / (js.name + m)).exists():
                shutil.move(str(hold / (js.name + m)), str(q / (js.name + m)))
        shutil.move(str(js), str(q / js.name))
print(json.dumps({"applied": a.apply, **c}, indent=1))

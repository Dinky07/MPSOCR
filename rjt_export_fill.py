#!/usr/bin/env python3
"""rjt_export_fill.py [--apply] (817, Dino 23 Nov: "take the lines from the Ronin export by JT number").
Ronin job transfers still in the queue (out/RoninJobTransfers, hold; not checked, not filled before): when RoninStock has
captured the JT (work/registers/ronin-jt-export.psv, sales order = DF number) with the same number of lines and the same
quantities in the same order, each line's stock code is taken from the capture (the reader gets 27 % of codes right,
quantities 97 %). The reader's own lines are kept in fields_read. Where lines or quantities differ nothing is changed
and one check flag says so. Report: reports/rjt export fill <stamp>.json"""
import csv, glob, json, os, re, sys
from datetime import datetime
sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock
R = "/srv/ocr/scans/pipeline"; apply = "--apply" in sys.argv; stamp = datetime.now().isoformat(timespec="seconds")
nk = lambda v: re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
def num(v):
    try: return round(float(str(v).replace(",", "").strip()), 3)
    except (TypeError, ValueError): return None
cap = {}
with open(f"{R}/work/registers/ronin-jt-export.psv", encoding="utf-8", errors="replace") as fh:
    for r in csv.reader(fh, delimiter="|"):
        if len(r) < 14 or r[0] == "AutoIndex" or not r[2].upper().startswith("DF"): continue
        cap.setdefault(r[2].upper().replace(" ", ""), []).append((int(r[10] or 0), r[11].strip(), num(r[12]), r[13].strip()))
tot = {"looked_at": 0, "not_captured": 0, "filled": 0, "codes_changed": 0, "already_same": 0, "differs": 0, "locked": 0}
FLAG = "check: lines differ from the RoninStock capture"
for p in sorted(glob.glob(f"{R}/out/RoninJobTransfers/*.json") + glob.glob(f"{R}/out/RoninJobTransfers/hold/*.json")):
    try: sc = json.load(open(p, encoding="utf-8"))
    except Exception: continue
    f = sc.get("fields") or {}
    if f.get("checked") or sc.get("export_fill"): continue
    tot["looked_at"] += 1
    lines = sorted(cap.get(nk(f.get("jt_number")), []))
    if not lines: tot["not_captured"] += 1; continue
    items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    same = len(items) == len(lines) and all(num(i.get("quantity")) == l[2] for i, l in zip(items, lines))
    changed = sum(1 for i, l in zip(items, lines) if nk(i.get("item_no")) != nk(l[1])) if same else 0
    tot["filled" if same else "differs"] += 1; tot["codes_changed"] += changed; tot["already_same"] += int(same and not changed)
    if not apply: continue
    try:
        with sidecar_lock.locked(p, "rjt_export_fill"):
            sc = json.load(open(p, encoding="utf-8")); f = sc.setdefault("fields", {})
            if f.get("checked") or sc.get("export_fill"): continue
            if same:
                if changed and "fields_read" not in sc: sc["fields_read"] = json.loads(json.dumps(f))
                ch = []
                for n, (i, l) in enumerate(zip([i for i in f.get("items") if isinstance(i, dict)], lines), 1):
                    if nk(i.get("item_no")) != nk(l[1]):
                        ch.append({"field": f"items[{n}].item_no", "old": i.get("item_no"), "new": l[1]}); i["item_no"] = l[1]
                f["item_codes"] = "; ".join(str(i.get("item_no")) for i in f.get("items") if isinstance(i, dict) and i.get("item_no")) or None
                sc["export_fill"] = {"at": stamp, "lines": len(lines), "codes_changed": len(ch), "source": "RoninStock export (sales order = DF number)"}
                if ch: sc.setdefault("edits", []).append({"at": stamp, "by": "rjt_export_fill (stock codes from the RoninStock capture)", "changes": ch})
            else:
                note = f"{FLAG} ({len(lines)} captured, {len(items)} read, or a quantity differs)"
                sc["export_fill"] = {"at": stamp, "lines": len(lines), "differs": True}
                if not any(str(x).startswith(FLAG) for x in (sc.get("flags") or [])):
                    sc.setdefault("flags", []).append(note); sc["needs_review"] = True
                    f["checks"] = list(f.get("checks") or []) + [note[7:]] if isinstance(f.get("checks"), list) or f.get("checks") is None else f.get("checks")
            tmp = p + ".rjt.tmp"; open(tmp, "w", encoding="utf-8").write(json.dumps(sc, indent=2, ensure_ascii=False, default=str)); os.replace(tmp, p)
    except sidecar_lock.Locked:
        tot["locked"] += 1
tot["applied"] = apply
json.dump(tot, open(f"{R}/reports/rjt export fill {datetime.now():%Y-%m-%d %H%M}{'' if apply else ' dry run'}.json", "w"), indent=1)
print(tot)

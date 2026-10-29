#!/usr/bin/env python3
"""Apply the supplier rules (supplier_rules.json, Dino 29 Oct: SMS and every "SMS F..." reading is "SMS Flanges") to the
purchase / Polymer orders already read: queue, hold and synced.

    python3 supplier_rule_apply.py [--apply]

Each changed sidecar: fields.supplier = the rule's name (the old reading kept in supplier_read), supplier_rule set, an
edit record "supplier rule (...)", and the review checks the rule answers removed ("file name says supplier SMS FLANGES",
"supplier read as ..., matched to ..."). Queue / hold orders not uploaded yet are also renamed, so the SharePoint file
name carries the right supplier ("SMS FURNESS-A80862-..." -> "SMS FLANGES-A80862-..."). Synced orders keep their file
name; their SharePoint Supplier column needs Sync-OcrOutput.ps1 -BackfillFields. Takes the sidecar lock.
Report: reports/supplier rule <stamp>[ dry run].csv
"""
import argparse, csv, glob, json, os, re, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import po_reader                                        # noqa: E402  supplier_rule()
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
key = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
stamp = datetime.now().isoformat(timespec="seconds")
rows, cnt = [], Counter()


def free_name(folder, stem, ext):
    p, k = folder / f"{stem}{ext}", 2
    while p.exists() or p.with_suffix(".json").exists():
        p = folder / f"{stem}_{k}{ext}"
        k += 1
    return p


for t in ("PurchaseOrders", "PolymerOrders"):
    for pat, where in (("out/{t}/*.json", "queue"), ("out/{t}/hold/*.json", "hold"), ("synced/{t}/*/*.json", "synced")):
        for js in sorted(glob.glob(str(ROOT / pat.format(t=t)))):
            js = Path(js)
            if js.name.endswith((".tmp", ".lock")):
                continue
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            f = sc.get("fields") or {}
            old = f.get("supplier")
            rule = po_reader.supplier_rule(old) if old else None
            if not rule:
                continue
            name, why = rule
            renamed = ""
            if old == name:
                continue                                 # already the rule's name
            res = "would change" if not a.apply else "changed"
            if a.apply:
                try:
                    with sidecar_lock.locked(js, "supplier_rule_apply"):
                        sc = json.load(open(js, encoding="utf-8"))
                        f = sc.get("fields") or {}
                        old = f.get("supplier")
                        if old != name:
                            f.setdefault("supplier_read", old)
                            f["supplier"] = name
                        f["supplier_rule"] = why
                        kn = key(name)
                        drop = lambda c: (bool(re.match(r"(check: )?file name says supplier (.+)$", str(c)))
                                          and key(re.sub(r"^(check: )?file name says supplier ", "", str(c))) in (kn,)) \
                            or bool(re.match(r"(check: )?supplier read as .+, matched to ", str(c)))
                        f["checks"] = [c for c in (f.get("checks") or []) if not drop(c)]
                        f["review"] = "; ".join(f["checks"]) or None
                        sc["flags"] = [x for x in (sc.get("flags") or []) if not drop(x)]
                        sc["needs_review"] = bool(sc["flags"])
                        sc["fields"] = f
                        sc.setdefault("edits", []).append({"at": stamp, "by": f"supplier rule ({name})",
                                                           "changes": [{"field": "supplier", "old": old, "new": name}]})
                        # not uploaded yet: rename so the library file name carries the right supplier
                        pdf = js.parent / str(sc.get("file") or "")
                        if where != "synced" and pdf.is_file() and not Path(str(js) + ".uploaded").exists():
                            m = re.match(r"^(.*?)(-(?:A\d{5}|PL\d{4}|\d{5}).*)$", pdf.stem)
                            if m and m.group(1) != name and po_reader.supplier_rule(m.group(1)):
                                newpdf = free_name(pdf.parent, name + m.group(2), pdf.suffix)
                                os.replace(pdf, newpdf)
                                sc["file"] = newpdf.name
                                renamed = newpdf.name
                        tmp = js.with_name(js.name + ".sr.tmp")
                        tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                        os.replace(tmp, js)
                        if renamed:
                            newjs = js.with_name(Path(renamed).stem + ".json")
                            os.replace(js, newjs)
                            att = Path(str(js) + ".attempts")
                            if att.exists():
                                os.replace(att, Path(str(newjs) + ".attempts"))
                except sidecar_lock.Locked:
                    res = "locked (next run)"
            cnt[f"{t} {where}: {res}{' + renamed' if renamed else ''}"] += 1
            rows.append({"type": t, "where": where, "file": js.name, "order_number": f.get("order_number"), "old": old,
                         "new": name, "checked": bool(f.get("checked")), "renamed_to": renamed, "result": res})

out = ROOT / "reports" / f"supplier rule {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}.csv"
with open(out, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
print(json.dumps({"applied": a.apply, "results": dict(cnt.most_common()),
                  "readings": dict(Counter(r["old"] for r in rows).most_common(30)), "report": str(out)}, indent=1))

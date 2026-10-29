#!/usr/bin/env python3
"""Ronin orders out of the Purchase Orders queues (Dino 29 Oct 05:26): every Ronin Flanges & Fittings order (DF####, or
read as a Ronin order form) in out/PurchaseOrders (queue and hold) goes to the new RoninOrders type.

  - not checked and not edited by a person: read again, so the new Ronin rules apply (Ronin is the buyer, not the
    supplier; supplier from the order's folder; SMS = SMS Flanges): the PDF is fed to in/RoninOrders with a _meta note
    ("reread_of", original name and folder) and the old copy goes to deleted/PurchaseOrders/<yyyy-mm>/ (recoverable)
  - checked or edited by a person: moved as it is to out/RoninOrders/ (hold when it has no DF number)
Left alone: anything already uploaded to the Purchase Orders library (listed in the report).

    python3 ronin_move.py [--apply]
Report: reports/ronin move <stamp>[ dry run].csv
"""
import argparse, csv, glob, json, os, re, shutil, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402  remove()
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
IN = ROOT / "in" / "RoninOrders"
SCRIPT_EDIT = re.compile(r"^(po_autocheck|route_sort|hold_resort|od_sort|requeue_po12|numbers_pass|fix_|supplier rule)", re.I)
ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
stamp = datetime.now().isoformat(timespec="seconds")
rows, cnt = [], Counter()
IN.mkdir(parents=True, exist_ok=True)
(IN / "_meta").mkdir(exist_ok=True)
(ROOT / "out" / "RoninOrders" / "hold").mkdir(parents=True, exist_ok=True)

for js in sorted(glob.glob(str(ROOT / "out" / "PurchaseOrders" / "*.json")) + glob.glob(str(ROOT / "out" / "PurchaseOrders" / "hold" / "*.json"))):
    js = Path(js)
    if js.name.endswith((".tmp", ".lock")):
        continue
    try:
        sc = json.load(open(js, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    f = sc.get("fields") or {}
    on = str(f.get("order_number") or "").upper()
    if not (re.fullmatch(r"DF\d{4}", on) or f.get("form") == "ronin_order"):
        continue
    where = "hold" if js.parent.name == "hold" else "queue"
    pdf = js.parent / str(sc.get("file") or "")
    human = [e for e in (sc.get("edits") or []) if not SCRIPT_EDIT.match(str(e.get("by") or ""))]
    if Path(str(js) + ".uploaded").exists():
        res = "left: already uploaded to Purchase Orders"
    elif not pdf.is_file():
        res = "left: PDF missing"
    elif f.get("checked") or human:
        res = "move as it is (checked / edited)"
    else:
        res = "re-read in RoninOrders"
    if a.apply and res.startswith(("move", "re-read")):
        try:
            with sidecar_lock.locked(js, "ronin_move"):
                sc = json.load(open(js, encoding="utf-8"))
                if res.startswith("move"):
                    dest = ROOT / "out" / "RoninOrders" / ("" if re.fullmatch(r"DF\d{4}", on) else "hold")
                    newpdf, k = dest / pdf.name, 2
                    while newpdf.exists() or newpdf.with_suffix(".json").exists():
                        newpdf = dest / f"{pdf.stem}_{k}{pdf.suffix}"
                        k += 1
                    sc["file"], sc["folder"] = newpdf.name, "RoninOrders"
                    sc["queued_as"] = sc.get("queued_as") or "PurchaseOrders"
                    if dest.name != "hold":
                        sc.pop("hold_reason", None)
                        sc["flags"] = [x for x in (sc.get("flags") or []) if not str(x).startswith("held: ")]
                    sc.setdefault("edits", []).append({"at": stamp, "by": "ronin_move (Dino 29 Oct: Ronin orders to their own queue)",
                                                       "changes": [{"field": "folder", "old": "PurchaseOrders", "new": "RoninOrders"}]})
                    shutil.copy2(pdf, newpdf)
                    tmp = newpdf.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                    os.replace(tmp, newpdf.with_suffix(".json"))
                    pdf.unlink()
                    js.unlink()
                    res = f"moved to out/RoninOrders/{'hold/' if dest.name == 'hold' else ''}{newpdf.name}"
                else:
                    stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(sc.get("source_file") or pdf.name)).stem)[:100]
                    page = sc.get("source_page")
                    name, k = f"{stem}{f'_p{page}' if page else ''}.pdf", 2
                    while (IN / name).exists() or (IN / "_meta" / (name + ".json")).exists():
                        name = f"{stem}{f'_p{page}' if page else ''}_{k}.pdf"
                        k += 1
                    note = {"original_name": sc.get("source_file") or pdf.name, "reread_of": f"out/PurchaseOrders/{where}/{pdf.name}",
                            "reread_at": stamp, "previous_doc_id": sc.get("doc_id"), "why": "Ronin order: own queue (Dino 29 Oct)"}
                    for k2 in ("source_folder", "source_modified", "source_page"):
                        if sc.get(k2):
                            note[k2] = sc[k2]
                    (IN / "_meta" / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
                    shutil.copy2(pdf, IN / (name + ".part"))
                    route_sort.remove(js, sc, "PurchaseOrders", f"Ronin order: read again in RoninOrders as in/RoninOrders/{name} (Dino 29 Oct)", stamp)
                    os.replace(IN / (name + ".part"), IN / name)
                    res = f"fed as in/RoninOrders/{name}"
        except sidecar_lock.Locked:
            res = "locked (not done)"
    cnt[f"{where}: {res.split(' as in/')[0].split(' to out/')[0]}"] += 1
    rows.append({"where": where, "file": js.name, "order_number": on, "form": f.get("form"), "supplier": f.get("supplier"),
                 "source_folder": sc.get("source_folder"), "result": res})

out = ROOT / "reports" / f"ronin move {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}.csv"
with open(out, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
print(json.dumps({"applied": a.apply, "results": dict(cnt.most_common()), "report": str(out)}, indent=1))

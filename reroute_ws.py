#!/usr/bin/env python3
"""Sends workshop sheets that the Purchase Orders reader already put aside ("not an order": on hold, or deleted from the
hold queue on the dashboard) to the Workshop Sheets queue, so they are read as sheets.

For each candidate sidecar in out/PurchaseOrders/hold/ and deleted/PurchaseOrders/**/ flagged not_an_order whose PDF
looks like a workshop sheet (ws_reader.looks_like_ws: file name + sheet wording), the PDF is COPIED to
in/WorkshopSheets/ with a _meta note (original name and folder). The PO hold copy is then moved to
deleted/PurchaseOrders/<yyyy-mm>/ with a .rerouted.json note (nothing is removed). Dry run unless --apply.

    python3 reroute_ws.py [--root /srv/ocr/scans/pipeline] [--apply] [--include-deleted]
"""
import argparse, json, os, re, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ws_reader  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--include-deleted", action="store_true", help="also sheets deleted from the PO hold queue")
a = ap.parse_args()
root = Path(a.root)
dest = root / "in" / "WorkshopSheets"
meta_dir = dest / "_meta"

cands = sorted((root / "out" / "PurchaseOrders" / "hold").glob("*.json"))
if a.include_deleted:
    cands += sorted(p for p in (root / "deleted" / "PurchaseOrders").rglob("*.json")
                    if not p.name.endswith((".deleted.json", ".rerouted.json")))
seen, n = set(), 0
for js in cands:
    try:
        sc = json.loads(js.read_text(encoding="utf-8"))
    except Exception:
        continue
    if "not_an_order" not in (sc.get("flags") or []):
        continue
    pdf = js.with_name(str(sc.get("file") or ""))
    if not pdf.is_file():
        continue
    src = str(sc.get("source_file") or pdf.name)
    ok, score, why = ws_reader.looks_like_ws(pdf, src)
    print(f"{'WS ' if ok else '-- '} {score:2d}  {js.relative_to(root)}  ({why})")
    if not ok:
        continue
    h = sc.get("source_sha256"), sc.get("source_page")
    if h in seen:
        continue
    seen.add(h)
    n += 1
    if not a.apply:
        continue
    dest.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(src).stem)[:120]
    name = stem + (f"_p{sc['source_page']}" if sc.get("source_page") else "") + ".pdf"
    k = 2
    while (dest / name).exists():
        name = f"{stem}_{k}.pdf"
        k += 1
    meta = {"original_name": src, "rerouted_from": str(js.relative_to(root))}
    if sc.get("source_folder"):
        meta["source_folder"] = sc["source_folder"]
    (meta_dir / (name + ".json")).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    shutil.copy2(pdf, dest / (name + ".tmp"))
    os.replace(dest / (name + ".tmp"), dest / name)
    if js.parent.name == "hold":
        gone = root / "deleted" / "PurchaseOrders" / datetime.now().strftime("%Y-%m")
        gone.mkdir(parents=True, exist_ok=True)
        for q in (js, pdf):
            shutil.move(str(q), str(gone / q.name))
        (gone / (js.stem + ".rerouted.json")).write_text(json.dumps({"to": f"in/WorkshopSheets/{name}",
                                                                      "at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
print(f"{n} workshop sheet(s) {'queued in in/WorkshopSheets' if a.apply else 'would be queued (dry run: add --apply)'}")

#!/usr/bin/env python3
"""MPS orders made out to Ronin that were read as Ronin forms (A79549 -> "DF9549"): back to Purchase Orders, read again
with po_reader's "made out to Ronin" rule (29 Oct). Picks RoninOrders documents whose original file name carries an
A-number and no DF number.
    python3 ronin_back.py [--apply]"""
import argparse, glob, json, os, re, shutil, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort, sidecar_lock                          # noqa: E402
ROOT = Path("/srv/ocr/scans/pipeline"); IN = ROOT / "in" / "PurchaseOrders"
ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
stamp = datetime.now().isoformat(timespec="seconds")
for js in sorted(glob.glob(str(ROOT / "out" / "RoninOrders" / "*.json")) + glob.glob(str(ROOT / "out" / "RoninOrders" / "hold" / "*.json"))):
    js = Path(js)
    try:
        sc = json.load(open(js, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    src = str(sc.get("source_file") or "").upper().replace(" ", "")
    if not re.search(r"(?<![A-Z0-9])A\d{5}(?!\d)", src) or re.search(r"(?<![A-Z0-9])DF\d{4}(?!\d)", src):
        continue
    pdf = js.parent / str(sc.get("file") or "")
    print(("back to Purchase Orders: " if a.apply else "would send back: ") + f"{js.name} ({sc.get('source_file')})")
    if not a.apply or not pdf.is_file():
        continue
    with sidecar_lock.locked(js, "ronin_back"):
        stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(sc.get("source_file") or pdf.name)).stem)[:100]
        name, k = f"{stem}.pdf", 2
        while (IN / name).exists() or (IN / "_meta" / (name + ".json")).exists():
            name = f"{stem}_{k}.pdf"; k += 1
        note = {"original_name": sc.get("source_file") or pdf.name, "reread_of": f"out/RoninOrders/{js.parent.name}/{pdf.name}",
                "reread_at": stamp, "why": "MPS order made out to Ronin, read as a Ronin form (29 Oct)"}
        for k2 in ("source_folder", "source_modified"):
            if sc.get(k2):
                note[k2] = sc[k2]
        (IN / "_meta").mkdir(exist_ok=True)
        (IN / "_meta" / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
        shutil.copy2(pdf, IN / (name + ".part"))
        route_sort.remove(js, sc, "RoninOrders", f"MPS order made out to Ronin: read again in Purchase Orders as {name}", stamp)
        os.replace(IN / (name + ".part"), IN / name)
        os.utime(IN / name, (1_600_000_000, 1_600_000_000))   # oldest in the queue: read next, not after today's batch

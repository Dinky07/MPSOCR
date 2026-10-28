#!/usr/bin/env python3
"""Queues workshop-sheet PDFs from a folder tree into the pipeline's Workshop Sheets queue, in batches, so they can be
checked on the dashboard (and used for training) before the next batch goes in.

Picks files whose NAME says they are workshop sheets ("WS SHEET(S)", "WORKSHOP SHEET", "DRGS"; --pattern to change),
oldest first, skipping files already fed (remembered in <root>/feed_ws.csv by SHA-256) and won't start a new batch while
the last one is still waiting in in/WorkshopSheets (unless --force). Each file gets a _meta note with its original
folder, as the PO feed does.

    python3 feed_ws.py --source "/mnt/nas-scans/ScanToFile/Melyssa/Orders-MPS" --folder-prefix "Melyssa\\Orders-MPS" --list
    python3 feed_ws.py --source ... --batch 10            # queue the next 10 files
"""
import argparse, csv, hashlib, json, os, re, shutil, sys
from datetime import datetime
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--source", required=True)
ap.add_argument("--root", default="/srv/ocr/scans/pipeline", help="pipeline root (the ocr-scans share's pipeline folder)")
ap.add_argument("--folder-prefix", default="", help="put in front of the relative folder in the _meta note")
ap.add_argument("--pattern", default=r"\bW\.?\s?S\.?\s*SHEETS?\b|WORK\s*SHOP\s*SHEETS?|\bDRGS\b")
ap.add_argument("--batch", type=int, default=10)
ap.add_argument("--order", choices=["oldest", "newest"], default="oldest")
ap.add_argument("--list", action="store_true")
ap.add_argument("--force", action="store_true")
a = ap.parse_args()

src, root = Path(a.source), Path(a.root)
dest = root / "in" / "WorkshopSheets"
state = root / "feed_ws.csv"
rx = re.compile(a.pattern, re.I)
files = [p for p in src.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf" and rx.search(p.name)]
files.sort(key=lambda p: p.stat().st_mtime, reverse=a.order == "newest")
done = {}
if state.exists():
    with open(state, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            done[r["sha256"]] = r


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


if a.list:
    by = {}
    for p in files:
        k = str(p.parent.relative_to(src)).split(os.sep)[0]
        by[k] = by.get(k, 0) + 1
    print(f"{len(files)} workshop-sheet files under {src} ({len(done)} fed before)")
    for k, v in sorted(by.items()):
        print(f"  {v:5d}  {k}")
    sys.exit(0)

waiting = [p for p in dest.glob("*") if p.is_file()] if dest.exists() else []
if waiting and not a.force:
    sys.exit(f"{len(waiting)} file(s) from the last batch are still waiting in {dest}: let them finish (or --force)")
dest.mkdir(parents=True, exist_ok=True)
(dest / "_meta").mkdir(exist_ok=True)
new = state.exists()
fed = 0
batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
with open(state, "a", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["sha256", "path", "size", "fed_at", "batch", "queued_as"])
    if not new:
        w.writeheader()
    for p in files:
        if fed >= a.batch:
            break
        h = sha(p)
        if h in done:
            continue
        rel = str(p.parent.relative_to(src)).replace("/", "\\")
        name = p.name
        k = 2
        while (dest / name).exists():
            name = f"{p.stem}_{k}{p.suffix}"
            k += 1
        meta = {"original_name": p.name, "source_folder": "\\".join(x for x in (a.folder_prefix, rel) if x and x != ".")}
        (dest / "_meta" / (name + ".json")).write_text(json.dumps(meta, indent=1), encoding="utf-8")
        shutil.copy2(p, dest / (name + ".tmp"))
        os.replace(dest / (name + ".tmp"), dest / name)
        w.writerow({"sha256": h, "path": str(p), "size": p.stat().st_size, "fed_at": datetime.now().isoformat(timespec="seconds"),
                    "batch": batch_id, "queued_as": name})
        done[h] = True
        fed += 1
        print("queued", p.relative_to(src))
print(f"{fed} file(s) queued in {dest} (batch {batch_id}); {len([p for p in files])} workshop-sheet files in the source")

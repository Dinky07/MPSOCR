#!/usr/bin/env python3
r"""
One-off (Dino 18 Nov 2026): move the scans the collector already moved into each Chronoscan folder's own imported\
into the shared folder each source's "move_after_import" now names (Supplier Docs MPS\imported\<Type>).

Only files the collector itself moved are touched: the state DB's nas_seen rows with status 'imported'. Anything
else in an imported\ folder (for example what Chronoscan's own upload left in JTs MPS\imported before 19 Oct) stays.
Plain renames on the share, never copy + delete, never overwrite (a clash gets _2, _3 ...); a recursive source keeps
its sub-folders. The collector must be stopped while this runs (it shares the state DB).

    sudo systemctl stop mps-ocr-collector
    python3 ops/relocate_imported.py            # dry run: lists every move, changes nothing
    python3 ops/relocate_imported.py --apply
    sudo systemctl start mps-ocr-collector
"""
import json, os, sqlite3, subprocess, sys
from datetime import datetime
from pathlib import Path

CONFIG = Path(os.environ.get("MPS_OCR_CONFIG", "/srv/ocr/pipeline/config.json"))
apply = "--apply" in sys.argv
cfg = json.loads(CONFIG.read_text(encoding="utf-8"))

state = subprocess.run(["systemctl", "is-active", "mps-ocr-collector"], capture_output=True, text=True).stdout.strip()
if state == "active":
    sys.exit("mps-ocr-collector is running: sudo systemctl stop mps-ocr-collector first")

db = sqlite3.connect(cfg.get("state_db", "/srv/ocr/pipeline/state.db"), timeout=30)
now = datetime.now().isoformat(timespec="seconds")
moved = skipped = 0
for src in cfg.get("collector", {}).get("sources", []):
    to = src.get("move_after_import")
    if not to or not Path(to).is_absolute():
        continue
    base, old, new = Path(src["path"]), Path(src["path"]) / "imported", Path(to)
    if old == new:
        continue
    rows = db.execute("SELECT path, size, mtime, sha256 FROM nas_seen WHERE type_folder=? AND status='imported'",
                      (src["type_folder"],)).fetchall()
    for path, size, mtime, digest in rows:
        p = Path(path)
        if old not in p.parents or p.parent == new or new in p.parents:
            continue                       # not in the old folder, or already in the new one
        if not p.exists():
            print(f"gone, skipped: {p}")
            skipped += 1
            continue
        dst_dir = new / p.parent.relative_to(old)
        dst = dst_dir / p.name
        n = 2
        while dst.exists():
            dst = dst_dir / f"{p.stem}_{n}{p.suffix}"
            n += 1
        print(f"{'move' if apply else 'would move'}: {p}  ->  {dst}")
        if not apply:
            moved += 1
            continue
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            os.rename(p, dst)
        except OSError as e:
            print(f"   FAILED, left in place: {e}")
            skipped += 1
            continue
        db.execute("UPDATE nas_seen SET status='moved', at=? WHERE path=?", (now, str(p)))
        db.execute("INSERT OR REPLACE INTO nas_seen VALUES(?,?,?,?,?,?,?)",
                   (str(dst), size, mtime, digest, "imported", src["type_folder"], now))
        db.commit()
        moved += 1
    if apply:
        try:
            old.rmdir()                    # only if now empty; never removes anything else
            print(f"removed empty folder {old}")
        except OSError:
            pass
print(f"{moved} {'moved' if apply else 'would be moved (dry run: add --apply)'}, {skipped} skipped")

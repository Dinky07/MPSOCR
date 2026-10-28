#!/usr/bin/env python3
"""Test-certificate backfill feed: the 922 files moved into the Test Certificates library (MPS Sales) go into the
TestCerts queue in batches (link mode: the library item already exists, the sync fills its columns and items).

The list is tc_feed.csv (built from the move: file, family, old library, SHA-256, old URL, old columns, new item id and
URL), in family rotation so each batch has pipe, flanges and fittings. The files are on the box in
/srv/ocr/scans/tc/files/<Family>/. Anything the pipeline has read (state.db, SHA-256) or fed before (<root>/feed_tc.csv)
is skipped. Each file gets a _meta note: original name and folder, family (the reader's hint), the old hand-captured
columns (shown beside the reading on the dashboard), the library item id/URL and sync mode "link".

    python3 feed_tc.py --list          # what is left
    python3 feed_tc.py --dry-run       # what the next batch would be
    python3 feed_tc.py --batch 50      # feed
"""
import argparse, csv, json, os, shutil, sqlite3
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed", default=str(HERE / "tc_feed.csv"))
    ap.add_argument("--files", default="/srv/ocr/scans/tc/files")
    ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
    ap.add_argument("--type", default="TestCerts")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root, files = Path(a.root), Path(a.files)
    dest = root / "in" / a.type
    ledger = root / "feed_tc.csv"
    rows = list(csv.DictReader(open(a.feed, encoding="utf-8")))
    db = sqlite3.connect(f"file:{HERE / 'state.db'}?mode=ro", uri=True)
    read = {r[0].lower() for r in db.execute("SELECT sha256 FROM processed")}
    fed = {r["sha256"].lower() for r in csv.DictReader(open(ledger, encoding="utf-8"))} if ledger.exists() else set()
    todo, missing = [], []
    for r in rows:
        if r["sha256"] in read or r["sha256"] in fed:
            continue
        p = files / r["family"] / r["file"]
        if not p.is_file():
            missing.append(str(p))
            continue
        todo.append((r, p))
    if a.list:
        left = {}
        for r, _ in todo:
            left[r["family"]] = left.get(r["family"], 0) + 1
        print(f"{len(rows)} in the list; read {sum(1 for r in rows if r['sha256'] in read)}, fed and waiting "
              f"{sum(1 for r in rows if r['sha256'] in fed and r['sha256'] not in read)}, still to feed {len(todo)} "
              f"({', '.join(f'{k} {v}' for k, v in sorted(left.items()))}); file not on the box: {len(missing)}")
        for m in missing[:10]:
            print("  missing:", m)
        return
    waiting = [p for p in dest.glob("*") if p.is_file() and not p.name.endswith((".part", ".tmp"))] if dest.exists() else []
    batch = todo[:max(0, a.batch - len(waiting))]
    print(f"{datetime.now():%Y-%m-%d %H:%M} {len(waiting)} waiting; feeding {len(batch)}; {len(todo) - len(batch)} left after this"
          + (f"; {len(missing)} files not on the box" if missing else ""))
    if a.dry_run or not batch:
        for r, p in batch[:10]:
            print(f"  {r['family']:9} {r['file']}")
        return
    (dest / "_meta").mkdir(parents=True, exist_ok=True)
    new = not ledger.exists()
    bid = datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(ledger, "a", newline="", encoding="utf-8") as lf:
        w = csv.DictWriter(lf, fieldnames=["sha256", "file", "family", "sp_item_id", "queued_as", "fed_at", "batch"])
        if new:
            w.writeheader()
        for r, p in batch:
            name, k = p.name, 2
            while (dest / name).exists():
                name = f"{p.stem}_{k}{p.suffix}"
                k += 1
            meta = {"original_name": p.name, "source_folder": r["library"], "family": r["family"],
                    "old_columns": r["old_columns"], "old_url": r["old_url"], "sp_item_id": r["sp_item_id"],
                    "sp_url": r["sp_url"], "sync_mode": "link", "fed_by": "feed_tc", "batch": bid}
            (dest / "_meta" / (name + ".json")).write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
            shutil.copy2(p, dest / (name + ".part"))           # the worker ignores .part files
            os.replace(dest / (name + ".part"), dest / name)
            w.writerow({"sha256": r["sha256"], "file": p.name, "family": r["family"], "sp_item_id": r["sp_item_id"],
                        "queued_as": name, "fed_at": datetime.now().isoformat(timespec="seconds"), "batch": bid})
    print(f"fed {len(batch)} -> {dest} (batch {bid})")


if __name__ == "__main__":
    main()

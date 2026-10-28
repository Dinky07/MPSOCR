#!/usr/bin/env python3
"""Daily purchase-order feed (timer mps-po-feed, 03:00): tops the purchase-order queue up to --batch files (1,500) from
the archive staged on the box (/srv/ocr/scans/po_balance/in = O:\\po_balance\\in), in Dino's order (24 Oct 2026):
the rest of 2024, then 2025, the current letter folders (A-Z, Email), 2023, and 2017-2022.

Skips anything the pipeline has read already (state.db, by SHA-256) and anything fed before (<root>/feed_po.csv), feeds a
file found in two folders once, and gives every file a _meta note with its original folder (as feed_ws.py does), so the
reader sees "Melyssa\\Orders-MPS\\2024\\M\\MIA GAS" like the earlier batches. The day's list goes to
<root>/work/po_daily/<date>/batch.csv for the checking file (po_daily.py).

    python3 feed_po.py --list                 # what is left, per group
    python3 feed_po.py --dry-run              # what tonight's batch would be
    python3 feed_po.py                        # feed (what the timer runs)
"""
import argparse, csv, hashlib, json, os, shutil, sqlite3, sys
from datetime import datetime
from pathlib import Path

GROUPS = ["2024", "2025", "current", "2023", "2017 - 2022"]
YEARS = {"2017 - 2022", "2023", "2024", "2025"}


def group_of(rel):
    top = rel.parts[0] if len(rel.parts) > 1 else ""
    if top in YEARS:
        return top
    return "current" if top else None               # A-Z and Email folders hold the current orders


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="/srv/ocr/scans/po_balance/in")
    ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
    ap.add_argument("--folder-prefix", default="Melyssa\\Orders-MPS")
    ap.add_argument("--batch", type=int, default=1500)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    src, root = Path(a.source), Path(a.root)
    dest = root / "in" / "PurchaseOrders"
    ledger = root / "feed_po.csv"
    work = root / "work" / "po_daily"
    work.mkdir(parents=True, exist_ok=True)
    index_p = work / "feed_index.csv"

    # SHA-256 per archive file, cached by (size, mtime) so only new or changed files are hashed
    index = {}
    if index_p.exists():
        for r in csv.DictReader(open(index_p, encoding="utf-8")):
            index[r["path"]] = r
    files, changed = [], 0
    for p in sorted(src.rglob("*")):
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        g = group_of(p.relative_to(src))
        if not g:
            continue
        st = p.stat()
        r = index.get(str(p))
        if not r or r["size"] != str(st.st_size) or r["mtime"] != str(int(st.st_mtime)):
            r = {"path": str(p), "size": str(st.st_size), "mtime": str(int(st.st_mtime)), "sha256": sha(p)}
            index[str(p)] = r
            changed += 1
        files.append((GROUPS.index(g), g, str(p.relative_to(src)).lower(), p, r["sha256"]))
    if changed:
        tmp = index_p.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["path", "size", "mtime", "sha256"])
            w.writeheader()
            w.writerows(index[k] for k in sorted(index))
        os.replace(tmp, index_p)

    db = sqlite3.connect(f"file:{Path(__file__).resolve().with_name('state.db')}?mode=ro", uri=True)
    read = {r[0].lower() for r in db.execute("SELECT sha256 FROM processed")}
    fed = set()
    if ledger.exists():
        fed = {r["sha256"].lower() for r in csv.DictReader(open(ledger, encoding="utf-8"))}
    todo, seen = [], set()
    for gi, g, key, p, h in sorted(files):
        if h in read or h in fed or h in seen:
            continue
        seen.add(h)
        todo.append((g, p, h))
    left = {g: sum(1 for x in todo if x[0] == g) for g in GROUPS}
    if a.list:
        print(f"{len(files)} archive PDFs; still to read {len(todo)} (read {len(read & {f[4] for f in files})}, fed and waiting "
              f"{len(fed - read)}); at {a.batch} a day: {-(-len(todo) // a.batch)} days")
        for g in GROUPS:
            print(f"  {g:12} {left[g]:6}")
        return
    waiting = [p for p in dest.glob("*") if p.is_file() and not p.name.endswith((".part", ".tmp"))] if dest.exists() else []
    n = max(0, a.batch - len(waiting))
    batch = todo[:n]
    print(f"{datetime.now():%Y-%m-%d %H:%M} queue has {len(waiting)} waiting; feeding {len(batch)} "
          f"({', '.join(f'{g} {sum(1 for x in batch if x[0] == g)}' for g in GROUPS if any(x[0] == g for x in batch))}); "
          f"{len(todo) - len(batch)} left after this")
    if a.dry_run or not batch:
        return
    day = work / datetime.now().strftime("%Y-%m-%d")
    day.mkdir(exist_ok=True)
    (dest / "_meta").mkdir(parents=True, exist_ok=True)
    new_ledger = not ledger.exists()
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(ledger, "a", newline="", encoding="utf-8") as lf, open(day / "batch.csv", "a", newline="", encoding="utf-8") as bf:
        lw = csv.DictWriter(lf, fieldnames=["sha256", "path", "size", "fed_at", "batch", "queued_as"])
        bw = csv.DictWriter(bf, fieldnames=["sha256", "path", "group", "queued_as", "fed_at", "batch"])
        if new_ledger:
            lw.writeheader()
        if bf.tell() == 0:
            bw.writeheader()
        for g, p, h in batch:
            rel = str(p.parent.relative_to(src)).replace("/", "\\")
            name, k = p.name, 2
            while (dest / name).exists():
                name = f"{p.stem}_{k}{p.suffix}"
                k += 1
            meta = {"original_name": p.name, "source_folder": "\\".join(x for x in (a.folder_prefix, rel) if x and x != "."),
                    "fed_by": "feed_po", "batch": batch_id}
            (dest / "_meta" / (name + ".json")).write_text(json.dumps(meta, indent=1), encoding="utf-8")
            shutil.copy2(p, dest / (name + ".part"))          # the worker ignores .part files
            os.replace(dest / (name + ".part"), dest / name)
            now = datetime.now().isoformat(timespec="seconds")
            lw.writerow({"sha256": h, "path": str(p), "size": p.stat().st_size, "fed_at": now, "batch": batch_id, "queued_as": name})
            bw.writerow({"sha256": h, "path": str(p), "group": g, "queued_as": name, "fed_at": now, "batch": batch_id})
    print(f"fed {len(batch)} -> {dest} (batch {batch_id}, list {day / 'batch.csv'})")


if __name__ == "__main__":
    main()

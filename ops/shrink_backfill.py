#!/usr/bin/env python3
"""shrink_backfill: make the files already in SharePoint smaller too (Dino, 19 Nov 2026: "yes do that").

PR #6 shrinks every PDF the worker publishes from now on (pdf_shrink: 200 dpi, JPEG quality 70; files already at or
below that are left alone). This does the same for the documents that synced before it, in three steps:

  1. frigate:  python3 ops/shrink_backfill.py plan [--type T ...] [--limit N]
     For each synced document the PC sync uploaded (synced/<Type>/<yyyy-MM>/<name>.json with its .json.uploaded
     marker), a shrunk copy goes to <root>/shrink_backfill/staged/<Type>/<yyyy-MM>/<file> and a row to
     <root>/shrink_backfill/plan.csv. Nothing in synced/ changes. Documents that would not get at least 15% smaller
     are left out. Link-mode types (SupplierInvoicesArchive, TestCerts: their SharePoint file is the archive's, not
     ours) are never touched.
  2. PC:       .\\Set-ShrunkFiles.ps1              (check)   then   .\\Set-ShrunkFiles.ps1 -Apply
     Replaces each file's content in SharePoint with the shrunk copy, only when the library file is still the one
     uploaded (same name, same size). SharePoint keeps the old file as the previous version. Writes done.csv.
  3. frigate:  python3 ops/shrink_backfill.py finish
     For each row in done.csv: the synced PDF is swapped for the shrunk copy (the original goes to
     shrink_backfill/originals/, delete that folder once happy) and the sidecar gets "shrunk": {..., "backfill": ...}.

What stays the same: the library columns, SQL and the sidecar's content_hash. ContentHash is how the libraries, SQL
and the fix-up CSVs know a document, so it stays the hash of the file as first uploaded.

  python3 ops/shrink_backfill.py plan --type PurchaseOrders --limit 20      # try a few first
  python3 ops/shrink_backfill.py status                                      # what is planned / done / finished
"""
import argparse, csv, hashlib, json, os, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pdf_shrink  # noqa: E402

ROOT = Path(os.environ.get("MPS_PIPELINE_ROOT", "/srv/ocr/scans/pipeline"))
LINK_TYPES = {"SupplierInvoicesArchive", "TestCerts"}      # Mode "link" in the PC's config.json: not our files
COLS = ["type", "sidecar", "file", "staged", "bytes_in", "bytes_out", "sha_in", "sha_out", "drive_id", "item_id", "sp_name"]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest().upper()


def read_csv(p):
    if not p.is_file():
        return []
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def plan(a):
    bf = ROOT / "shrink_backfill"
    out = bf / "plan.csv"
    have = {r["sidecar"] for r in read_csv(out)}
    finished = {r["sidecar"] for r in read_csv(bf / "finished.csv")}
    types = a.type or sorted(p.name for p in (ROOT / "synced").iterdir() if p.is_dir())
    new, seen, small, skipped = [], 0, 0, 0
    for t in types:
        if t in LINK_TYPES:
            print(f"{t}: link-mode library (not our files), skipped")
            continue
        for js in sorted((ROOT / "synced" / t).glob("*/*.json")):
            rel = str(js.relative_to(ROOT))
            if rel in have or rel in finished:
                continue
            marker = js.with_name(js.name + ".uploaded")
            if not marker.is_file():
                skipped += 1
                continue
            try:
                sc = json.loads(js.read_text(encoding="utf-8-sig"))
                sp = json.loads(marker.read_text(encoding="utf-8-sig"))
            except Exception:
                skipped += 1
                continue
            if sc.get("shrunk") or not sp.get("itemId") or not sp.get("driveId"):
                skipped += 1
                continue
            pdf = js.with_name(str(sc.get("file") or ""))
            if not pdf.is_file() or pdf.suffix.lower() not in (".pdf", ".jpg", ".jpeg"):
                skipped += 1
                continue
            seen += 1
            dst = bf / "staged" / t / js.parent.name / pdf.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            info = pdf_shrink.shrink(pdf, dst)
            if not info:
                small += 1
                continue
            new.append({"type": t, "sidecar": rel, "file": pdf.name, "staged": str(dst.relative_to(ROOT)),
                        "bytes_in": info["bytes_in"], "bytes_out": info["bytes_out"], "sha_in": sha256(pdf),
                        "sha_out": sha256(dst), "drive_id": sp["driveId"], "item_id": sp["itemId"],
                        "sp_name": sp.get("name") or pdf.name})
            if a.limit and len(new) >= a.limit:
                break
        if a.limit and len(new) >= a.limit:
            break
    if new:
        bf.mkdir(parents=True, exist_ok=True)
        first = not out.is_file()
        with open(out, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, COLS)
            if first:
                w.writeheader()
            w.writerows(new)
    bi, bo = sum(int(r["bytes_in"]) for r in new), sum(int(r["bytes_out"]) for r in new)
    print(f"{seen} document(s) looked at, {len(new)} shrunk and staged, {small} already small, {skipped} skipped "
          f"(no upload marker / already shrunk / missing file)")
    if new:
        print(f"staged: {bi / 1e9:.2f} GB -> {bo / 1e9:.2f} GB ({100 * (1 - bo / bi):.0f}% smaller)")
        print(f"next, on the PC:  .\\Set-ShrunkFiles.ps1   then   .\\Set-ShrunkFiles.ps1 -Apply")


def finish(a):
    bf = ROOT / "shrink_backfill"
    plan_rows = {r["sidecar"]: r for r in read_csv(bf / "plan.csv")}
    finished = {r["sidecar"] for r in read_csv(bf / "finished.csv")}
    rows = [r for r in read_csv(bf / "done.csv") if r.get("result") == "replaced" and r["sidecar"] not in finished]
    stamp = datetime.now().isoformat(timespec="seconds")
    n = 0
    for d in rows:
        r = plan_rows.get(d["sidecar"])
        if not r:
            print(f"{d['sidecar']}: not in plan.csv, skipped")
            continue
        js = ROOT / r["sidecar"]
        pdf, staged = js.with_name(r["file"]), ROOT / r["staged"]
        if not js.is_file() or not staged.is_file() or not pdf.is_file():
            print(f"{r['sidecar']}: file missing (moved since?), skipped")
            continue
        if sha256(pdf) != r["sha_in"] or sha256(staged) != r["sha_out"]:
            print(f"{r['sidecar']}: the synced PDF or the staged copy changed since the plan, skipped")
            continue
        orig = bf / "originals" / r["type"] / js.parent.name / pdf.name
        orig.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, orig)
        tmp = pdf.with_name(pdf.name + ".tmp")
        shutil.copy2(staged, tmp)
        os.replace(tmp, pdf)
        sc = json.loads(js.read_text(encoding="utf-8-sig"))
        sc["shrunk"] = {"bytes_in": int(r["bytes_in"]), "bytes_out": int(r["bytes_out"]), "dpi": pdf_shrink.DEFAULTS["dpi"],
                        "quality": pdf_shrink.DEFAULTS["quality"], "backfill": stamp, "sha_in": r["sha_in"], "sha_out": r["sha_out"]}
        tj = js.with_name(js.name + ".tmp")
        tj.write_text(json.dumps(sc, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tj, js)
        staged.unlink()
        first = not (bf / "finished.csv").is_file()
        with open(bf / "finished.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["sidecar", "at"])
            w.writerow([r["sidecar"], stamp])
        n += 1
    print(f"{n} synced PDF(s) swapped for the shrunk copy; originals in {bf / 'originals'}")


def status(a):
    bf = ROOT / "shrink_backfill"
    p, d, f = read_csv(bf / "plan.csv"), read_csv(bf / "done.csv"), read_csv(bf / "finished.csv")
    bi, bo = sum(int(r["bytes_in"]) for r in p), sum(int(r["bytes_out"]) for r in p)
    res = {}
    for r in d:
        res[r.get("result")] = res.get(r.get("result"), 0) + 1
    print(f"planned {len(p)} ({bi / 1e9:.2f} GB -> {bo / 1e9:.2f} GB); PC results {res or 'none yet'}; finished {len(f)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan")
    s.add_argument("--type", action="append", help="only this type (repeatable); default every synced type")
    s.add_argument("--limit", type=int, default=0, help="stop after this many staged documents")
    sub.add_parser("finish")
    sub.add_parser("status")
    a = ap.parse_args()
    if not pdf_shrink.AVAILABLE:
        sys.exit("pdf_shrink needs pikepdf and Pillow in this python")
    {"plan": plan, "finish": finish, "status": status}[a.cmd](a)

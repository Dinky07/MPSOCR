#!/usr/bin/env python3
"""Orphan pages on the Purchase / Polymer Orders holds, rejoined and sorted again (Dino 2 Nov: "if you can rejoin pages
in a separated file do it and move to the sorter so it can be sorted again").

An orphan (orphan_remove.py's rule) is a page document on hold from a scan of two or more pages whose other pages are
nowhere in the pipeline any more (not in a queue, on a hold or synced). Its whole original scan (the archive file in
feed_po.csv, else done/<folder>/<month>/ via state.db, checked by SHA-256) goes back. The same for a scan already put
back together in one file on hold (grouped_pages = every page, the 29 Oct regroup: 162 on 2 Nov, judged on page 1 only),
read as not an order. Either way it goes back into in/PurchaseOrders with a _meta "reread_of" note, so the worker reads it again although it has
seen it: the pre-sort sorts its pages (other queues, or the order reader) and one file = one document groups them. The
orphan page goes to deleted/<Type>/<yyyy-mm>/ with the reason (recoverable). Each scan is fed once
(work/orphan_refeed/fed.csv). Skipped: checked, edited by a person, uploaded, locked, original not found or changed.

    python3 orphan_refeed.py [--apply] [--limit N]
Report: reports/orphan refeed <stamp>[ dry run].csv
"""
import argparse, csv, glob, hashlib, json, os, re, shutil, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402  remove()
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
IN = ROOT / "in" / "PurchaseOrders"
LEDGER = ROOT / "work" / "orphan_refeed" / "fed.csv"
TYPES = ("PurchaseOrders", "PolymerOrders")
FORMS = {"mps_order", "polymer_order", "internal_group_transfer", "ronin_order"}
SCRIPT_EDIT = re.compile(r"^(po_autocheck|route_sort|hold_resort|od_sort|requeue_po12|numbers_pass|fix_|supplier rule|ronin_|coalesce_hold|regroup_|ownform_fix|394 restore|\d+-)", re.I)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def npages(p):
    try:
        import subprocess
        return int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True, timeout=60).stdout).group(1))
    except Exception:
        return 0


def find_original(h, feed, db):
    """The scan as it came in: the archive file (feed_po.csv), else done/<folder>/<month>/<name> (state.db), by hash."""
    r = feed.get(h)
    if r and Path(r["path"]).is_file():
        return Path(r["path"]), r.get("queued_as") or Path(r["path"]).name
    row = db.execute("SELECT folder, source FROM processed WHERE sha256=?", (h,)).fetchone()
    if row:
        for p in sorted(glob.glob(str(ROOT / "done" / row[0] / "*" / glob.escape(row[1])))):
            if sha256(p).lower() == h:
                return Path(p), row[1]
    return None, None


def human_edits(sc):
    return [e for e in sc.get("edits") or [] if not SCRIPT_EDIT.match(str(e.get("by") or ""))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    docs = defaultdict(list)                         # scan sha -> [(place, type, json)]
    for pat, place in (("out/*/*.json", "queue"), ("out/*/hold/*.json", "hold"), ("synced/*/*/*.json", "synced")):
        for p in glob.glob(str(ROOT / pat)):
            if p.endswith((".tmp", ".lock")):
                continue
            try:
                sc = json.load(open(p, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            h = str(sc.get("source_sha256") or "").lower()
            if h:
                docs[h].append((place, p.split("/")[-2] if place == "queue" else p.split("/")[-3], Path(p)))
    feed = {r["sha256"].lower(): r for r in csv.DictReader(open(ROOT / "feed_po.csv", encoding="utf-8"))}
    import sqlite3
    cfg = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))
    db = sqlite3.connect(cfg.get("state_db", "/srv/ocr/pipeline/state.db"), timeout=30)
    done = set()
    if LEDGER.exists():
        done = {r["sha256"] for r in csv.DictReader(open(LEDGER, encoding="utf-8"))}
    rows, cnt = [], Counter()
    todo = []
    for t in TYPES:
        for js in sorted((ROOT / "out" / t / "hold").glob("*.json")):
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            f = sc.get("fields") or {}
            h = str(sc.get("source_sha256") or "").lower()
            if not h or sc.get("merged") or f.get("checked") or Path(str(js) + ".uploaded").exists():
                continue
            if f.get("form") in FORMS and f.get("order_number"):
                continue                                   # an order form with its number is a document, not a stray page
            others = [d for d in docs[h] if d[2] != js]
            if others:
                continue                                   # the rest of the scan is somewhere: not an orphan
            orig, oname = find_original(h, feed, db)
            res = None
            n = npages(orig) if orig is not None else 0
            if orig is not None and n < 2:
                continue                                   # a one-page scan was never split: nothing to rejoin
            g = sc.get("grouped_pages") or []
            if g and len(g) != n:
                continue                                   # grouped with only part of its scan: not handled here
            # g == every page: a scan already put back together (regroup 29 Oct), judged on page 1 only - fed again whole
            # so the pre-sort sees every page (Dino 2 Nov "yes, both")
            if human_edits(sc):
                res = "skipped: edited by a person"
            elif h in done:
                res = "skipped: fed before"
            elif orig is None:
                res = "skipped: original scan not found"
            if res:
                cnt[res] += 1
                rows.append({"type": t, "document": js.name, "source_file": sc.get("source_file"), "page": sc.get("source_page"),
                             "original": str(orig or ""), "fed_as": "", "result": res})
                continue
            todo.append((t, js, sc, h, orig, oname))
    if a.limit:
        todo = todo[: a.limit]
    md = IN / "_meta"
    if a.apply:
        md.mkdir(parents=True, exist_ok=True)
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
    new = not LEDGER.exists()
    fh = open(LEDGER, "a", encoding="utf-8", newline="") if a.apply else None
    w = csv.DictWriter(fh, fieldnames=["at", "sha256", "original", "fed_as", "orphan"]) if fh else None
    if w and new:
        w.writeheader()
    for t, js, sc, h, orig, oname in todo:
        stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(oname or sc.get("source_file") or orig.name)).stem)[:100]
        name, k = f"{stem}.pdf", 2
        while (IN / name).exists() or (md / (name + ".json")).exists() or list((ROOT / "work").glob(f"PurchaseOrders__{glob.escape(name)}")):
            name = f"{stem}_{k}.pdf"
            k += 1
        res = "would feed the whole scan"
        if a.apply:
            try:
                if sha256(orig).lower() != h:
                    res = "skipped: original changed (hash differs)"
                else:
                    with sidecar_lock.locked(js, "orphan_refeed", wait=2):
                        sc = json.load(open(js, encoding="utf-8"))
                        if (sc.get("fields") or {}).get("checked") or human_edits(sc):
                            res = "skipped: checked / edited meanwhile"
                        else:
                            note = {"original_name": orig.name, "reread_of": f"out/{t}/hold/{sc.get('file')}", "reread_at": stamp,
                                    "rejoined": True, "why": "orphan page: the whole scan fed again to be sorted (Dino 2 Nov)",
                                    "previous_doc_id": sc.get("doc_id"), "previous_hold_reason": sc.get("hold_reason")}
                            for k2 in ("source_folder", "source_modified"):
                                if sc.get(k2):
                                    note[k2] = sc[k2]
                            w.writerow({"at": stamp, "sha256": h, "original": str(orig), "fed_as": name, "orphan": js.name})
                            fh.flush()                     # the ledger first: never fed twice
                            (md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
                            shutil.copy2(orig, IN / (name + ".part"))
                            try:
                                route_sort.remove(js, sc, t, f"orphan page rejoined: its whole scan was fed again as "
                                                             f"in/PurchaseOrders/{name} to be sorted (2 Nov)", stamp)
                            except Exception as e:
                                (IN / (name + ".part")).unlink(missing_ok=True)
                                (md / (name + ".json")).unlink(missing_ok=True)
                                raise RuntimeError(f"not moved to deleted/ ({e}); not fed (ledger row stays: remove it to retry)")
                            os.replace(IN / (name + ".part"), IN / name)
                            res = "fed the whole scan"
            except sidecar_lock.Locked:
                res = "skipped: locked"
            except Exception as e:
                res = f"failed: {str(e)[:120]}"
        cnt[res.split(":")[0] if res.startswith(("failed", "skipped")) else res] += 1
        rows.append({"type": t, "document": js.name, "source_file": sc.get("source_file"), "page": sc.get("source_page"),
                     "original": str(orig), "fed_as": name, "result": res})
    if fh:
        fh.close()
    out = ROOT / "reports" / f"orphan refeed {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f2:
        wr = csv.DictWriter(f2, fieldnames=["type", "document", "source_file", "page", "original", "fed_as", "result"])
        wr.writeheader()
        wr.writerows(rows)
    print(json.dumps({"applied": a.apply, "results": dict(cnt.most_common()), "report": str(out)}, indent=1))


if __name__ == "__main__":
    main()

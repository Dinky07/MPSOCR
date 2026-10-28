#!/usr/bin/env python3
"""Apply the po_reader v12 rules to purchase orders already waiting in the out queue / hold queue (no re-reading):
the date from the archive file name (then the scan-date and neighbouring-order checks, as the reader does) and the
price memory. Orders that are Checked, or that anyone has edited on the dashboard, are left exactly as they are.
Each changed sidecar gets the rule's note in checks/review and a "rule_fixes" record; the old sidecars are backed up
by the calling script.

    python3 requeue_po12.py            # dry run: what would change
    python3 requeue_po12.py --apply
"""
import argparse, json, os, re, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import po_reader as P
import price_memory as M
import mps_jobs
import dashboard as D

ROOT = Path("/srv/ocr/scans/pipeline")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    P.JOBS_DIR = mps_jobs.JOBS_DIR = str(ROOT)
    mem = M.load(str(ROOT / "training" / "price_memory.json"))
    files = []
    for t in ("PurchaseOrders", "PolymerOrders"):
        files += sorted((ROOT / "out" / t).glob("*.json")) + sorted((ROOT / "out" / t / "hold").glob("*.json"))
    n = {"orders": 0, "checked": 0, "edited": 0, "recent": 0, "date": 0, "price_orders": 0, "prices": 0}
    examples = []
    for js in files:
        try:
            sc = json.loads(js.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if sc.get("doc_type") != "po":
            continue
        n["orders"] += 1
        f = sc.get("fields") or {}
        if f.get("checked"):
            n["checked"] += 1
            continue
        if sc.get("edits"):
            n["edited"] += 1
            continue
        if time.time() - js.stat().st_mtime < 120:
            n["recent"] += 1                          # being written right now: next time
            continue
        notes, fixes = [], []
        base = re.sub(r"^[A-Z]+__", "", str(sc.get("file") or js.stem).upper())
        nd = P.name_date(base)
        if nd and f.get("date") != nd:
            chk = []
            scan = mps_jobs.scan_date(sc.get("source_folder"), sc.get("source_file") or sc.get("file"),
                                      str(js.with_name(str(sc.get("file") or ""))), modified=None, sha256=sc.get("source_sha256"))
            fixed, _ = mps_jobs.check_order_date(f.get("order_number"), nd, chk, scan=scan)
            if fixed != f.get("date"):
                notes.append(f"date read as {f.get('date') or 'nothing'}: {nd} taken from the file name")
                notes += chk
                fixes.append({"field": "date", "old": f.get("date"), "new": fixed})
                f["date_read"], f["date"] = f.get("date"), fixed
                n["date"] += 1
        ch = M.fix_lines(f.get("items") or [], f.get("supplier"), mem)
        if ch:
            for i, old_p, new_p, desc in ch:
                notes.append(f"line {i}: price read as {old_p:g}, corrected to {new_p:g} (paid for {str(desc)[:40]} on an "
                             f"earlier checked order) - check")
                fixes.append({"field": f"items[{i}].unit_price", "old": old_p, "new": new_p})
            D._recompute("po", f)
            n["price_orders"] += 1
            n["prices"] += len(ch)
        if not fixes:
            continue
        if len(examples) < 12:
            examples.append(f"  {sc.get('file')}: " + "; ".join(notes)[:220])
        if a.apply:
            f["checks"] = list(f.get("checks") or []) + notes
            f["review"] = "; ".join(f["checks"]) or None
            sc["fields"] = f
            sc.setdefault("rule_fixes", []).append({"at": datetime.now().isoformat(timespec="seconds"), "rules": "po_reader v12",
                                                    "changes": fixes})
            tmp = js.with_name(js.name + ".tmp")
            tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, js)
    print(("APPLIED" if a.apply else "DRY RUN") + f": {n['orders']} orders in the queues; left alone: {n['checked']} checked, "
          f"{n['edited']} edited, {n['recent']} being written")
    print(f"  dates from the file name: {n['date']} orders;  prices corrected: {n['prices']} on {n['price_orders']} orders")
    print("\n".join(examples))


if __name__ == "__main__":
    main()

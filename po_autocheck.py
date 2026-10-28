#!/usr/bin/env python3
"""Clear the purchase-order queue (Dino 28 Oct 11:01): a clear Mining Pressure Systems order or a clear Polymer Lining
Systems order is marked Checked; date issues on the archive (backfill) orders are ignored - the date comes from the
buying spreadsheet where it has the order.

"Clear" means all of:
  - in the queue (not on hold), not checked yet, read as an order: form mps_order with an A-number (A#####), or
    polymer_order with a PL-number (PL####); Document Type "Purchase Order"; not cancelled; legibility not poor
  - supplier read; at least one line with a description
  - no review check left other than date checks (missing_date, "date read as ...", scan-date notes)
  - against the buying spreadsheets (work/registers, as the checking file uses them):
      * row found and it agrees on supplier and number of lines (status "match" / "sheet confirms", or "check" where
        only the Date differs): FINAL values = the spreadsheet's, as the checking-file import writes them
        -> checked_how "auto: spreadsheet"  (counts as checked data for training, like the import)
      * no row: the reader's values stand
        -> checked_how "auto: reader only"  (uploaded as Checked, but training.py leaves it out: nobody verified it)
    a row that disagrees on supplier or line count is not clear.
Everything else stays in the queue for the checking file. The reader's own reading is kept as fields_read.
DRY RUN unless --apply; reports/po autocheck <stamp>[ dry run].csv/.json. Takes the sidecar lock.

    python3 po_autocheck.py [--apply] [--no-reader-only]
"""
import argparse, csv, glob, json, os, re, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import po_daily as PD                              # noqa: E402  registers(), order_key(), pick(), compare()
import sidecar_lock                                # noqa: E402
import dashboard as D                              # noqa: E402  _recompute()

ROOT = Path("/srv/ocr/scans/pipeline")
DATE_FLAG = re.compile(r"\bdate\b|scan[- ]date|\byear\b", re.I)

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--no-reader-only", action="store_true", help="only orders the spreadsheet confirms")
a = ap.parse_args()

fed = set()
for led in (ROOT / "feed_po.csv",):
    if led.exists():
        fed = {r["sha256"].lower() for r in csv.DictReader(open(led, encoding="utf-8"))}
regs = PD.registers()
stamp = datetime.now().isoformat(timespec="seconds")
rows, why_not, block_flags = [], Counter(), Counter()
for t in ("PurchaseOrders", "PolymerOrders"):
    for js in sorted(glob.glob(str(ROOT / "out" / t / "*.json"))):
        js = Path(js)
        try:
            sc = json.load(open(js, encoding="utf-8"))
        except Exception:
            continue
        f = sc.get("fields") or {}
        if sc.get("doc_type") != "po" or f.get("checked"):
            continue
        on, form = str(f.get("order_number") or "").upper(), f.get("form")
        kind = ("MPS" if form == "mps_order" and re.fullmatch(r"A\d{5}", on) else
                "Polymer" if form == "polymer_order" and re.fullmatch(r"PL\d{4}", on) else None)
        backfill = str(sc.get("source_sha256") or "").lower() in fed
        flags = [str(x) for x in (sc.get("flags") or [])]
        why = None
        if not kind:
            why = f"not an MPS A-number / Polymer PL-number order ({form or 'no form'})"
        elif f.get("library_doc_type") != "Purchase Order":
            why = f"document type {f.get('library_doc_type')}"
        elif f.get("cancelled") or "cancelled" in flags:
            why = "cancelled"
        elif f.get("legibility") == "poor" or "poor_legibility" in flags:
            why = "poor legibility"
        elif not f.get("supplier"):
            why = "no supplier"
        elif not [i for i in (f.get("items") or []) if isinstance(i, dict) and i.get("description")]:
            why = "no lines"
        blocking = [x for x in flags if not DATE_FLAG.search(x) and x not in ("missing_date",)]
        if not why and blocking:
            why = "review checks"
            for x in blocking:
                block_flags[re.sub(r"\d+", "N", x)[:90]] += 1
        key = PD.order_key(f, sc.get("source_file") or sc.get("file"))
        reg = PD.pick(regs.get(key) or [], f) if key else None
        status, diffs, hdr, lines = PD.compare(f, reg)
        if not why and reg is not None and status == "check" and [d for d in diffs if d != "Date" and not d.startswith("line ")]:
            why = "spreadsheet disagrees: " + ", ".join(d for d in diffs if d != "Date" and not d.startswith("line "))
        if not why and reg is None and a.no_reader_only:
            why = "no spreadsheet row (--no-reader-only)"
        how = None if why else ("auto: spreadsheet" if reg is not None else "auto: reader only")
        why_not[why or how] += 1
        rows.append({"file": js.name, "type": t, "kind": kind or "", "order": on, "supplier": f.get("supplier"),
                     "backfill": backfill, "status": status, "diffs": ", ".join(diffs), "result": how or "stays",
                     "why": why or "", "flags": "; ".join(flags)[:300]})
        if not a.apply or why:
            continue
        try:
            with sidecar_lock.locked(js, "po_autocheck"):
                sc = json.load(open(js, encoding="utf-8"))           # fresh, under the lock
                f = sc.setdefault("fields", {})
                if f.get("checked"):
                    continue
                if "fields_read" not in sc:
                    sc["fields_read"] = json.loads(json.dumps(f))      # the reader's own reading, for training
                changes = []
                if reg is not None:                                    # FINAL = the spreadsheet's values (as the import)
                    for k, read, sheet, final, ok in hdr:
                        if (f.get(k) if f.get(k) != "" else None) != final and final not in (None, ""):
                            changes.append({"field": k, "old": f.get(k), "new": final})
                            f[k] = final
                    old_items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
                    new_items = []
                    for n, row, per in lines:
                        base = dict(old_items[n - 1]) if n - 1 < len(old_items) else {}
                        vals = {k: final for k, read, sheet, final, ok in row}
                        base.update(quantity=PD._num(vals.get("quantity")) if PD._num(vals.get("quantity")) is not None else vals.get("quantity"),
                                    description=vals.get("description"), unit_price=PD._num(vals.get("unit_price")))
                        if per:
                            base["price_unit"] = per
                        new_items.append(base)
                    sig = lambda its: [[i.get("quantity"), i.get("description"), i.get("unit_price")] for i in its]
                    if new_items and sig(new_items) != sig(old_items):
                        changes.append({"field": "items", "old": len(old_items), "new": len(new_items)})
                        f["items"] = new_items
                D._recompute("po", f)
                f["checked"] = True
                f["checks"] = [c for c in (f.get("checks") or []) if not DATE_FLAG.search(str(c))]
                f["review"] = "; ".join(f["checks"]) or None
                sc["flags"] = [x for x in (sc.get("flags") or []) if not (DATE_FLAG.search(str(x)) or x == "missing_date")]
                sc["needs_review"] = bool(sc["flags"])
                sc["checked_at"], sc["checked_how"] = stamp, how
                sc.setdefault("edits", []).append({"at": stamp, "by": f"po_autocheck ({how})", "changes": changes})
                tmp = js.with_name(js.name + ".ac.tmp")
                tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                os.replace(tmp, js)
        except sidecar_lock.Locked as e:
            why_not["locked (left for next run)"] += 1
            why_not[how] -= 1

out = ROOT / "reports" / f"po autocheck {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
summary = {"applied": a.apply, "orders_looked_at": len(rows),
           "by_kind": {f"{k} {v}": n for (k, v), n in sorted(Counter((r["kind"] or "other", r["result"]) for r in rows).items())},
           "backfill_clear": sum(1 for r in rows if r["backfill"] and r["result"] != "stays"),
           "results": dict(why_not.most_common()), "blocking_checks_top": dict(block_flags.most_common(15)),
           "report": f"{out}.csv"}
json.dump(summary, open(f"{out}.json", "w"), indent=1)
print(json.dumps(summary, indent=1))

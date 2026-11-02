#!/usr/bin/env python3
"""v8. Clear the purchase-order queue (Dino 28 Oct 11:01): a clear Mining Pressure Systems order or a clear Polymer Lining
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
    python3 po_autocheck.py --hold [--apply]   # the hold queues: an order that is clear, or already ticked Checked (the
                                               # checking-file import ticks it where it lies), is marked and released
    python3 po_autocheck.py --park [--apply]   # Dino 28 Oct 11:15: every order left unchecked with review checks in the
                                               # queue goes to hold for checking (dashboard Release / --hold bring it back)
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
# v8 (blind review 28 Oct 18:50): only the reader's own date checks count as date-only ("date read as ...", "date ...
# matches the scan folder year ...", missing_date); a job check that mentions the date ("job X not in the job list; the
# order date fits Y") is a job check and blocks like any other
DATE_FLAG = re.compile(r"^(check: )?(date\b|missing_date$)", re.I)
# Dino 28 Oct 11:20: an MPS order made out to Polymer is a clear MPS order; differences in Att of / Signed by do not
# block (the spreadsheet's values are written) - only Supplier, Job or line count do
POLY_NOTE = re.compile(r"MPS order A\d{5} made out to Polymer .*taken as an MPS order", re.I)
ok_flag = lambda x: bool(DATE_FLAG.search(str(x)) or POLY_NOTE.search(str(x))) or x == "missing_date"
BLOCKING_DIFF = lambda d: d in ("Supplier", "Job") or d.startswith("lines ")
# Dino 28 Oct 16:57: a supplier check is settled when the supplier in the file name and the buying spreadsheet agree
# (A81115: file name AVIC FASTENERS, read as AUIC FASTENER) -> the spreadsheet's supplier, Checked (training data)
SUP_FLAG = re.compile(r"file name says supplier|supplier read as|supplier not read|matched to", re.I)


# Dino 28 Oct 17:07: a job the reader moved from elsewhere on the form (written beside the order number = the
# other-reference box, or an allocation like NLE taken as job NE) is settled when it is a real Sage job and the
# buying spreadsheet doesn't give a different one
JOB_FLAG = re.compile(r"\bjob \S+ taken from the other-reference box|allocation read as .+: taken as job", re.I)
SAGE_JOBS = set()
try:
    import csv as _csv
    for _r in _csv.DictReader(open(ROOT / "jobs_sage.csv", encoding="utf-8-sig")):
        if _r.get("JobNumber"):
            SAGE_JOBS.add(re.sub(r"\s", "", _r["JobNumber"]).upper())
except Exception:
    pass


# Dino 28 Oct 17:16 (IGT 16013, job 31367E): a Kusasa IGT with a real Sage job goes up - IGTs sync on order number +
# job (config SyncIf "IGT"), so it is released to the queue, not ticked; their allocation is the customer / site, so
# "allocation X is not on the list" doesn't apply, nor a supplier read matched to KUSASA or a job box reading ignored
IGT_OK = re.compile(r"allocation .+ is not on the list|matched to KUSASA|job box read as .+: not a job number, ignored", re.I)


def folder_supplier(sc):
    """Dino 28 Oct 17:25: the order's original folder is the supplier (Melyssa\\Orders-MPS\\2024\\V\\VACUUM SERVICES)."""
    parts = [p for p in re.split(r"[\\/]+", str(sc.get("source_folder") or "")) if p.strip()]
    if len(parts) >= 3 and not re.fullmatch(r"\d{4}|[A-Z0-9]|ORDERS?.*|MELYSSA", parts[-1].strip(), re.I):
        return parts[-1].strip()
    return None


def file_supplier(flags, f, sc):
    for x in flags:
        m = re.search(r"file name says supplier (.+?)\s*$", str(x), re.I)
        if m:
            return m.group(1)
    if f.get("supplier_file"):
        return f["supplier_file"]
    m = re.match(r"^\s*(?!PO\d)([A-Z][^-]{2,60}?)\s*-\s*(?:A\d{5}|PL\d{4})", str(sc.get("source_file") or ""), re.I)
    return m.group(1) if m and m.group(1).upper() not in ("PAGE", "UNKNOWN SUPPLIER") else None

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--no-reader-only", action="store_true", help="only orders the spreadsheet confirms")
ap.add_argument("--hold", action="store_true", help="look at out/<type>/hold and release what is clear or checked")
ap.add_argument("--park", action="store_true", help="move unchecked orders that need review from the queue to hold")
a = ap.parse_args()
assert not (a.hold and a.park), "--hold or --park, not both"
HELD = re.compile(r"^held: ")


WAIT_DAYS = 7


def young(f, sc):
    """408: an order with no spreadsheet row waits this long for the row before the reader's values are enough (the
    buying spreadsheet can lag behind the scans). Age from the order date; a date in the future (misread) doesn't wait."""
    d = None
    for v in (f.get("date"), str(sc.get("processed_at") or "")[:10]):
        try:
            d = datetime.strptime(str(v or "")[:10], "%Y-%m-%d").date()
            break
        except ValueError:
            continue
    return d is not None and 0 <= (datetime.now().date() - d).days < WAIT_DAYS


def park(js, t):
    """queue -> hold, JSON first (the PC only syncs JSONs in out/<type>)."""
    with sidecar_lock.locked(js, "po_autocheck park"):
        sc = json.load(open(js, encoding="utf-8"))
        f = sc.get("fields") or {}
        if f.get("checked") or not sc.get("needs_review"):
            return "skipped (changed meanwhile)"
        n = len([x for x in (sc.get("flags") or []) if not HELD.match(str(x))])
        reason = f"review checks ({n})"
        sc["hold_reason"] = reason
        sc["flags"] = [x for x in (sc.get("flags") or []) if not HELD.match(str(x))] + ["held: " + reason]
        sc.setdefault("edits", []).append({"at": stamp, "by": "po_autocheck --park (Dino 28 Oct: unchecked orders to hold)",
                                           "changes": [{"field": "hold_reason", "old": None, "new": reason}]})
        tmp = js.with_name(js.name + ".ac.tmp")
        tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, js)
        D.move_doc(js.parent, js.parent / "hold", js.name, json_last=False)
    return "parked"

fed = set()
for led in (ROOT / "feed_po.csv",):
    if led.exists():
        fed = {r["sha256"].lower() for r in csv.DictReader(open(led, encoding="utf-8"))}
regs = PD.registers()
stamp = datetime.now().isoformat(timespec="seconds")
rows, why_not, block_flags = [], Counter(), Counter()


def _release(js, sc, stamp):
    """hold -> queue, JSON last. Called under the sidecar lock. A name clash leaves the sidecar untouched on hold."""
    if (js.parent.parent / js.name).exists() or (js.parent.parent / str(sc.get("file") or "x/")).exists():
        raise ValueError(f"{js.name} or its PDF is already in the queue")
    old = sc.pop("hold_reason", None)
    sc["flags"] = [x for x in (sc.get("flags") or []) if not HELD.match(str(x))]
    sc["needs_review"] = bool(sc["flags"]) and not (sc.get("fields") or {}).get("checked")
    sc.setdefault("edits", []).append({"at": stamp, "by": "po_autocheck --hold (released)",
                                       "changes": [{"field": "hold_reason", "old": old, "new": None}]})
    tmp = js.with_name(js.name + ".ac.tmp")
    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, js)
    D.move_doc(js.parent, js.parent.parent, js.name, json_last=True)
for t in ("PurchaseOrders", "PolymerOrders"):
    for js in sorted(glob.glob(str(ROOT / "out" / t / ("hold/*.json" if a.hold else "*.json")))):
        js = Path(js)
        try:
            sc = json.load(open(js, encoding="utf-8"))
        except Exception:
            continue
        f = sc.get("fields") or {}
        if sc.get("doc_type") != "po":
            continue
        if a.park:
            if f.get("checked") or not sc.get("needs_review"):
                continue
            res = "parked (dry run)"
            if a.apply:
                try:
                    res = park(js, t)
                except sidecar_lock.Locked:
                    res = "locked (left for next run)"
                except ValueError as e:
                    res = f"not moved: {e}"[:80]
            why_not[res] += 1
            rows.append({"file": js.name, "type": t, "kind": "", "order": f.get("order_number"), "supplier": f.get("supplier"),
                         "backfill": "", "status": "", "diffs": "", "result": res, "why": "",
                         "flags": "; ".join(str(x) for x in (sc.get("flags") or []))[:300]})
            continue
        if f.get("checked") and not a.hold:
            continue
        on, form = str(f.get("order_number") or "").upper(), f.get("form")
        kind = ("MPS" if form == "mps_order" and re.fullmatch(r"A\d{5}", on) else
                "Polymer" if form == "polymer_order" and re.fullmatch(r"PL\d{4}", on) else None)
        if form == "internal_group_transfer" and re.fullmatch(r"\d{5}", on):
            if not a.hold or f.get("checked"):
                continue
            job = re.sub(r"\s", "", str(f.get("job_number") or "")).upper()
            flags_i = [str(x) for x in (sc.get("flags") or []) if not HELD.match(str(x))]
            left = [x for x in flags_i if not (ok_flag(x) or IGT_OK.search(x))]
            res = ("IGT stays: job not a Sage job" if job not in SAGE_JOBS else
                   "IGT stays: " + "; ".join(left)[:60] if left else
                   "IGT cancelled" if f.get("cancelled") else "IGT released (Sage job)")
            if res == "IGT released (Sage job)" and a.apply:
                try:
                    with sidecar_lock.locked(js, "po_autocheck igt"):
                        sc = json.load(open(js, encoding="utf-8"))
                        f2 = sc.setdefault("fields", {})
                        f2["checks"] = [c for c in (f2.get("checks") or []) if not (ok_flag(c) or IGT_OK.search(str(c)))]
                        f2["review"] = "; ".join(f2["checks"]) or None
                        sc["flags"] = [x for x in (sc.get("flags") or []) if not (ok_flag(x) or IGT_OK.search(str(x)))]
                        sc.setdefault("edits", []).append({"at": stamp, "by": "po_autocheck --hold (IGT with a Sage job: released)",
                                                           "changes": [{"field": "hold_reason", "old": sc.get("hold_reason"), "new": None}]})
                        _release(js, sc, stamp)
                except (sidecar_lock.Locked, ValueError) as e:
                    res = f"IGT left on hold: {e}"[:80]
            why_not[res if not res.startswith("IGT stays: ") or res.endswith("Sage job") else "IGT stays: other checks"] += 1
            rows.append({"file": js.name, "type": t, "kind": "IGT", "order": on, "supplier": f.get("supplier"), "backfill": "",
                         "status": "", "diffs": "", "result": res, "why": "", "flags": "; ".join(flags_i)[:300]})
            continue
        backfill = str(sc.get("source_sha256") or "").lower() in fed
        flags = [str(x) for x in (sc.get("flags") or []) if not HELD.match(str(x))]   # "held: ..." is the hold itself
        why = None
        if f.get("checked"):
            kind = kind or "checked"   # ticked Checked (checking file / dashboard) but still on hold: release as it is
        elif not kind:
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
        key = PD.order_key(f, sc.get("source_file") or sc.get("file"))
        reg = PD.pick(regs.get(key) or [], f) if key else None
        fsup, ssup, dsup = file_supplier(flags, f, sc), (reg or {}).get("supplier"), folder_supplier(sc)
        # the supplier in the file name, or the order's folder (17:25: the file name often says KUSASA / PAGE), agrees
        # with the buying spreadsheet
        sup_ok = bool(reg is not None and ssup and ((fsup and PD.same("supplier", fsup, ssup)) or
                                                     (dsup and PD.same("supplier", dsup, ssup))))
        status0, diffs0, _h, _l = PD.compare(f, reg)
        # v8: the spreadsheet must give the job and agree (a blank Job cell confirms nothing): the order stays for the
        # checking file otherwise
        job_ok = bool(reg is not None and (reg or {}).get("job_number") and "Job" not in diffs0
                      and re.sub(r"\s", "", str(f.get("job_number") or "")).upper() in SAGE_JOBS)
        blocking = [x for x in flags if not ok_flag(x) and not (sup_ok and SUP_FLAG.search(x))
                    and not (job_ok and JOB_FLAG.search(x))]
        if not why and blocking and not f.get("checked"):
            why = "review checks"
            for x in blocking:
                block_flags[re.sub(r"\d+", "N", x)[:90]] += 1
        status, diffs, hdr, lines = PD.compare(f, reg)
        bd = [d for d in diffs if BLOCKING_DIFF(d) and not (sup_ok and d == "Supplier")]
        if not why and not f.get("checked") and reg is not None and status == "check" and bd:
            why = "spreadsheet disagrees: " + ", ".join(bd)
        # Dino 16:57: on hold with only date checks (the spreadsheet differing on something the reader didn't flag):
        # back to the queue unchecked, with the spreadsheet's date
        date_only = bool(a.hold and kind in ("MPS", "Polymer") and not f.get("checked") and why
                         and why.startswith("spreadsheet disagrees") and not blocking)
        if not why and not f.get("checked") and reg is None and a.no_reader_only:
            why = "no spreadsheet row (--no-reader-only)"
        if not why and not f.get("checked") and reg is None and young(f, sc):
            why = f"no spreadsheet row yet (waits {WAIT_DAYS} days from the order date)"
        how = None if why else ("already checked" if f.get("checked") else
                                "auto: spreadsheet" if reg is not None else "auto: reader only")
        if how == "auto: spreadsheet" and sup_ok and [x for x in flags if SUP_FLAG.search(x)]:
            how = ("auto: spreadsheet (supplier: file name + spreadsheet)" if fsup and PD.same("supplier", fsup, ssup)
                   else "auto: spreadsheet (supplier: folder + spreadsheet)")
        if how and how.startswith("auto: spreadsheet") and job_ok and [x for x in flags if JOB_FLAG.search(x)]:
            how += " (job: Sage job, spreadsheet agrees)"
        if date_only:
            how, why = "released unchecked (date from spreadsheet)", None
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
                if f.get("checked") and how != "already checked":
                    continue
                if how == "already checked":
                    if a.hold:
                        _release(js, sc, stamp)
                    continue
                if how.startswith("released unchecked"):
                    old = f.get("date")
                    if reg is not None and reg.get("date"):
                        f["date"], f["date_from"] = reg["date"], "spreadsheet"
                    f["checks"] = [c for c in (f.get("checks") or []) if not ok_flag(c)]
                    f["review"] = "; ".join(f["checks"]) or None
                    sc["flags"] = [x for x in (sc.get("flags") or []) if not ok_flag(x)]
                    sc.setdefault("edits", []).append({"at": stamp, "by": "po_autocheck --hold (date only: released)",
                                                       "changes": [{"field": "date", "old": old, "new": f.get("date")}]})
                    _release(js, sc, stamp)
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
                f["checks"] = [c for c in (f.get("checks") or []) if not ok_flag(c)]
                f["review"] = "; ".join(f["checks"]) or None
                sc["flags"] = [x for x in (sc.get("flags") or []) if not ok_flag(x) and not (sup_ok and SUP_FLAG.search(str(x)))
                               and not (job_ok and JOB_FLAG.search(str(x)))]
                f["checks"] = [c for c in (f.get("checks") or []) if not (sup_ok and SUP_FLAG.search(str(c)))
                               and not (job_ok and JOB_FLAG.search(str(c)))]
                f["review"] = "; ".join(f["checks"]) or None
                sc["needs_review"] = bool(sc["flags"])
                sc["checked_at"], sc["checked_how"] = stamp, how
                sc.setdefault("edits", []).append({"at": stamp, "by": f"po_autocheck ({how})", "changes": changes})
                if a.hold:
                    _release(js, sc, stamp)
                else:
                    tmp = js.with_name(js.name + ".ac.tmp")
                    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                    os.replace(tmp, js)
        except sidecar_lock.Locked as e:
            why_not["locked (left for next run)"] += 1
            why_not[how] -= 1
        except ValueError as e:                      # name clash in the queue: written as checked, left on hold
            why_not[f"left on hold: {e}"[:80]] += 1
            why_not[how] -= 1

mode = " hold" if a.hold else " park" if a.park else ""
out = ROOT / "reports" / f"po autocheck{mode} {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
summary = {"applied": a.apply, "mode": mode.strip() or "queue", "orders_looked_at": len(rows),
           "by_kind": {f"{k} {v}": n for (k, v), n in sorted(Counter((r["kind"] or "other", r["result"]) for r in rows).items())},
           "backfill_clear": sum(1 for r in rows if r["backfill"] and r["result"] != "stays"),
           "results": dict(why_not.most_common()), "blocking_checks_top": dict(block_flags.most_common(15)),
           "report": f"{out}.csv"}
json.dump(summary, open(f"{out}.json", "w"), indent=1)
print(json.dumps(summary, indent=1))

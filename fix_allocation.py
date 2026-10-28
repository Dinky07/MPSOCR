#!/usr/bin/env python3
"""Apply the slash rule (mps_jobs) to orders already read: a job number in the allocation box moves to job_number,
payment terms and order methods to their own fields, allocation words are normalised (unknown ones flagged),
VEHICLES orders get the registration into other_po_ref, IAC orders are ordered by Ivor, and the order's date is
checked against the orders numbered around it (orders_index.csv) and corrected when a single digit was misread.

  python3 fix_allocation.py [--root /srv/ocr/scans/pipeline] [--apply] [--synced] [--csv allocation_fixes.csv]

Default: waiting documents (out/PurchaseOrders, out/PolymerOrders and their hold/), dry run. --apply rewrites the
sidecars (and renames the PDF + JSON when the library name changes, as the dashboard does). --synced also lists the
documents already in SharePoint and, with --apply, writes <root>/allocation_fixes.csv (ContentHash, Name, NewName and
the corrected columns) for "01-Build-PurchaseOrders.ps1 -Phase Allocation", which applies them to the library.
"""
import argparse, csv, datetime, glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mps_jobs, po_reader


def original_pdf(root, side):
    """The scan as it arrived (done/<Type>/<yyyy-mm>/<source_file>), for its creation stamp; None when gone."""
    name = side.get("source_file")
    if not name:
        return None
    for t in (side.get("queued_as"), side.get("folder"), "PurchaseOrders", "PolymerOrders"):
        if t:
            hits = glob.glob(os.path.join(root, "done", t, "*", glob.escape(name)))
            if hits:
                return hits[0]
    return None

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--synced", action="store_true")
ap.add_argument("--csv", default=None)
ap.add_argument("--library", default=None, help="po_library.csv from the library build: apply the rule to its rows too "
                "(writes <name>.fixed.csv beside it and adds the SharePoint corrections to the CSV)")
a = ap.parse_args()
po_reader.JOBS_DIR = mps_jobs.JOBS_DIR = a.root

FIELDS = ("order_number", "allocation", "job_number", "payment_terms", "order_method", "other_po_ref", "ordered_by", "signed_by", "date")
COLS = {"order_number": "Title", "allocation": "POAllocation", "job_number": "POJobNumber", "payment_terms": "POPaymentTerms", "order_method": "POOrderMethod",
        "other_po_ref": "POOtherRef", "signed_by": "POSignedBy", "date": "PODate"}
JOB_CHK = ("job ", "job:", "file name names more than one job", "allocation ", "vehicle registration", "VEHICLES order", "date read as")

pats = [f"{a.root}/out/{t}/{h}*.json" for t in ("PurchaseOrders", "PolymerOrders") for h in ("", "hold/")]
if a.synced:
    pats += [f"{a.root}/synced/{t}/*/*.json" for t in ("PurchaseOrders", "PolymerOrders")]
n = flagged = 0
rows = []
for p in sorted(set(x for pat in pats for x in glob.glob(pat))):
    try:
        with open(p, encoding="utf-8-sig") as fh:
            side = json.load(fh)
    except (OSError, ValueError):
        continue
    if side.get("doc_type") != "po" or os.path.exists(p + ".uploaded") and "/out/" in p:
        continue
    f = side.get("fields") or {}
    before = {k: f.get(k) for k in FIELDS}
    d = {k: f.get(k) for k in ("order_number", "polymer_po", "other_po_ref", "allocation", "job_number", "payment_terms",
                                "order_method", "notes", "delivery_address", "signed_by", "writer", "ordered_by", "form")}
    d["date_iso"] = f.get("date")
    d["lines"] = [{"description": i.get("description")} for i in (f.get("items") or []) if isinstance(i, dict)]
    base = re.sub(r"^[A-Z]+__", "", os.path.basename(str(side.get("source_file") or side.get("file") or "")).upper())
    chk = []
    if d.get("date_iso") and d.get("order_number"):
        fixed, changed = mps_jobs.check_order_date(d["order_number"], d["date_iso"], chk,
                                                   scan=mps_jobs.scan_date(side.get("source_folder"), side.get("source_file"), original_pdf(a.root, side),
                                                                           modified=side.get("source_modified"), sha256=side.get("source_sha256")))
        if changed:
            d["date_read"], d["date_iso"], d["date_corrected"] = d["date_iso"], fixed, True
    mps_jobs.apply_allocation(d, chk)
    if not d.get("job_number") and d.get("form") != "polymer_order" and not str(d.get("order_number") or "").startswith("PL"):
        m_ref = re.fullmatch(r"\s*(?:JOB\s*)?0*(\d{4,5})\s?([A-E])\s*", str(d.get("other_po_ref") or "").upper())
        if m_ref:
            d["job_number"], d["other_po_ref"] = m_ref.group(1) + m_ref.group(2), None
            chk.append(f"job {d['job_number']} taken from the other-reference box")
    po_reader.find_job(d, base, chk)
    after = {k: d.get("date_iso" if k == "date" else k) for k in FIELDS}
    if before.get("signed_by") and not after.get("signed_by"):
        after["signed_by"] = before["signed_by"]
    if before.get("job_number") and not after.get("job_number"):
        after["job_number"] = before["job_number"]
    changes = [{"field": k, "old": before[k], "new": after[k]} for k in FIELDS if (before[k] or None) != (after[k] or None)]
    new_chk = [c for c in chk if c not in (f.get("checks") or [])]
    if not changes and not new_chk:
        continue
    n += 1
    flagged += bool(new_chk)
    where = "in SharePoint" if "/synced/" in p else ("held" if "/hold/" in p else "waiting")
    desc = ", ".join(f"{c['field']}: {c['old'] or '-'} -> {c['new'] or '-'}" for c in changes)
    print(f"{os.path.relpath(p, a.root)} [{where}]  {desc}  {'; '.join(new_chk)}")
    if "/synced/" in p and f.get("content_hash"):
        row = {"ContentHash": f["content_hash"], "Name": side.get("file") or os.path.basename(p), "NewName": "", "Note": "; ".join(new_chk)}
        for c in changes:
            if c["field"] in COLS:
                row[COLS[c["field"]]] = c["new"] if c["new"] not in (None, "") else "(clear)"
        rows.append(row)
    if not a.apply:
        continue
    for c in changes:
        f[c["field"]] = c["new"]
    for k in ("job_number_read", "job_customer", "job_name", "allocation_known", "allocation_changes", "date_read", "date_corrected"):
        if d.get(k) is not None:
            f[k] = d[k]
    if f.get("date_corrected"):
        f["date_as_written"] = f.get("date_as_written") or f.get("date_read")
    f["po_number"], f["ordered_by"] = f.get("order_number"), f.get("ordered_by") or f.get("signed_by")
    f["checks"] = [c for c in (f.get("checks") or []) if not str(c).startswith(JOB_CHK)] + chk
    f["review"] = "; ".join(f["checks"]) or None
    side["flags"] = [x for x in (side.get("flags") or []) if not (str(x).startswith("check: ") and str(x)[7:].startswith(JOB_CHK))] + ["check: " + c for c in chk]
    side["needs_review"] = bool(side["flags"])
    side.setdefault("edits", []).append({"at": datetime.datetime.now().isoformat(timespec="seconds"), "by": "fix_allocation.py", "changes": changes})
    # the library name carries the date: rename the pair when it changed (waiting documents only)
    new_js = p
    if "/synced/" not in p and "form_label" in f:
        stem = po_reader.library_name(f, side.get("source_file") or side.get("file") or "document", "")[:150]
        pdf = os.path.join(os.path.dirname(p), str(side.get("file") or ""))
        if stem and os.path.isfile(pdf) and not re.fullmatch(re.escape(stem) + r"(_\d+)?", os.path.splitext(os.path.basename(pdf))[0]):
            ext = os.path.splitext(pdf)[1].lower()
            cand, k = os.path.join(os.path.dirname(p), stem + ext), 2
            while os.path.exists(cand) or os.path.exists(os.path.splitext(cand)[0] + ".json"):
                cand, k = os.path.join(os.path.dirname(p), f"{stem}_{k}{ext}"), k + 1
            os.replace(pdf, cand)
            side["file"] = os.path.basename(cand)
            new_js = os.path.splitext(cand)[0] + ".json"
            if rows and rows[-1]["ContentHash"] == f.get("content_hash"):
                rows[-1]["NewName"] = side["file"]
    tmp = new_js + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(side, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, new_js)
    if new_js != p:
        os.remove(p)
        if os.path.exists(p + ".attempts"):
            os.replace(p + ".attempts", new_js + ".attempts")
    if "/synced/" in p and rows and rows[-1]["ContentHash"] == f.get("content_hash") and "form_label" in f:
        rows[-1]["NewName"] = po_reader.library_name(f, side.get("source_file") or side.get("file") or "document")
if a.library:
    # the archive loaded by 01-Build-PurchaseOrders.ps1 -Phase Load: same rule on its rows (keyed by ContentHash)
    lib_rows, lib_changed, fixed_rows = [], 0, []
    with open(a.library, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        lib_cols = rd.fieldnames
        lib_rows = list(rd)
    for r in lib_rows:
        d = {"order_number": r.get("Title"), "allocation": r.get("POAllocation"), "job_number": r.get("POJobNumber"),
             "payment_terms": r.get("POPaymentTerms"), "order_method": r.get("POOrderMethod"), "other_po_ref": r.get("POOtherRef"),
             "notes": r.get("PONotes"), "delivery_address": r.get("PODeliverTo"), "signed_by": r.get("POSignedBy"),
             "ordered_by": None, "writer": None, "polymer_po": None, "date_iso": r.get("PODate") or None,
             "form": "polymer_order" if str(r.get("Title") or "").upper().startswith("PL") else "mps_order",
             "lines": [{"description": x} for x in str(r.get("POLines") or "").splitlines()]}
        before = {k: d.get("date_iso" if k == "date" else k) for k in FIELDS}
        chk = []
        on = str(d.get("order_number") or "")
        if on in po_reader.EXAMPLE_NUMBERS and re.sub(r"\D", "", on) not in re.sub(r"\D", "", str(r.get("OriginalName") or "")):
            chk.append(f"order number {on} is the example number in the reader's instructions, not read from this "
                       "document: cleared - check what this document is")
            d["order_number"] = None
        if d["date_iso"] and d["order_number"]:
            fixed, changed = mps_jobs.check_order_date(d["order_number"], d["date_iso"], chk,
                                                       scan=mps_jobs.scan_date(r.get("SourceFolder"), r.get("OriginalName"), r.get("LocalPath")))
            if changed:
                d["date_iso"] = fixed
        mps_jobs.apply_allocation(d, chk)
        if not d.get("job_number") and d["form"] != "polymer_order":
            m_ref = re.fullmatch(r"\s*(?:JOB\s*)?0*(\d{4,5})\s?([A-E])\s*", str(d.get("other_po_ref") or "").upper())
            if m_ref:
                d["job_number"], d["other_po_ref"] = m_ref.group(1) + m_ref.group(2), None
                chk.append(f"job {d['job_number']} taken from the other-reference box")
        base = str(r.get("OriginalName") or r.get("TargetName") or "").upper()
        po_reader.find_job(d, base, chk)
        after = {k: d.get("date_iso" if k == "date" else k) for k in FIELDS}
        if before.get("signed_by") and not after.get("signed_by"):
            after["signed_by"] = before["signed_by"]
        if before.get("job_number") and not after.get("job_number"):
            after["job_number"] = before["job_number"]     # not matched to the job list: keep what is there (flagged)
        changes = [{"field": k, "old": before[k], "new": after[k]} for k in FIELDS if (before[k] or None) != (after[k] or None)]
        new_chk = [c for c in chk if c not in str(r.get("POReview") or "")]
        if changes or new_chk:
            lib_changed += 1
            desc = ", ".join(f"{c['field']}: {c['old'] or '-'} -> {c['new'] or '-'}" for c in changes)
            print(f"library {r.get('TargetName') or r.get('Title')}  {desc}  {'; '.join(new_chk)}")
            row = {"ContentHash": r.get("ContentHash"), "Name": r.get("TargetName"), "NewName": "", "Note": "; ".join(new_chk)}
            for c in changes:
                if c["field"] in COLS:
                    row[COLS[c["field"]]] = c["new"] if c["new"] not in (None, "") else "(clear)"
                    r[COLS[c["field"]]] = c["new"] or ""
            if new_chk:
                r["POReview"] = "; ".join([x for x in [str(r.get("POReview") or "")] if x] + new_chk)
                row["POReview"] = r["POReview"]
            if any(c["field"] == "date" for c in changes) and r.get("TargetName"):
                old_name = str(r["TargetName"])
                m_dt = re.search(r"-(\d\d)-(\d\d)-(\d{4})(?=(?:-|\.pdf$))", old_name, re.I)
                y, mo, dd = str(after["date"]).split("-")
                if m_dt:
                    new_name = old_name[:m_dt.start()] + f"-{dd}-{mo}-{y}" + old_name[m_dt.end():]
                    row["NewName"] = new_name
                    r["TargetName"] = new_name
            if row.get("ContentHash"):
                rows.append(row)
    print(f"library: {lib_changed} of {len(lib_rows)} rows to change")
    if a.apply:
        out_lib = os.path.splitext(a.library)[0] + ".fixed.csv"
        extra_cols = [c for c in sorted({k for r in lib_rows for k in r}) if c not in lib_cols]   # e.g. POPaymentTerms
        with open(out_lib, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, list(lib_cols) + extra_cols, quoting=csv.QUOTE_ALL)
            w.writeheader(); w.writerows(lib_rows)
        print(f"corrected library data in {out_lib}")
if (a.synced or a.library) and a.apply and rows:
    out = a.csv or os.path.join(a.root, "allocation_fixes.csv")
    cols = ["ContentHash", "Name", "NewName"] + sorted(set(COLS.values())) + ["POReview", "Note"]
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, cols, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"{len(rows)} SharePoint correction(s) in {out}")
print(f"{n} document(s) {'updated' if a.apply else 'to update (dry run: add --apply)'}, {flagged} with new check lines")

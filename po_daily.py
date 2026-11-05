#!/usr/bin/env python3
"""Daily purchase-order checking cycle (24 Oct 2026, Dino): 03:00 feed (feed_po.py) -> read -> checking file -> Dino
checks during the day -> 19:30 import -> 20:00 training queue.

  watch     (timer mps-po-check, 03:05) wait until today's batch has been read (or 15:00), run the numbers pass on it
            (numbers_pass_run.sh: line quantities / unit prices from the numbers model, Dino 27 Oct), then build the
            checking file
  build     build the checking file now: today's batch, or --all for every unchecked order in the queues
  import    (timer mps-po-import, 19:30) rows marked Use in the checking files -> Checked orders (training data)

Checking file: <root>/po_check/<date> PO checking.xlsx (O:\\pipeline\\po_check). Every order is compared with its row
in the buying spreadsheets (GENERAL BUYING / JOB PURCHASING 2017-2026, <root>/work/registers): the spreadsheet is the
gold standard, so each value has Read / Sheet / Final columns, Final = the spreadsheet's value where it has one, else
the reading. Orders that match their row on every field are pre-marked Use; mismatches and orders with no row come
first. Dino edits Final where needed and marks Use; the import writes the Final values into the order, ticks Checked and
records the change like a dashboard edit. Nothing is synced to SharePoint by this (sync stays manual).
"""
import argparse, csv, difflib, glob, json, os, re, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sidecar_lock                                   # noqa: E402  (review F10: one lock per sidecar for every writer)
ROOT = Path("/srv/ocr/scans/pipeline")
REG_DIR = ROOT / "work" / "registers"
CHECK_DIR = ROOT / "po_check"
WORK = ROOT / "work" / "po_daily"
USE_YES = {"Y", "YES", "X", "1", "TRUE", "OK", "\u2713"}
HDR = [("date", "Date"), ("supplier", "Supplier"), ("job_number", "Job"), ("signed_by", "Signed by"), ("att_of", "Att of")]


# ------------------------------------------------------------------------------------------------ helpers
def _n(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def _num(v):
    try:
        return None if v in (None, "") else round(float(str(v).replace(",", "").replace("R", "")), 2)
    except (TypeError, ValueError):
        return None


def same(field, a, b):
    if a in (None, "") and b in (None, ""):
        return True
    if field in ("supplier", "att_of", "description"):
        x, y = _n(a), _n(b)
        return bool(x and y) and (x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= 0.85)
    if field in ("quantity", "unit_price"):
        return _num(a) == _num(b)
    if field == "signed_by":
        return _n(a)[:4] == _n(b)[:4]
    if field == "job_number":
        return _n(a).lstrip("J") == _n(b).lstrip("J")
    return _n(a) == _n(b)


def registers():
    """Order number -> [records] from the buying spreadsheets; cached in work/po_daily/registers.json."""
    cache = WORK / "registers.json"
    books = sorted(REG_DIR.glob("*.xlsx"))
    if cache.exists() and books and cache.stat().st_mtime >= max(b.stat().st_mtime for b in books):
        return json.load(open(cache, encoding="utf-8"))
    import train_truth as TT
    import subprocess
    tmp = WORK / "registers.tmp.json"
    subprocess.run([sys.executable, TT.__file__, "--out", str(tmp)] + [str(b) for b in books], check=True,
                   capture_output=True, cwd=str(REG_DIR))
    os.replace(tmp, cache)
    return json.load(open(cache, encoding="utf-8"))


def order_key(f, file_name):
    on = str(f.get("order_number") or "").upper().replace(" ", "")
    if on:
        return on
    m = re.search(r"(?<![A-Z0-9])(A\d{5}|PLS?\d{4}|DF\d{4})(?!\d)", str(file_name or "").upper().replace(" ", ""))
    return m.group(1).replace("PLS", "PL") if m else None


def pick(recs, f):
    if not recs:
        return None
    for r in recs:
        if f.get("date") and r.get("date") == f.get("date"):
            return r
    return recs[0]


def sidecars():
    """All purchase-order sidecars waiting in the queues: (path, sidecar)."""
    out = []
    for t in ("PurchaseOrders", "PolymerOrders"):
        for js in sorted(glob.glob(str(ROOT / "out" / t / "*.json")) + glob.glob(str(ROOT / "out" / t / "hold" / "*.json"))):
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if sc.get("doc_type") == "po":
                out.append((Path(js), sc))
    return out


# ------------------------------------------------------------------------------------------------ compare
def compare(f, reg):
    """-> (status, differing fields, header rows, line rows)"""
    diffs, hdr = [], []
    for k, label in HDR:
        a, b = f.get(k), (reg or {}).get(k)
        ok = reg is None or b in (None, "") or same(k, a, b)
        if not ok:
            diffs.append(label)
        hdr.append((k, a, b, b if reg and b not in (None, "") else a, ok))
    ri = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    gi = [i for i in ((reg or {}).get("lines") or []) if isinstance(i, dict)]
    lines = []
    if reg and len(ri) != len(gi):
        diffs.append(f"lines {len(ri)}/{len(gi)}")
    for n in range(max(len(ri), len(gi))):
        x = ri[n] if n < len(ri) else {}
        y = gi[n] if n < len(gi) else {}
        row = []
        for k in ("quantity", "description", "unit_price"):
            a, b = x.get(k), y.get(k)
            ok = not reg or (n < len(gi) and same(k, a, b))
            if reg and not ok and n < len(ri) and n < len(gi):
                diffs.append(f"line {n + 1} {'qty' if k == 'quantity' else 'price' if k == 'unit_price' else 'description'}")
            row.append((k, a, b, b if reg and n < len(gi) else a, ok))
        lines.append((n + 1, row, y.get("price_unit") or x.get("price_unit")))
    if reg is None:
        status = "no spreadsheet row"
    elif not diffs:
        status = "match"
    elif not any(x in ("Date", "Supplier") or x.startswith("lines ") for x in diffs):
        # the row is this order (same number, supplier and date, same number of lines): FINAL = the spreadsheet's values,
        # the differences are the reader's misreadings - pre-marked like a match, and the best training data there is
        status = "sheet confirms"
    else:
        status = "check"
    return status, diffs, hdr, lines



def _append(ws, vals):
    """ws.append, but a value read as "= 21636.18 | ..." stays text (openpyxl would write it as a formula Excel removes)."""
    ws.append(vals)
    for c in ws[ws.max_row]:
        if isinstance(c.value, str) and c.value.startswith("="):
            c.data_type = "s"


# ------------------------------------------------------------------------------------------------ build
def build(day, only_batch=True, out_name=None):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.datavalidation import DataValidation
    regs = registers()
    shas = None
    bpath = WORK / day / "batch.csv"
    if only_batch:
        if not bpath.exists():
            print(f"no batch list for {day} ({bpath})")
            return None
        shas = {r["sha256"].lower() for r in csv.DictReader(open(bpath, encoding="utf-8"))}
    rows = []
    for js, sc in sidecars():
        f = sc.get("fields") or {}
        if f.get("checked"):
            continue
        if shas is not None and str(sc.get("source_sha256") or "").lower() not in shas:
            continue
        key = order_key(f, sc.get("source_file") or sc.get("file"))
        reg = pick(regs.get(key) or [], f) if key else None
        status, diffs, hdr, lines = compare(f, reg)
        rel = js.relative_to(ROOT / "out")
        rows.append({"rel": str(rel).replace("\\", "/"), "sc": sc, "f": f, "key": key, "reg": reg, "status": status,
                     "diffs": diffs, "hdr": hdr, "lines": lines, "hold": js.parent.name == "hold"})
    # no order number read or in the file name: on-hold documents that are not orders (enquiries, quotes, contracts, tax
    # invoices, Polymer scans) - listed on their own tab, not offered for import
    others = [r for r in rows if not r["key"]]
    rows = [r for r in rows if r["key"]]
    order = {"no spreadsheet row": 0, "check": 1, "sheet confirms": 2, "match": 3}
    rows.sort(key=lambda r: (order[r["status"]], r["key"] or "~", r["rel"]))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Orders"
    head = ["Use", "Status", "What differs", "Order", "Open PDF", "On hold"]
    for _, label in HDR:
        head += [f"{label} read", f"{label} sheet", f"{label} FINAL"]
    head += ["Lines read", "Lines sheet", "Total read", "Reader notes", "Ref"]
    _append(ws, head)
    bold, red = Font(bold=True), PatternFill("solid", fgColor="FFE0E0")
    final_fill, use_fill = PatternFill("solid", fgColor="E8F4E8"), PatternFill("solid", fgColor="FFF6D5")
    for c in ws[1]:
        c.font = bold
    for r in rows:
        f = r["f"]
        pdf = str(r["sc"].get("file") or "")
        win = "O:\\pipeline\\out\\" + str(Path(r["rel"]).parent).replace("/", "\\") + "\\" + pdf
        vals = ["Y" if r["status"] in ("match", "sheet confirms") else "", r["status"], ", ".join(r["diffs"]), r["key"], "PDF", "yes" if r["hold"] else ""]
        for k, a, b, fin, ok in r["hdr"]:
            vals += [a, b, fin]
        vals += [len(f.get("items") or []), len((r["reg"] or {}).get("lines") or []) if r["reg"] else None, f.get("order_value"),
                 "; ".join(str(c) for c in (f.get("checks") or []))[:500], r["rel"]]
        _append(ws, vals)
        row = ws.max_row
        ws.cell(row, 5).hyperlink = "file:///" + win.replace("\\", "/")
        ws.cell(row, 5).font = Font(color="0563C1", underline="single")
        ws.cell(row, 1).fill = use_fill
        col = 7
        for k, a, b, fin, ok in r["hdr"]:
            if not ok:
                ws.cell(row, col).fill = red
                ws.cell(row, col + 1).fill = red
            ws.cell(row, col + 2).fill = final_fill
            col += 3
    dv = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"A2:A{max(2, ws.max_row)}")
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = ws.dimensions
    for i, w in enumerate([5, 17, 30, 10, 6, 7] + [11, 11, 11, 22, 22, 22, 9, 9, 9, 10, 10, 10, 14, 14, 14] + [6, 6, 10, 60, 45], 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    wl = wb.create_sheet("Lines")
    _append(wl, ["Order", "Line", "Qty read", "Qty sheet", "Qty FINAL", "Description read", "Description sheet", "Description FINAL",
               "Price read", "Price sheet", "Price FINAL", "Per", "Status", "Ref"])
    for c in wl[1]:
        c.font = bold
    for r in rows:
        for n, cells, per in r["lines"]:
            vals = [r["key"], n]
            for k, a, b, fin, ok in cells:
                vals += [a, b, fin]
            _append(wl, vals + [per, r["status"], r["rel"]])
            row = wl.max_row
            col = 3
            for k, a, b, fin, ok in cells:
                if not ok and r["reg"]:
                    wl.cell(row, col).fill = red
                    wl.cell(row, col + 1).fill = red
                wl.cell(row, col + 2).fill = final_fill
                col += 3
    wl.freeze_panes = "C2"
    wl.auto_filter.ref = wl.dimensions
    for i, w in enumerate([10, 5, 8, 8, 8, 40, 40, 40, 11, 11, 11, 8, 17, 45], 1):
        wl.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    if others:
        wo = wb.create_sheet("No order number")
        _append(wo, ["Open PDF", "On hold", "Supplier read", "Date read", "Reader notes", "Ref"])
        for c in wo[1]:
            c.font = bold
        for r in sorted(others, key=lambda r: r["rel"]):
            pdf = str(r["sc"].get("file") or "")
            win = "O:\\pipeline\\out\\" + str(Path(r["rel"]).parent).replace("/", "\\") + "\\" + pdf
            _append(wo, ["PDF", "yes" if r["hold"] else "", r["f"].get("supplier"), r["f"].get("date"),
                       "; ".join(str(c) for c in (r["f"].get("checks") or []))[:300], r["rel"]])
            wo.cell(wo.max_row, 1).hyperlink = "file:///" + win.replace("\\", "/")
            wo.cell(wo.max_row, 1).font = Font(color="0563C1", underline="single")
        wo.freeze_panes = "B2"
        wo.auto_filter.ref = wo.dimensions
        for i, w in enumerate([6, 7, 30, 11, 80, 60], 1):
            wo.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    sm = wb.create_sheet("How to check", 0)
    cnt = {s: sum(1 for r in rows if r["status"] == s) for s in order}
    for line in [
        f"Purchase orders read {day}: {len(rows)} orders",
        f"  {cnt['no spreadsheet row']:5}  no row in the buying spreadsheets              - top of the Orders tab: check against the PDF",
        f"  {cnt['check']:5}  date, supplier or number of lines differ from the row - next: is it the right row? (red = disagree)",
        f"  {cnt['sheet confirms']:5}  the row is this order; other values were misread - FINAL = spreadsheet, pre-marked Use = Y",
        f"  {cnt['match']:5}  match their row on every field                     - pre-marked Use = Y",
        "",
        "1. Orders tab: for each order, check the FINAL columns (green). FINAL = the spreadsheet's value where it has one,",
        "   otherwise what the reader read. Change a FINAL value if it is wrong; open the PDF with the PDF link.",
        "2. Lines tab: the same for every line (FINAL qty / description / price).",
        "3. Mark Use = Y for every order that is right (untick a pre-marked one with N or blank).",
        "4. Save the file where it is. At 19:30 the orders marked Y are written into the pipeline as Checked (training data)",
        "   and the 20:00 training queue learns from them. Nothing goes to SharePoint until you say a batch is complete.",
        "",
        "Leave the Ref column alone: it is how the import finds each order.",
        "",
        f"No order number tab: {len(others)} documents with no order number (mostly on hold: enquiries, quotes, contracts,",
        "   tax invoices). They are not imported from this file; they are listed so you can see what is sitting in hold.",
    ]:
        sm.append([line])
    sm.column_dimensions["A"].width = 120
    sm["A1"].font = Font(bold=True, size=13)
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    out = CHECK_DIR / (out_name or f"{day} PO checking.xlsx")
    tmp = out.with_name("~tmp-" + out.name)
    wb.save(tmp)
    os.replace(tmp, out)
    try:
        os.chmod(out, 0o664)
    except OSError:
        pass
    print(f"{out}: {len(rows)} orders ({cnt['no spreadsheet row']} no row, {cnt['check']} to check, "
          f"{cnt['sheet confirms']} confirmed by the sheet, {cnt['match']} match); {len(others)} with no order number on their own tab")
    return out


def watch(day, deadline_hour=15, poll=300):
    """Wait until every file of today's batch has been read (a sidecar with its SHA-256), then build."""
    bpath = WORK / day / "batch.csv"
    if not bpath.exists():
        print(f"no batch for {day}: nothing to watch")
        return
    shas = {r["sha256"].lower() for r in csv.DictReader(open(bpath, encoding="utf-8"))}
    while True:
        done = set()
        for t in ("PurchaseOrders", "PolymerOrders"):
            for js in glob.glob(str(ROOT / "out" / t / "*.json")) + glob.glob(str(ROOT / "out" / t / "hold" / "*.json")) + \
                    glob.glob(str(ROOT / "failed" / "**" / "*.json"), recursive=True):
                try:
                    h = str(json.load(open(js, encoding="utf-8")).get("source_sha256") or "").lower()
                except (OSError, ValueError):
                    continue
                if h in shas:
                    done.add(h)
        left = len(shas - done)
        print(f"{datetime.now():%H:%M} read {len(done)} of {len(shas)}", flush=True)
        if not left or datetime.now().hour >= deadline_hour:
            break
        time.sleep(poll)
    numbers_pass(day)
    autocheck()
    build(day)


def autocheck():
    """408 (Dino 2 Nov): the day's clear orders are checked as soon as the batch is read, not only at 19:30 (queue, then
    hold; same rules - an order with no spreadsheet row waits 7 days for it)."""
    import subprocess
    try:                                                 # 498: own order forms read as "not an order", first
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().with_name("ownform_fix.py")), "--queue", "--apply"],
                           capture_output=True, text=True, timeout=1800)
        print(f"ownform_fix: rc={r.returncode} {(r.stdout.strip().splitlines() or [''])[0][:200]}", flush=True)
    except Exception as ex:
        print(f"ownform_fix: {ex}", flush=True)
    for args in (["--apply"], ["--hold", "--apply"]):
        try:
            r = subprocess.run([sys.executable, str(Path(__file__).resolve().with_name("po_autocheck.py"))] + args,
                               capture_output=True, text=True, timeout=1800)
            rep = [l for l in r.stdout.splitlines() if '"report"' in l]
            print(f"autocheck {' '.join(args)}: rc={r.returncode} {rep[-1].strip() if rep else ''}", flush=True)
        except Exception as ex:
            print(f"autocheck {' '.join(args)}: {ex}", flush=True)


def numbers_pass(day):
    """Line quantities and unit prices from the fine-tuned numbers model before the checking file is written (Dino 27 Oct).
    Never stops the checking file: a failure leaves the main reader's values. Off with flags/numbers_pass_off."""
    import subprocess
    sh = Path(__file__).resolve().with_name("numbers_pass_run.sh")
    if not sh.exists() or (ROOT / "flags" / "numbers_pass_off").exists():
        print("numbers pass: not run (script missing or switched off)", flush=True)
        return
    t = time.time()
    try:
        r = subprocess.run(["bash", str(sh), day], timeout=5 * 3600)
        print(f"numbers pass: finished rc={r.returncode} in {(time.time() - t) / 60:.0f} min", flush=True)
    except Exception as ex:
        print(f"numbers pass: {ex}", flush=True)


# ------------------------------------------------------------------------------------------------ import
def do_import(apply):
    import openpyxl
    sys.path.insert(0, "/srv/ocr/pipeline")
    import dashboard as D
    state_p = WORK / "imported.json"
    state = json.load(open(state_p, encoding="utf-8")) if state_p.exists() else {}
    tot = {"orders": 0, "imported": 0, "already": 0, "gone": 0, "checked_before": 0}
    skipped_files = []
    for xl in sorted(CHECK_DIR.glob("*PO checking*.xlsx")):
        if xl.name.startswith(("~", ".")):
            continue
        try:
            wb = openpyxl.load_workbook(xl, data_only=True)
            ws, wl = wb["Orders"], wb["Lines"]
        except Exception as e:                        # review F9 (28 Oct): one unreadable file (an Excel autofilter openpyxl
            print(f"SKIPPED {xl.name}: {str(e)[:160]}")   # cannot parse, 24 Oct) stopped every later file's import
            skipped_files.append(xl.name)
            continue
        head = [c.value for c in ws[1]]
        ix = {h: i for i, h in enumerate(head)}
        lines = {}
        lhead = [c.value for c in wl[1]]
        lx = {h: i for i, h in enumerate(lhead)}
        for row in wl.iter_rows(min_row=2, values_only=True):
            if row[lx["Ref"]]:
                lines.setdefault(row[lx["Ref"]], []).append(row)
        done = state.setdefault(xl.name, {})
        for row in ws.iter_rows(min_row=2, values_only=True):
            ref = row[ix["Ref"]]
            if not ref:
                continue
            tot["orders"] += 1
            if str(row[ix["Use"]] or "").strip().upper() not in USE_YES:
                continue
            if ref in done:
                tot["already"] += 1
                continue
            js = ROOT / "out" / ref
            if not js.exists():
                alt = [p for p in (ROOT / "out").glob("*/*" + Path(ref).name) if p.is_file()] + \
                      [p for p in (ROOT / "out").glob("*/hold/" + Path(ref).name) if p.is_file()]
                js = alt[0] if alt else None
            if not js:
                tot["gone"] += 1                      # synced or deleted since the file was made
                continue
            try:
                lk = sidecar_lock.locked(js, "po_import")
                lk.__enter__()
            except sidecar_lock.Locked as ex:
                print(f"  {ref}: {ex} - left for the next run")
                tot["locked"] = tot.get("locked", 0) + 1
                continue
            try:
                sc = json.load(open(js, encoding="utf-8"))
                f = sc.setdefault("fields", {})
                if "fields_read" not in sc and not f.get("checked"):   # the reader's own reading, for training (26 Oct)
                    sc["fields_read"] = json.loads(json.dumps(f))
                if f.get("checked"):
                    tot["checked_before"] += 1
                    done[ref] = "checked before"
                    continue
                changes = []
                for k, label in HDR:
                    new = row[ix[f"{label} FINAL"]]
                    if k == "date" and isinstance(new, datetime):
                        new = new.date().isoformat()
                    new = None if new in ("", None) else (str(new).strip() if not isinstance(new, (int, float)) else new)
                    if (f.get(k) if f.get(k) != "" else None) != new:
                        changes.append({"field": k, "old": f.get(k), "new": new})
                        f[k] = new
                old_items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
                new_items = []
                for lr in sorted(lines.get(ref, []), key=lambda r: r[lx["Line"]] or 0):
                    q, d, p = lr[lx["Qty FINAL"]], lr[lx["Description FINAL"]], lr[lx["Price FINAL"]]
                    if q in (None, "") and d in (None, "") and p in (None, ""):
                        continue
                    n = (lr[lx["Line"]] or 1) - 1
                    base = dict(old_items[n]) if 0 <= n < len(old_items) else {}
                    base.update(quantity=_num(q) if _num(q) is not None else q, description=None if d is None else str(d).strip(),
                                unit_price=_num(p))
                    if lr[lx["Per"]]:
                        base["price_unit"] = lr[lx["Per"]]
                    new_items.append(base)
                sig = lambda its: [[i.get("quantity"), i.get("description"), i.get("unit_price")] for i in its]
                if sig(new_items) != sig(old_items):
                    changes.append({"field": "items", "old": len(old_items), "new": len(new_items)})
                    f["items"] = new_items
                D._recompute("po", f)
                f["checked"] = True
                if apply:
                    sc["checked_at"] = datetime.now().isoformat(timespec="seconds")
                    sc.setdefault("edits", []).append({"at": sc["checked_at"], "by": f"checking file {xl.name}", "changes": changes})
                    tmp = js.with_name(js.name + ".tmp")
                    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                    os.replace(tmp, js)
                    done[ref] = datetime.now().isoformat(timespec="seconds")
                tot["imported"] += 1
            finally:
                lk.__exit__(None, None, None)
    if apply:
        tmp = state_p.with_suffix(".tmp")
        json.dump(state, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, state_p)
    print(("IMPORTED" if apply else "DRY RUN") + f": {tot['imported']} orders marked Use written as Checked; "
          f"{tot['already']} done on an earlier day, {tot['checked_before']} were checked already, "
          f"{tot['gone']} no longer in the queue (synced?); {tot['orders']} rows read")


def backfill_read(apply):
    """Orders imported before 26 Oct lost the reader's own reading (the import wrote the checked values over it). Rebuild
    "fields_read" from the checking files: header fields from the import's recorded changes (old values), lines from the
    Lines sheet's "read" columns. Orders anywhere in the pipeline (queue, hold, synced)."""
    import openpyxl
    state = json.load(open(WORK / "imported.json", encoding="utf-8")) if (WORK / "imported.json").exists() else {}
    where = {}
    for pat in ("out/PurchaseOrders/*.json", "out/PurchaseOrders/hold/*.json", "synced/PurchaseOrders/*/*.json"):
        for p in ROOT.glob(pat):
            where.setdefault(p.name, p)
    tot = {"imported": 0, "had_read": 0, "rebuilt": 0, "items_differ": 0, "missing": 0}
    for xl in sorted(CHECK_DIR.glob("*PO checking*.xlsx")):
        if xl.name.startswith(("~", ".")) or not state.get(xl.name):
            continue
        wb = openpyxl.load_workbook(xl, read_only=True, data_only=True)
        wl = wb["Lines"]
        rows = list(wl.iter_rows(values_only=True))
        lx = {h: i for i, h in enumerate(rows[0])}
        lines = {}
        for r in rows[1:]:
            if r[lx["Ref"]]:
                lines.setdefault(r[lx["Ref"]], []).append(r)
        for ref, when in state[xl.name].items():
            if when == "checked before":
                continue
            tot["imported"] += 1
            js = where.get(Path(ref).name)
            if not js:
                tot["missing"] += 1
                continue
            sc = json.load(open(js, encoding="utf-8"))
            if "fields_read" in sc:
                tot["had_read"] += 1
                continue
            f = sc.get("fields") or {}
            rd = json.loads(json.dumps(f))
            rd.pop("checked", None)
            for e in sc.get("edits") or []:
                if str(e.get("by", "")).startswith("checking file"):
                    for ch in e.get("changes") or []:
                        if ch.get("field") not in ("items",) and ch.get("field") in rd:
                            rd[ch["field"]] = ch.get("old")
            old = [i for i in (f.get("items") or []) if isinstance(i, dict)]
            new_items = []
            for lr in sorted(lines.get(ref, []), key=lambda r: r[lx["Line"]] or 0):
                q, d, pr = lr[lx["Qty read"]], lr[lx["Description read"]], lr[lx["Price read"]]
                if q in (None, "") and d in (None, "") and pr in (None, ""):
                    continue                                   # a line the reader didn't read
                n = (lr[lx["Line"]] or 1) - 1
                base = dict(old[n]) if 0 <= n < len(old) else {}
                base.update(quantity=_num(q) if _num(q) is not None else q, description=None if d is None else str(d).strip(),
                            unit_price=_num(pr))
                new_items.append(base)
            if lines.get(ref):
                rd["items"] = new_items
            sig = lambda its: [[i.get("quantity"), i.get("unit_price")] for i in its or []]
            tot["items_differ"] += sig(rd.get("items")) != sig(f.get("items"))
            tot["rebuilt"] += 1
            if apply:
                sc["fields_read"] = rd
                tmp = js.with_name(js.name + ".tmp")
                tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                os.replace(tmp, js)
    print(("REBUILT" if apply else "DRY RUN") + f": {tot['rebuilt']} imported orders given the reader's own reading back "
          f"({tot['items_differ']} with line quantities or prices the reader got wrong); {tot['had_read']} had it already; "
          f"{tot['missing']} not found; {tot['imported']} imported orders in the checking files")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["watch", "build", "import", "backfill-read"])
    ap.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--all", action="store_true", help="build: every unchecked order in the queues, not just today's batch")
    ap.add_argument("--apply", action="store_true", help="import: write (default is a dry run)")
    a = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    if a.command == "watch":
        watch(a.day)
    elif a.command == "build":
        build(a.day, only_batch=not a.all, out_name=f"{a.day} PO checking (all unchecked).xlsx" if a.all else None)
    elif a.command == "backfill-read":
        backfill_read(a.apply)
    else:
        do_import(a.apply)


if __name__ == "__main__":
    main()

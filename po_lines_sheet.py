#!/usr/bin/env python3
"""467 (Dino 4 Nov "from the Purchase Order out queue and the MPS purchase order spreadsheets, a large corrected training
sheet on qty, pricing and descriptions"). Every MPS order the reader has read (Purchase Orders: queue, hold and
synced), one row per order line: what the reader read against that order's lines in the buying spreadsheets
(GENERAL BUYING / JOB PURCHASING, work/po_daily/registers.json), with a FINAL value = the spreadsheet's where the line
is the same item. Read-only on the documents; writes
  training/po_lines/PO lines training <date>.xlsx   for Dino: Use / FINAL columns, the PDF one click away
  training/po_lines/po_lines_<date>.json            the same, for the training jobs (Use rows only)

The reader's own reading is fields_read when the order was corrected (checking file, autocheck, dashboard), else fields.
Lines are paired in order where the counts agree; otherwise by description (an extra or missing line on either side).
A line is "same item" when its descriptions are alike (>= 0.5) or, with equal counts, the qty or price agrees.
Status per line:
  match        read = sheet on qty, price and description
  corrected    the sheet corrects qty / price / description; nothing suspicious -> Use = Y
  check        the sheet differs in a way that may not be a misreading: price x1.15 (VAT), price = qty x price (a line
               total), price per another unit, description quite different, sheet value blank -> Use blank
  read only    a line the sheet does not have (or the sheet has a line the reader missed: "sheet only")
Orders whose spreadsheet row names another supplier, or whose date is more than 60 days off, are left out (wrong row).

    python po_lines_sheet.py [--root O:/pipeline] [--out DIR]
"""
import argparse, difflib, glob, json, os, re, sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path


def _n(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def num(v):
    try:
        return None if v in (None, "") else round(float(str(v).replace(",", "").replace("R", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def sim(a, b):
    x, y = _n(a), _n(b)
    if not x or not y:
        return 0.0
    if x in y or y in x:
        return max(0.9, difflib.SequenceMatcher(None, x, y).ratio())
    return difflib.SequenceMatcher(None, x, y).ratio()


def same_supplier(a, b):
    x, y = _n(a), _n(b)
    return bool(x and y) and (x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= 0.7)


def days_apart(a, b):
    try:
        return abs((date.fromisoformat(str(a)[:10]) - date.fromisoformat(str(b)[:10])).days)
    except ValueError:
        return None


def digits(v):
    """1131.6 -> "11316", 11.31 -> "1131", 260 -> "260": the digits as written, for spotting a misread digit or point."""
    s = f"{float(v):.2f}"
    s = s[:-3] if s.endswith(".00") else s[:-1] if s.endswith("0") else s
    return s.replace(".", "").lstrip("0") or "0"


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def misread(a, b):
    """Two numbers that differ the way a misreading does: a digit or the decimal point (R11-31 read 1131.6, a dropped 1)."""
    if a is None or b is None:
        return False
    x, y = digits(a), digits(b)
    return lev(x, y) <= (2 if max(len(x), len(y)) >= 4 else 1)


UNIT = {"ea": "each", "pkg": "kg", "plg": "kg", "plug": "kg", "p/kg": "kg", "/l": "litre", "plen": "length", "pm": "metre", "m²": "m2"}


def unit(u):
    u = str(u or "").strip().lower()
    return UNIT.get(u, u) or None


def date_final(read, sheet):
    """(FINAL date, verified, why) for the order date: the sheet's where the two agree or differ the way a misreading does
    (only the year: 2005 for 2025; or a day / month digit 1, 4 or 7 for another of them - her 4 is an L with a flick,
    her 1 a plain stroke); otherwise the reading stays and the date is unverified (the sheet may carry another date,
    e.g. the day it was entered)."""
    r, s = str(read or "")[:10], str(sheet or "")[:10]
    if not s:
        return r or None, False, "no date on the sheet"
    if not r:
        return s, True, "date not read"
    if r == s:
        return s, True, ""
    if r[5:] == s[5:]:
        return s, True, "year misread"
    a, b = r[8:10] + r[5:7], s[8:10] + s[5:7]
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    if r[:4] in (s[:4], s[:2] + "0" + s[3]) and len(diff) == 1 and set(diff[0]) <= set("147"):
        return s, True, "day / month digit misread"
    return r, False, f"sheet says {s}"


def pair_score(x, y):
    s = sim(x.get("description"), y.get("description"))
    q = num(x.get("quantity")), num(y.get("quantity"))
    p = num(x.get("unit_price")), num(y.get("unit_price"))
    if q[0] is not None and q[0] == q[1] and p[0] is not None and p[0] == p[1]:
        s = max(s, 0.8)
    elif (q[0] is not None and q[0] == q[1]) or misread(*p):
        s = max(s, 0.55 if s >= 0.25 else s)
    return s


def align(ri, gi):
    """[(read line or None, sheet line or None)] in order: a DP on how alike the lines are (description, qty, price).
    Equal counts where the DP pairs fewer than half: by position."""
    n, m = len(ri), len(gi)
    S = [[pair_score(ri[i], gi[j]) for j in range(m)] for i in range(n)]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            take = best[i + 1][j + 1] + (S[i][j] if S[i][j] >= 0.5 else -1)
            best[i][j] = max(take, best[i + 1][j], best[i][j + 1])
    out, i, j = [], 0, 0
    while i < n and j < m:
        if S[i][j] >= 0.5 and best[i][j] == best[i + 1][j + 1] + S[i][j]:
            out.append((ri[i], gi[j])); i += 1; j += 1
        elif best[i][j] == best[i + 1][j]:
            out.append((ri[i], None)); i += 1
        else:
            out.append((None, gi[j])); j += 1
    out += [(ri[k], None) for k in range(i, n)] + [(None, gi[k]) for k in range(j, m)]
    if n == m and sum(1 for x, y in out if x and y) < n / 2:
        return list(zip(ri, gi))
    return out


def judge(x, y, equal_counts):
    """-> (status, what differs, notes, final qty, final price, final description, desc likeness)"""
    if x is None:
        return "sheet only", "line not read", "", num(y.get("quantity")), num(y.get("unit_price")), y.get("description"), None
    if y is None:
        return "read only", "not in the sheet", "", None, None, None, None
    qr, qs = num(x.get("quantity")), num(y.get("quantity"))
    pr, ps = num(x.get("unit_price")), num(y.get("unit_price"))
    dr, ds = x.get("description"), y.get("description")
    s = sim(dr, ds)
    q_ok, p_ok = qr == qs, pr == ps
    numbers_agree = (q_ok or misread(qr, qs)) and (p_ok or misread(pr, ps)) and (qs is not None or ps is not None)
    same_item = s >= 0.5 or numbers_agree or (equal_counts and (q_ok or p_ok) and (qr is not None or pr is not None))
    diffs, notes = [], []
    if not q_ok:
        diffs.append("qty")
    if not p_ok:
        diffs.append("price")
    if s < 0.85:
        diffs.append("description")
    if not diffs:
        return "match", "", "", qs, ps, ds, round(s, 2)
    if s < 0.8 and same_item:
        # the sheet's wording is often the buyer's shorthand ("FF SO" for FLATFACE SLIP ON on the page): not a transcription,
        # so the description stays as read and is marked unverified; the line still counts for qty / price
        notes_desc = "description as read (the sheet words it differently)"
    else:
        notes_desc = ""
    if not same_item:
        notes.append("not the same item?")
    if qs is None and ps is None and (qr is not None or pr is not None):
        # a note / continuation row on the sheet (no numbers): the reader's numbers stay unverified
        return "unverified", ", ".join(diffs), "sheet line has no qty or price", qr, pr, (ds if s >= 0.6 else dr), round(s, 2)
    if qs is None and qr is not None:
        notes.append("sheet has no qty")
    if ps is None and pr is not None:
        notes.append("sheet has no price")
    if pr and ps and not p_ok and not misread(pr, ps):
        r = ps / pr
        if abs(r - 1.15) < 0.006 or abs(r - 1 / 1.15) < 0.006:
            notes.append("price differs by VAT (x1.15)")
        elif qs and qs > 1 and (abs(ps - pr * qs) < 0.02 or abs(pr - ps * qs) < 0.02):
            notes.append("price looks like a line total")
        else:
            notes.append(f"price {r:.2f}x the reading, not a misread digit")
            if unit(x.get("price_unit")) and unit(y.get("price_unit")) and unit(x.get("price_unit")) != unit(y.get("price_unit")):
                notes.append(f"per {unit(y.get('price_unit'))} on the sheet, {unit(x.get('price_unit'))} read")
    if qr and qs and not q_ok and not misread(qr, qs):
        notes.append(f"qty {qr:g} read, {qs:g} on the sheet, not a misread digit")
    fq = qs if qs is not None else qr
    fp = ps if ps is not None else pr
    fd = ds if (ds and same_item and s >= 0.8) else dr
    st = "check" if notes else "corrected"
    if notes_desc:
        notes.append(notes_desc)
        if st == "corrected" and diffs == ["description"]:
            st = "match"                      # qty and price agree; only the sheet's wording differs
    return st, ", ".join(diffs), "; ".join(notes), fq, fp, fd, round(s, 2)


STABLE = "PO lines training.xlsx"
JOBS = set()                      # 503: the Sage job list (jobs_sage.csv, from MPSBusiness)
AUTO_COLS = [("Auto Use", "auto_use", 4), ("Auto qty", "auto_qty", 4), ("Auto price", "auto_price", 4),
             ("Auto description", "auto_desc", 4), ("Auto description verified", "auto_desc_ok", 4)]


def yn(v):
    s = str(v or "").strip().upper()
    return "Y" if s in ("Y", "YES", "X", "1", "TRUE", "OK") else "N" if s.startswith("N") else ""


def txt(v):
    return str(v).strip() if v not in (None, "") else None


def load_edits(p, out):
    """(hash, line) -> the visible values Dino set, where they differ from what the builder wrote (the hidden Auto columns;
    for the first sheet of 4 Nov, which has none, the builder's JSON of that day)."""
    import openpyxl
    try:
        ws = openpyxl.load_workbook(p, read_only=True, data_only=True)["Lines"]
    except Exception as e:
        print(f"  {p.name}: not read ({e})", flush=True)
        return {}
    it = ws.iter_rows(values_only=True)
    head = list(next(it, []))
    I = {k: i for i, k in enumerate(head) if k}
    if "Hash" not in I:
        return {}
    auto_json = None
    if "Auto Use" not in I:
        m = re.search(r"(\d{4}-\d\d-\d\d)", p.name)
        jp = out / f"po_lines_{m.group(1)}.json" if m else None
        if not jp or not jp.exists():
            return {}
        auto_json = {}
        for o in json.load(open(jp, encoding="utf-8"))["orders"]:
            for k, l in enumerate(o["lines"], 1):
                auto_json[(str(o["hash"]), k)] = ("Y" if l["use"] else "", num(l["quantity"]), num(l["unit_price"]),
                                                  txt(l["description"]), "Y" if l["description_verified"] else "")
    edits = {}
    for r in it:
        if not r or not r[I["Hash"]]:
            continue
        key = (str(r[I["Hash"]]), int(r[I["Line"]] or 0))
        vis = (yn(r[I["Use"]]), num(r[I["Qty FINAL"]]), num(r[I["Price FINAL"]]), txt(r[I["Description FINAL"]]), yn(r[I["Description verified"]]))
        if auto_json is None:
            auto = (yn(r[I["Auto Use"]]), num(r[I["Auto qty"]]), num(r[I["Auto price"]]), txt(r[I["Auto description"]]),
                    yn(r[I["Auto description verified"]]))
        else:
            auto = auto_json.get(key)
            if auto is None:
                continue
        if vis != auto:
            edits[key] = {"vis": vis, "check": (num(r[I["Qty read"]]), num(r[I["Price read"]]), num(r[I["Qty sheet"]]), num(r[I["Price sheet"]]))}
    print(f"  {p.name}: {len(edits)} lines changed by Dino", flush=True)
    return edits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
    ap.add_argument("--out", default=None)
    ap.add_argument("--link-root", default="O:\\pipeline", help="how the PDF links start on Dino's PC")
    a = ap.parse_args()
    root = Path(a.root)
    out = Path(a.out) if a.out else root / "training" / "po_lines"
    out.mkdir(parents=True, exist_ok=True)
    regs = json.load(open(root / "work" / "po_daily" / "registers.json", encoding="utf-8"))
    import csv
    JOBS.update(_n(r.get("JobNumber")) for r in csv.DictReader(open(root / "jobs_sage.csv", encoding="utf-8-sig")))
    try:                                             # and the Job File list on SharePoint (jobs_jobfile.csv)
        for r in csv.DictReader(open(root / "jobs_jobfile.csv", encoding="utf-8-sig")):
            JOBS.add(_n(r.get("JobNumber") or r.get("Title") or r.get("Job")))
    except OSError:
        pass
    seen, rows, orders, cnt = set(), [], [], Counter()
    pats = [("queue", "out/PurchaseOrders/*.json"), ("hold", "out/PurchaseOrders/hold/*.json"), ("synced", "synced/PurchaseOrders/*/*.json")]
    for where, pat in pats:
        for js in sorted(glob.glob(str(root / pat))):
            if js.endswith((".deleted.json", ".rerouted.json")):
                continue
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            f = sc.get("fields") or {}
            if sc.get("doc_type") != "po" or f.get("form") != "mps_order":
                continue
            on = str(f.get("order_number") or "").upper().replace(" ", "")
            if not re.fullmatch(r"A\d{5}", on):
                continue
            h = f.get("content_hash") or sc.get("source_sha256") or js
            if h in seen:
                cnt["duplicate scan"] += 1
                continue
            seen.add(h)
            cnt["orders"] += 1
            recs = regs.get(on) or []
            if not recs:
                cnt["no spreadsheet row"] += 1
                continue
            rd = sc.get("fields_read") or f
            sup = rd.get("supplier") or f.get("supplier")
            # the row for this order: same supplier; of several, the nearest date (the reader's year is often wrong, so
            # the date alone never rules a row out)
            cand = [r for r in recs if same_supplier(sup, r.get("supplier")) or same_supplier(f.get("supplier"), r.get("supplier"))]
            if not cand:
                cnt["sheet row names another supplier: left out"] += 1
                continue
            reg = min(cand, key=lambda r: days_apart(f.get("date") or rd.get("date"), r.get("date")) or 0)
            ri = [i for i in (rd.get("items") or []) if isinstance(i, dict)]
            gi = [i for i in (reg.get("lines") or []) if isinstance(i, dict) and (i.get("description") or i.get("quantity") or i.get("unit_price"))]
            if not ri and not gi:
                continue
            cnt["orders compared"] += 1
            pdf = Path(js).with_name(str(sc.get("file") or ""))
            rel = str(pdf.relative_to(root)).replace("/", "\\")
            pairs = align(ri, gi)
            o_lines, o_ok = [], True
            d_final, d_ok, d_why = date_final(rd.get("date"), reg.get("date"))
            # 503: the job - the order's (the file name's where it has one), verified when every job is in the Sage job list
            jf = f.get("job_number")
            j_ok = bool(jf) and all(_n(x) in JOBS for x in str(jf).split(","))
            cnt["order date " + ("verified" if d_ok else "unverified")] += 1
            for k, (x, y) in enumerate(pairs, 1):
                st, what, note, fq, fp, fd, s = judge(x, y, len(ri) == len(gi))
                use = "Y" if st in ("match", "corrected") else ""
                o_ok &= bool(use)
                x, y = x or {}, y or {}
                row = {"use": use, "status": st, "differs": what, "notes": note, "order": on, "supplier": reg.get("supplier") or sup,
                       "date": reg.get("date") or f.get("date"), "line": k, "lines_read": len(ri), "lines_sheet": len(gi),
                       "qty_read": num(x.get("quantity")), "qty_sheet": num(y.get("quantity")), "qty_final": fq,
                       "price_read": num(x.get("unit_price")), "price_sheet": num(y.get("unit_price")), "price_final": fp,
                       "per_read": x.get("price_unit"), "per_sheet": y.get("price_unit") or y.get("per"),
                       "desc_read": x.get("description"), "desc_sheet": y.get("description"), "desc_final": fd, "desc_alike": s,
                       "desc_ok": "Y" if (s is not None and s >= 0.8) else "",
                       "where": where, "book": f"{reg.get('book')} / {reg.get('tab')} row {reg.get('row')}", "pdf": rel,
                       "hash": h, "corrected_before": bool(sc.get("fields_read")), "checked_how": sc.get("checked_how") or "",
                       "supplier_read": rd.get("supplier_read") or sup, "date_read": rd.get("date_read") or rd.get("date"), "date_final": d_final, "date_ok": "Y" if d_ok else "",
                       "job_read": rd.get("job_number_read") or rd.get("job_number"), "job_final": jf if j_ok else None,
                       "job_ok": "Y" if j_ok else "",
                       "date_why": d_why}
                rows.append(row)
                o_lines.append(row)
                cnt["line: " + st] += 1
            orders.append({"order": on, "hash": h, "pdf": rel, "sidecar": str(Path(js).relative_to(root)).replace("\\", "/"),
                           "supplier": reg.get("supplier"), "date": reg.get("date"), "_rows": o_lines})
    # Dino's changes in the sheet (Use, FINAL, Description verified) are carried into the new one, line by line
    edits = {}
    for p in sorted(list(out.glob("PO lines training*.xlsx")), key=lambda p: p.stat().st_mtime):
        if not p.name.startswith("~$"):
            edits.update(load_edits(p, out))
    applied = stale = 0
    for r in rows:
        r.update(auto_use=r["use"], auto_qty=r["qty_final"], auto_price=r["price_final"], auto_desc=r["desc_final"],
                 auto_desc_ok=r["desc_ok"], edited="")
        e = edits.get((str(r["hash"]), r["line"]))
        if not e:
            continue
        if e["check"] != (r["qty_read"], r["price_read"], r["qty_sheet"], r["price_sheet"]):
            stale += 1
            r["notes"] = (r["notes"] + "; " if r["notes"] else "") + "your earlier change not carried over: the reading or the sheet changed"
            continue
        r["use"], r["qty_final"], r["price_final"], r["desc_final"], r["desc_ok"] = e["vis"]
        r["edited"] = "Y"
        applied += 1
    if applied or stale:
        cnt["lines changed by Dino, carried over"] = applied
        cnt["lines changed by Dino, not carried (reading changed)"] = stale
    for o in orders:
        rs = o.pop("_rows")
        o["all_lines_usable"] = all(r["use"] == "Y" for r in rs)
        o.update(supplier_read=rs[0]["supplier_read"], date_read=rs[0]["date_read"], date_final=rs[0]["date_final"],
                 date_verified=rs[0]["date_ok"] == "Y")
        o["lines"] = [{"description": r["desc_final"], "quantity": r["qty_final"], "unit_price": r["price_final"],
                       "price_unit": unit(r["per_sheet"] if r["per_sheet"] not in (None, "total") else r["per_read"]),
                       "description_verified": r["desc_ok"] == "Y", "use": r["use"] == "Y", "status": r["status"]} for r in rs]
        cnt["orders all lines usable"] += o["all_lines_usable"]
    stamp = datetime.now().strftime("%Y-%m-%d")
    jp = out / f"po_lines_{stamp}.json"
    jp.write_text(json.dumps({"made": datetime.now().isoformat(timespec="seconds"), "counts": cnt, "orders": orders},
                             ensure_ascii=False, default=str), encoding="utf-8")

    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Lines"
    cols = [("Use", "use", 6), ("Status", "status", 11), ("What differs", "differs", 18), ("Notes", "notes", 30),
            ("Order", "order", 9), ("PDF", None, 6), ("Supplier", "supplier", 22), ("Date", "date", 11), ("Line", "line", 5),
            ("Qty read", "qty_read", 9), ("Qty sheet", "qty_sheet", 9), ("Qty FINAL", "qty_final", 9),
            ("Price read", "price_read", 11), ("Price sheet", "price_sheet", 11), ("Price FINAL", "price_final", 11),
            ("Per read", "per_read", 8), ("Per sheet", "per_sheet", 8),
            ("Description read", "desc_read", 40), ("Description sheet", "desc_sheet", 40), ("Description FINAL", "desc_final", 40),
            ("Alike", "desc_alike", 6), ("Description verified", "desc_ok", 9), ("Lines read", "lines_read", 6), ("Lines sheet", "lines_sheet", 6), ("Where", "where", 7),
            ("Spreadsheet", "book", 34), ("Already corrected", "corrected_before", 9), ("Ref", "pdf", 50), ("Hash", "hash", 12),
            ("Changed by you", "edited", 8), ("Supplier read", "supplier_read", 22), ("Date read", "date_read", 11),
            ("Date FINAL", "date_final", 11), ("Date verified", "date_ok", 8), ("Date note", "date_why", 24), ("Job read", "job_read", 10),
            ("Job FINAL", "job_final", 10), ("Job verified", "job_ok", 7)] + AUTO_COLS
    ws.append([c[0] for c in cols])
    bold = Font(bold=True)
    fill = {"corrected": PatternFill("solid", fgColor="E8F4E8"), "check": PatternFill("solid", fgColor="FFF2CC"),
            "read only": PatternFill("solid", fgColor="EEEEEE"), "sheet only": PatternFill("solid", fgColor="EEEEEE"),
            "unverified": PatternFill("solid", fgColor="EEEEEE")}
    final = PatternFill("solid", fgColor="DDEBF7")
    for c in ws[1]:
        c.font = bold
        c.alignment = Alignment(wrap_text=True, vertical="top")
    order_rank = {"check": 0, "corrected": 1, "sheet only": 2, "read only": 3, "unverified": 4, "match": 5}
    rows.sort(key=lambda r: (order_rank[r["status"]], r["order"], r["line"]))
    for r in rows:
        ws.append([("PDF" if k is None else r[k]) for _, k, _ in cols])
        rr = ws.max_row
        for c in ws[rr]:
            if isinstance(c.value, str) and c.value.startswith("="):
                c.data_type = "s"
        link = ws.cell(rr, 6)
        link.hyperlink = a.link_root.rstrip("\\") + "\\" + r["pdf"]
        link.font = Font(color="0563C1", underline="single")
        if r["status"] in fill:
            ws.cell(rr, 2).fill = fill[r["status"]]
        for ci in (12, 15, 20):
            ws.cell(rr, ci).fill = final
    for i, (name, _, w) in enumerate(cols, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        if name.startswith("Auto "):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].hidden = True    # what the builder set: spots your changes
    ws.freeze_panes = "F2"
    ws.auto_filter.ref = ws.dimensions
    dv = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"A2:A{ws.max_row}")
    s2 = wb.create_sheet("Summary")
    s2.append(["What", "Count"])
    for k, v in cnt.items():
        s2.append([k, v])
    s2.column_dimensions["A"].width = 46
    s3 = wb.create_sheet("How")
    for line in [
        "One row per order line: what the reader read, what the buying spreadsheet has for that order, and FINAL (blue).",
        "FINAL = the spreadsheet's qty and price where the line is the same item. The description is the sheet's only when it is a near-copy (80% alike or more);",
        "otherwise it stays as read and 'Description verified' is blank: the sheet often words it in shorthand (FF SO for FLATFACE SLIP ON), so it is not what the page says.",
        "Lines without a verified description still train the qty and price; correct the FINAL description and put Y in 'Description verified' to train it too.",
        "Use = Y: the line goes into training with its FINAL values. Rows marked check have no Use yet: set Y (after fixing FINAL if needed) or N.",
        "corrected = the sheet differs the way a misreading does: a digit or the decimal point (R11-31 read as 1131.6, a dropped 1), or the description's wording.",
        "check = the spreadsheet differs in a way that may not be a misreading (VAT, a line total, a very different number, a different item, a blank value).",
        "read only / sheet only: a line on one side only (the reader read an extra line, or missed one). unverified: the sheet's line has no qty or price (a note row).",
        "Not used unless you set Use = Y. Orders whose spreadsheet row names another supplier are left out (probably the wrong row).",
        "PDF opens the order (O:\\pipeline). Sorted: check first, then corrected, then lines on one side only, then matches.",
        "The sheet is rebuilt every evening (20:15, and Saturday 16:30 before training) with every order read since: new orders are added and",
        "your changes to Use, the FINAL columns and Description verified are kept ('Changed by you' = Y). Save and close it before 20:15.",
        "A change is not kept when that line's reading or spreadsheet values have changed since: its Notes say so.",
        "Supplier (the sheet's name) and Date FINAL are the order's, on each of its lines; training learns them too (Dino 4 Nov). Date FINAL is the sheet's",
        "where the reading agrees or was misread (the year, or a 1 / 4 / 7 in the day or month); otherwise the reading stays and Date verified is blank.",
    ]:
        s3.append([line])
    s3.column_dimensions["A"].width = 140
    xp = out / STABLE
    tmp = out / (".~" + STABLE)
    wb.save(tmp)
    os.replace(tmp, xp)                                  # never a half-written sheet; an open copy in Excel keeps its own
    print(json.dumps(cnt, indent=1))
    print("sheet:", xp)
    print("json:", jp)


if __name__ == "__main__":
    main()

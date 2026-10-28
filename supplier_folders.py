#!/usr/bin/env python3
"""Supplier training sheet (Dino 28 Oct 17:25): for every MPS / Polymer order the pipeline has (queue, hold, synced),
the supplier from three places - the order's original folder (Melyssa\\Orders-MPS\\<year>\\<letter>\\<SUPPLIER>), the
buying spreadsheet, and what the reader read - so the supplier names can be taught / matched (folder + spreadsheet
agreeing = the right name; the reader's reading next to it = what it gets wrong).

    python3 supplier_folders.py
Writes training/supplier folders vs spreadsheet.xlsx (sheets: Orders, Pairs) and training/supplier_folder_pairs.json
(folder name -> spreadsheet supplier, where they agree) and a copy of the workbook in reports/.
"""
import glob, json, re, shutil, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import po_daily as PD                                   # noqa: E402


def folder_supplier(sc):
    """The last part of the order's original folder (same rule as po_autocheck v7)."""
    parts = [p for p in re.split(r"[\\/]+", str(sc.get("source_folder") or "")) if p.strip()]
    if len(parts) >= 3 and not re.fullmatch(r"\d{4}|[A-Z0-9]|ORDERS?.*|MELYSSA", parts[-1].strip(), re.I):
        return parts[-1].strip()
    return None


ROOT = Path("/srv/ocr/scans/pipeline")
regs = PD.registers()
rows = []
for pat, where in (("out/{t}/*.json", "queue"), ("out/{t}/hold/*.json", "hold"), ("synced/{t}/*/*.json", "synced")):
    for t in ("PurchaseOrders", "PolymerOrders"):
        for js in glob.glob(str(ROOT / pat.format(t=t))):
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except Exception:
                continue
            f = sc.get("fields") or {}
            if f.get("form") not in ("mps_order", "polymer_order") or not f.get("order_number"):
                continue
            key = PD.order_key(f, sc.get("source_file") or sc.get("file"))
            reg = PD.pick(regs.get(key) or [], f) if key else None
            dsup = folder_supplier(sc)
            ssup = (reg or {}).get("supplier")
            read = (sc.get("fields_read") or {}).get("supplier") or f.get("supplier_read") or f.get("supplier")
            rows.append({"Order": f.get("order_number"), "Folder supplier": dsup, "Spreadsheet supplier": ssup,
                         "Reader read": read, "Final supplier": f.get("supplier"),
                         "Folder = spreadsheet": bool(dsup and ssup and PD.same("supplier", dsup, ssup)),
                         "Reader = spreadsheet": bool(read and ssup and PD.same("supplier", read, ssup)),
                         "Checked": bool(f.get("checked")), "Where": f"{t} {where}", "Folder": sc.get("source_folder"),
                         "File": sc.get("source_file")})
pairs = defaultdict(Counter)
for r in rows:
    if r["Folder supplier"] and r["Spreadsheet supplier"]:
        pairs[(r["Folder supplier"].upper(), str(r["Spreadsheet supplier"]).upper())][r["Folder = spreadsheet"]] += 1

import openpyxl
from openpyxl.styles import Font, PatternFill
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Orders"
cols = list(rows[0].keys()) if rows else ["Order"]
ws.append(cols)
for r in sorted(rows, key=lambda r: (str(r["Folder supplier"]), str(r["Order"]))):
    ws.append([r[c] for c in cols])
p = wb.create_sheet("Pairs")
p.append(["Folder supplier", "Spreadsheet supplier", "Orders", "Agree", "Use as alias"])
alias = {}
for (d, s), c in sorted(pairs.items(), key=lambda kv: -sum(kv[1].values())):
    agree = c[True] >= c[False]
    p.append([d, s, sum(c.values()), "yes" if agree else "no", "yes" if agree and d != s else ""])
    if agree:
        alias.setdefault(d, s)
for sh in (ws, p):
    for c in sh[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
    sh.freeze_panes = "A2"
    for col in sh.columns:
        sh.column_dimensions[col[0].column_letter].width = 26
out = ROOT / "training" / "supplier folders vs spreadsheet.xlsx"
out.parent.mkdir(exist_ok=True)
wb.save(out)
shutil.copy2(out, ROOT / "reports" / f"supplier folders vs spreadsheet {datetime.now():%Y-%m-%d %H%M}.xlsx")
json.dump(alias, open(ROOT / "training" / "supplier_folder_pairs.json", "w"), indent=1, ensure_ascii=False)
print(json.dumps({"orders": len(rows), "with_folder": sum(1 for r in rows if r["Folder supplier"]),
                  "with_sheet_row": sum(1 for r in rows if r["Spreadsheet supplier"]),
                  "folder_agrees_with_sheet": sum(1 for r in rows if r["Folder = spreadsheet"]),
                  "reader_agrees_with_sheet": sum(1 for r in rows if r["Reader = spreadsheet"]),
                  "pairs": len(pairs), "aliases": len(alias), "sheet": str(out)}, indent=1))

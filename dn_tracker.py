#!/usr/bin/env python3
"""Delivery note scan tracker: which MPS invoices (= delivery notes) have their signed delivery note scanned into the
Delivery Notes library on MPS Sales, and which are missing. Rebuilt every morning (timer mps-dn-tracker) from:
  <root>/sage_invoices.csv  Sage customer documents, last 6 years (PC sync, nightly after the 01:00 Sage read)
  <root>/dn_scans.csv       every scan in the Delivery Notes library (PC sync, same run)
  <root>/dn_scans_extra.csv optional: scans kept elsewhere (same columns), counted as scanned with their location
Writes <root>/reports/Delivery Note Scan Tracker.xlsx:
  Summary          per month: invoices, scanned, missing, % scanned; customers with the most missing (last 12 months)
  Missing          every invoice with no scan, newest first: date, days, customer, job, order, value, what was on it
  All invoices     every invoice with Scanned Yes/No and a link to the scan
  Scans not in Sage scans whose document number is not an MPS invoice of the last 6 years (typos, other companies)

    python3 dn_tracker.py [--root /srv/ocr/scans/pipeline]
"""
import argparse, csv, os, re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path


# accounts whose invoices don't get a signed MPS delivery note back (Dino 25 Oct: CAT cash sales stainless steel use
# other delivery notes); left out of the tracker entirely
EXCLUDE_ACCOUNTS = {"CAS52D": "CAT CASH SALES STAINLESS STEEL"}
# Sage customer groups left out (Dino 26 Oct: customers in the stainless steel division are not chased). Read from
# sage_invoices.csv CustomerGroupID (MPSBusiness D150, 26 Oct); until that column arrives, the group's accounts as at the
# 17 Oct Sage export (dn_group6_accounts.csv beside this script) stand in.
EXCLUDE_GROUPS = {"6": "stainless steel division"}
GROUP_FALLBACK = Path(__file__).resolve().with_name("dn_group6_accounts.csv")


def dn_key(s):
    m = re.match(r"^\s*IN\s*0*(\d{5,6})\s*(?:\.\w+)?$", str(s or "").upper())
    return f"IN{int(m.group(1)):06d}" if m else None


def txt(v):
    """openpyxl writes a string starting with = as a formula; keep it text."""
    return ("'" + v) if isinstance(v, str) and v.startswith("=") else v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
    ap.add_argument("--company", default="MPS")
    a = ap.parse_args()
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    root = Path(a.root)
    inv_p, scan_p, extra_p = root / "sage_invoices.csv", root / "dn_scans.csv", root / "dn_scans_extra.csv"
    invoices, excluded, ex_keys = {}, 0, set()
    ex_group, ex_by = 0, Counter()
    fallback = {}
    if GROUP_FALLBACK.exists():
        for g in csv.DictReader(open(GROUP_FALLBACK, encoding="utf-8-sig")):
            fallback[g["Account"].strip().upper()] = str(g.get("Group") or "").strip()
    for r in csv.DictReader(open(inv_p, encoding="utf-8-sig")):
        if r["Company"] != a.company or r["DocType"] != "4":
            continue
        acct = r["CustomerAccount"].strip().upper()
        grp = str(r.get("CustomerGroupID") or "").strip().split(".")[0] if "CustomerGroupID" in r else fallback.get(acct, "")
        if acct in EXCLUDE_ACCOUNTS or grp in EXCLUDE_GROUPS:
            excluded += 1
            if acct not in EXCLUDE_ACCOUNTS:
                ex_group += 1
                ex_by[r["CustomerName"]] += 1
            ex_keys.add(dn_key(r["DocNumber"]))
            continue
        k = dn_key(r["DocNumber"])
        if k and k not in invoices:
            invoices[k] = r
    scans = {}
    sources = [(scan_p, "Delivery Notes library")] + ([(extra_p, "elsewhere")] if extra_p.exists() else [])
    for p, where in sources:
        if not p.exists():
            continue
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            k = dn_key(r.get("DocumentNo")) or dn_key(r.get("File"))
            if k and k not in scans:
                scans[k] = dict(r, Where=r.get("Where") or where)
    today = date.today()
    bold, head = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    miss_fill = PatternFill("solid", fgColor="FDE9E7")
    wb = openpyxl.Workbook()

    def sheet(ws, header, rows, widths, link_col=None):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = bold, head
        for row in rows:
            ws.append([txt(v) for v in row[0]])
            if link_col and row[1]:
                c = ws.cell(ws.max_row, link_col)
                c.hyperlink, c.font = row[1], Font(color="0563C1", underline="single")
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def ddate(s):
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    # Missing
    missing = sorted((k for k in invoices if k not in scans), key=lambda k: (invoices[k]["DocDate"], k), reverse=True)
    ws = wb.active
    ws.title = "Missing"
    rows = []
    for k in missing:
        r = invoices[k]; d = ddate(r["DocDate"])
        rows.append(([k, d, (today - d).days if d else None, r["CustomerName"], r["CustomerAccount"], r["JobNumber"], r["JobName"],
                      r["CustomerOrder"], float(r["TotalExcl"] or 0), int(r["LineCount"] or 0), (r["Lines"] or "")[:250]], None))
    sheet(ws, ["DN No", "Date", "Days", "Customer", "Account", "Job", "Job name", "Customer order", "Value excl", "Lines", "On it"],
          rows, [11, 11, 7, 32, 10, 9, 24, 20, 12, 7, 80])
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=2):
        row[0].number_format = "yyyy-mm-dd"
    for row in ws.iter_rows(min_row=2, min_col=9, max_col=9):
        row[0].number_format = "#,##0.00"

    # All invoices
    wa = wb.create_sheet("All invoices")
    rows = []
    for k in sorted(invoices, key=lambda k: (invoices[k]["DocDate"], k), reverse=True):
        r = invoices[k]; s = scans.get(k)
        rows.append(([k, ddate(r["DocDate"]), r["CustomerName"], r["JobNumber"], "Yes" if s else "No",
                      (s or {}).get("Where", ""), (s or {}).get("File", "")], (s or {}).get("Url") or None))
    sheet(wa, ["DN No", "Date", "Customer", "Job", "Scanned", "Where", "Scan"], rows, [11, 11, 32, 9, 9, 22, 30], link_col=7)
    for row in wa.iter_rows(min_row=2, min_col=2, max_col=2):
        row[0].number_format = "yyyy-mm-dd"
    for row in wa.iter_rows(min_row=2, min_col=5, max_col=5):
        if row[0].value == "No":
            row[0].fill = miss_fill

    # Scans not in Sage
    wx = wb.create_sheet("Scans not in Sage")
    rows = [([r.get("DocumentNo"), r.get("File"), r.get("DNDate", "")[:10], r.get("JobNo"), r.get("Where")], r.get("Url") or None)
            for k, r in sorted(scans.items()) if k not in invoices and k not in ex_keys]
    sheet(wx, ["Document No", "File", "DN Date", "Job", "Where"], rows, [14, 30, 11, 10, 22], link_col=2)

    # Summary (first sheet)
    sm = wb.create_sheet("Summary", 0)
    per = defaultdict(lambda: [0, 0])
    for k, r in invoices.items():
        m = r["DocDate"][:7]
        per[m][0] += 1
        per[m][1] += k in scans
    n_inv, n_scan = len(invoices), sum(1 for k in invoices if k in scans)
    stamp = lambda p: datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if p.exists() else "not there yet"
    for line in [f"Delivery note scan tracker - built {datetime.now():%Y-%m-%d %H:%M}",
                 f"MPS invoices (Sage, last 6 years): {n_inv:,}   scanned: {n_scan:,} ({n_scan / max(1, n_inv):.0%})   missing: {n_inv - n_scan:,}",
                 f"Sources: Sage invoices {stamp(inv_p)}; Delivery Notes library list {stamp(scan_p)}"
                 + (f"; other scans {stamp(extra_p)}" if extra_p.exists() else ""),
                 "A delivery note counts as scanned when a file with its number is in the Delivery Notes library (MPS Sales).",
                 "Left out (use other delivery notes): " + ", ".join(f"{n} ({k})" for k, n in EXCLUDE_ACCOUNTS.items())
                 + f" - {excluded - ex_group:,} invoices",
                 "Left out (not chased): customers in Sage customer group " + ", ".join(f"{k} ({n})" for k, n in EXCLUDE_GROUPS.items())
                 + f" - {ex_group:,} invoices" + (f": {', '.join(f'{n} {c}' for n, c in ex_by.most_common(8))}" if ex_by else ""), ""]:
        sm.append([line])
    sm["A1"].font = Font(bold=True, size=13)
    sm.append(["Month", "Invoices", "Scanned", "Missing", "% scanned"])
    for c in sm[sm.max_row]:
        c.font, c.fill = bold, head
    for m in sorted(per, reverse=True):
        i, s = per[m]
        sm.append([m, i, s, i - s, s / i if i else None])
        sm.cell(sm.max_row, 5).number_format = "0%"
    sm.append([])
    sm.append(["Customers with the most missing delivery notes (last 12 months)"])
    sm.cell(sm.max_row, 1).font = bold
    cutoff = date(today.year - 1, today.month, 1)
    cust = Counter(invoices[k]["CustomerName"] for k in missing if (ddate(invoices[k]["DocDate"]) or today) >= cutoff)
    sm.append(["Customer", "Missing"])
    for c in sm[sm.max_row]:
        c.font, c.fill = bold, head
    for name, n in cust.most_common(25):
        sm.append([txt(name), n])
    for col, w in zip("ABCDE", (40, 10, 10, 10, 10)):
        sm.column_dimensions[col].width = w

    out = root / "reports" / "Delivery Note Scan Tracker.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name("~tmp-" + out.name)
    wb.save(tmp)
    os.replace(tmp, out)
    print(f"{out}: {excluded} invoices of excluded accounts left out; {n_inv} invoices, {n_scan} scanned, {n_inv - n_scan} missing; {len(scans)} scans listed "
          f"({sum(1 for k in scans if k not in invoices and k not in ex_keys)} not an MPS invoice of the last 6 years)")


if __name__ == "__main__":
    main()

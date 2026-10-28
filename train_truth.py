#!/usr/bin/env python3
"""The buying spreadsheets (JOB PURCHASING / GENERAL BUYING, one workbook per year) -> truth.json: one record per
order number with its header values and line items as Melyssa typed them.

  python3 train_truth.py --out truth.json "14 - JOB PURCHASING 2024.xlsx" "14 - GENERAL BUYING 2024.xlsx"

Every tab is a table with a header row; a row with an order number starts an order, rows without one are further
lines of the same order. Tabs differ a little (REQ NO / PO REQ NO / VEHICLE REG / IGT), so columns are found by
header name. An order that appears on several tabs (CONS orders are repeated on EMMA PAYMENTS and FORKLIFTS & CRANES,
STOCK orders on PIPE; FITTINGS) keeps every occurrence; the sheet builder picks one and flags disagreements.
"""
import argparse, datetime, json, re, sys
import openpyxl

AUTHORITY = {"MF": "Melyssa", "IC": "Ivor", "IAC": "Ivor", "MC": "Melyssa"}
SKIP_TABS = {"PRICE INCREASES"}
UNIT = {"EACH": "each", "EA": "each", "LEN": "length", "LENS": "length", "LENGTH": "length", "KG": "kg", "KGS": "kg", "LTR": "litre",
        "LTRS": "litre", "L": "litre", "MTR": "metre", "M": "metre", "MTRS": "metre", "LOT": "lot", "TOTAL": "total", "TOTAL EX VAT": "total",
        "BOX": "each", "PAIR": "each", "PAIRS": "each", "ROLL": "each", "SET": "each", "HR": "each", "HRS": "each"}


def norm_order(v):
    s = re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
    if not s:
        return None
    m = re.fullmatch(r"(A|PL|PLS|DF)0*(\d+)", s)
    if m:
        return ("PL" if m.group(1) == "PLS" else m.group(1)) + m.group(2)
    if re.fullmatch(r"\d{4,5}", s):
        return s                      # an IGT number
    return None


def cell_date(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y-%m-%d")
    m = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})\s*", str(v or ""))
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def clean(v):
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def num(v):
    if v in (None, ""):
        return None
    try:
        return round(float(str(v).replace(",", "").replace("R", "").strip()), 2)
    except ValueError:
        return None


def header_map(row):
    h = {}
    for i, c in enumerate(row):
        k = re.sub(r"[^A-Z]", "", str(c or "").upper())
        if not k:
            continue
        for want, keys in (("date", ("DATE",)), ("order", ("ORDERNUMBER", "ORDERNO")), ("igt", ("IGT",)),
                           ("job", ("JOBNUMBER", "JOBNO")), ("allocation", ("ALLOCATION",)), ("authority", ("AUTHORITY",)),
                           ("qty", ("QTY",)), ("description", ("DESCRIPTION",)), ("supplier", ("SUPPLIER",)),
                           ("contact", ("SUPPCONTACT", "SUPPLIERCONTACT")), ("price", ("PRICE",)), ("per", ("PER",)),
                           ("reason", ("REASON",)), ("req", ("REQNO", "POREQNO")), ("reg", ("VEHICLEREG",)),
                           ("driver", ("DRIVER",)), ("collection", ("COLLECTIONDETAILS",)), ("due", ("DUEDATE",))):
            if k in keys and want not in h:
                h[want] = i
    return h if ("order" in h or "igt" in h) and "description" in h else None


def read_tab(ws, book, tab):
    rows = ws.iter_rows(values_only=True)
    h = None
    for row in rows:
        h = header_map(row)
        if h:
            break
    if not h:
        return []
    get = lambda row, k: row[h[k]] if k in h and h[k] < len(row) else None
    orders, cur = [], None
    for r, row in enumerate(rows, start=2):
        on = norm_order(get(row, "igt") if "igt" in h else get(row, "order"))
        desc, qty, price = clean(get(row, "description")), num(get(row, "qty")), num(get(row, "price"))
        if on:
            auth = clean(get(row, "authority"))
            cur = {"order_number": on, "book": book, "tab": tab, "row": r, "date": cell_date(get(row, "date")),
                   "job_number": clean(get(row, "job")), "allocation": clean(get(row, "allocation")),
                   "authority": auth, "signed_by": AUTHORITY.get((auth or "").upper().strip()),
                   "supplier": clean(get(row, "supplier")), "att_of": clean(get(row, "contact")),
                   "other_po_ref": clean(get(row, "req")), "registration": clean(get(row, "reg")), "reason": clean(get(row, "reason")),
                   "form": "internal_group_transfer" if "igt" in h else ("polymer_order" if on.startswith("PL") else "mps_order"),
                   "lines": []}
            if cur["form"] == "internal_group_transfer":
                cur["supplier"] = "KUSASA"
            orders.append(cur)
        if cur is None or not desc:
            continue
        per = clean(get(row, "per"))
        # a row with only a description (REMOVE EXISTING HDPE; INT: RL 50 SHORE ...) is its own line on the form too
        cur["lines"].append({"quantity": qty, "description": desc, "unit_price": price,
                             "per": per, "price_unit": UNIT.get((per or "").upper().strip(), None)})
    return orders


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("books", nargs="+")
    ap.add_argument("--out", default="truth.json")
    a = ap.parse_args()
    truth = {}
    for b in a.books:
        wb = openpyxl.load_workbook(b, data_only=True, read_only=True)
        bname = re.sub(r"^[0-9a-f]{8}-", "", b.rsplit("/", 1)[-1]).replace("_", " ")
        for ws in wb.worksheets:
            if ws.title.strip().upper() in SKIP_TABS:
                continue
            for o in read_tab(ws, bname, ws.title.strip()):
                truth.setdefault(o["order_number"], []).append(o)
    json.dump(truth, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    n = sum(len(v) for v in truth.values())
    print(f"{len(truth)} order numbers, {n} occurrences, {sum(len(o['lines']) for v in truth.values() for o in v)} lines -> {a.out}")
    multi = {k: [o['tab'] for o in v] for k, v in truth.items() if len(v) > 1}
    print(f"{len(multi)} orders on more than one tab")


if __name__ == "__main__":
    main()

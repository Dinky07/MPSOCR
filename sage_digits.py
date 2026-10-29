#!/usr/bin/env python3
"""Supplier-invoice amounts and dates that disagree with Sage only by a reading slip (Dino 29 Oct: SPEEDTEK CC 6332 -
total "12893-80" read as 1289380, VAT 1681-80 as 168180, 30/1/2024 as 30/11/2024; Sage has 12893.80 / 1681.80 /
2024-01-30).

For every supplier invoice waiting in out/SupplierInvoices* (and hold/) that Sage has - same supplier (its Sage account
from the VAT number) and the same invoice number (sage_ap.csv, written by the PC sync):
  total / vat  replaced by Sage's value when the read value is Sage's with the decimal point missed (x10, x100, x1000)
               or with one digit missing or extra; one different digit only when the invoice's other numbers back it
               (the read VAT already equals Sage's, or the read total disagrees with the read subtotal + VAT)
  invoice_date replaced by Sage's date when the date as written is one digit away from Sage's date written the same
               way (30/11/2024 vs 30/1/2024)
Anything else is reported, never changed. Each change goes into fields.text_fixes ({field, value, how}) and edits, and
the value as read is kept in fields.<field>_as_read.
    python3 sage_digits.py [--apply] [--synced]      (--synced: also report invoices already uploaded; never changed)
Report: reports/sage digits <stamp>[ dry run].csv / .json
"""
import argparse, csv, glob, json, re, sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")


def ref_keys(ref):
    """As the dashboard's: CIN0387242 / CIN-0387242 / 0387242; I55038 vs Sage 55038."""
    k = re.sub(r"[\s\-/._#]", "", str(ref or "")).upper()
    keys = {k} if k else set()
    digits = re.sub(r"\D", "", k).lstrip("0")
    if len(digits) >= 3:
        keys.add("#" + digits)
    return keys


def num(x):
    try:
        return float(str(x).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def lev(a, b):
    """Edit distance (insert / delete / substitute), and whether a substitution was needed for distance 1."""
    if abs(len(a) - len(b)) > 1:
        return 9, False
    if len(a) == len(b):
        d = sum(1 for x, y in zip(a, b) if x != y)
        return d, d == 1
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return (1 if any(l[:i] + l[i + 1:] == s for i in range(len(l))) else 9), False


def cents(v):
    return f"{v:.2f}".replace(".", "").lstrip("0") or "0"


def amount_slip(read, sage, backed):
    """Why the read amount is Sage's amount misread, or None."""
    if read is None or sage is None or abs(read - sage) <= 1:
        return None
    for k, why in ((10, "decimal point missed (x10)"), (100, "decimal point missed"), (1000, "decimal point missed (x1000)")):
        if sage and abs(read / k - sage) <= 0.011 * max(1, k / 100):
            return why
        if read and abs(read * k - sage) <= 0.011 * max(1, k / 100):
            return f"decimal point in the wrong place (/{k})"
    for r in {cents(read), re.sub(r"\D", "", f"{read:.2f}".rstrip("0").rstrip("."))}:
        d, sub = lev(r, cents(sage))
        if d == 1 and (not sub or backed):
            return "one digit " + ("different" if sub else ("missed" if len(r) < len(cents(sage)) else "extra"))
    return None


def date_forms(d):
    """Sage's date written the ways people write it, digits only: 30/1/2024, 30/01/2024, 30/1/24, 2024/01/30 ..."""
    out = set()
    for dd in (str(d.day), f"{d.day:02}"):
        for mm in (str(d.month), f"{d.month:02}"):
            for yy in (str(d.year), f"{d.year % 100:02}"):
                out.add(dd + mm + yy)
                out.add(yy + mm + dd)
    return out


def date_slip(as_written, read_iso, sage_iso):
    if not sage_iso or read_iso == sage_iso:
        return None
    try:
        sd = date.fromisoformat(sage_iso[:10])
    except ValueError:
        return None
    if re.search(r"[A-Za-z]", str(as_written or "")):
        return None                       # "9 Sep 2025": the month is a word, its digits can't be compared
    w = re.sub(r"\D", "", str(as_written or ""))
    if not w:
        return None
    # a missed or extra digit only: one different digit can be a real difference (Sage's date may be the posting date)
    found = None
    for f in date_forms(sd):
        d, sub = lev(w, f)
        if d == 0:
            return "same date, read another way"
        if d == 1 and not sub:
            found = f"one digit {'extra' if len(w) > len(f) else 'missed'} in the date as written ({as_written})"
    return found


def load_sage():
    by_acc, acc_by_vat = defaultdict(lambda: defaultdict(list)), defaultdict(set)
    p = ROOT / "sage_ap.csv"
    with open(p, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            acc = (r.get("VendorAccount") or "").strip()
            if not acc:
                continue
            row = {"account": acc, "ref": r.get("Reference"), "date": (r.get("TxDate") or "")[:10],
                   "amount": num(r.get("AmountIncl")), "vat": num(r.get("VatAmount"))}
            for k in ref_keys(r.get("Reference")):
                by_acc[acc][k].append(row)
            v = re.sub(r"\D", "", r.get("VatNo") or "")
            if re.fullmatch(r"4\d{9}", v):
                acc_by_vat[v].add(acc)
    return by_acc, acc_by_vat, datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")


def check(f, by_acc, acc_by_vat):
    """(sage row or None, {field: (sage value, why)}, notes)"""
    vat_no = re.sub(r"\D", "", str(f.get("supplier_vat_no") or ""))
    accs = acc_by_vat.get(vat_no) or set()
    keys = ref_keys(f.get("invoice_number"))
    hits = {}
    for a in accs:
        for k in keys:
            for r in by_acc[a].get(k, []):
                hits[(a, r["ref"], r["date"], r["amount"])] = r
    if not accs:
        return None, {}, "supplier not in Sage by VAT number"
    if not hits:
        return None, {}, "invoice number not in Sage for this supplier"
    total, vat, sub = num(f.get("total")), num(f.get("vat")), num(f.get("subtotal"))
    inconsistent = None not in (total, vat, sub) and abs(sub + vat - total) > 1
    best = None
    for h in hits.values():                                    # the Sage row the read values fit best
        fixes = {}
        if h["amount"] is not None and total is not None and abs(total - h["amount"]) > 1:
            why = amount_slip(total, h["amount"], backed=(vat is not None and h["vat"] is not None and abs(vat - h["vat"]) <= 1) or inconsistent)
            if why:
                fixes["total"] = (round(h["amount"], 2), why)
        if h["vat"] is not None and vat is not None and abs(vat - h["vat"]) > 1:
            why = amount_slip(vat, h["vat"], backed=(total is not None and h["amount"] is not None and abs(total - h["amount"]) <= 1) or "total" in fixes)
            if why:
                fixes["vat"] = (round(h["vat"], 2), why)
        why = date_slip(f.get("invoice_date_as_written") or f.get("invoice_date"), str(f.get("invoice_date") or "")[:10], h["date"])
        if why:
            fixes["invoice_date"] = (h["date"], why)
        left = []
        if h["amount"] is not None and total is not None and abs(total - h["amount"]) > 1 and "total" not in fixes:
            left.append(f"total {total:g} vs Sage {h['amount']:g}")
        if h["date"] and str(f.get("invoice_date") or "")[:10] != h["date"] and "invoice_date" not in fixes:
            left.append(f"date {f.get('invoice_date')} vs Sage {h['date']}")
        score = (len(left), -len(fixes))
        if best is None or score < best[0]:
            best = (score, h, fixes, "; ".join(left))
    _, h, fixes, left = best
    return h, fixes, left or ("agrees with Sage" if not fixes else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--synced", action="store_true", help="also report invoices already uploaded (never changed)")
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    by_acc, acc_by_vat, sage_at = load_sage()
    pats = ["out/SupplierInvoices*/*.json", "out/SupplierInvoices*/hold/*.json"] + (["synced/SupplierInvoices*/*/*.json"] if a.synced else [])
    rows, cnt = [], Counter()
    for pat in pats:
        for js in sorted(Path(p) for p in glob.glob(str(ROOT / pat))):
            if js.name.endswith((".tmp", ".lock")):
                continue
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except Exception:
                continue
            f = sc.get("fields") or {}
            if sc.get("doc_type") != "supplier_invoice":
                continue
            h, fixes, note = check(f, by_acc, acc_by_vat)
            synced = js.relative_to(ROOT).parts[0] == "synced"
            result = "no change" if not fixes else ("would fix" if not a.apply or synced else "fixed")
            if synced and fixes:
                result = "uploaded already: fix by hand"
            if fixes and a.apply and not synced:
                try:
                    with sidecar_lock.locked(js, "sage_digits"):
                        sc2 = json.load(open(js, encoding="utf-8"))
                        f2 = sc2.setdefault("fields", {})
                        changes = []
                        for fld, (val, why) in fixes.items():
                            old = f2.get(fld)
                            if num(old) != num(f.get(fld)) and old != f.get(fld):
                                continue                       # edited meanwhile: leave it
                            f2.setdefault(f"{fld}_as_read", old)
                            f2[fld] = val
                            how = f"Sage {h['account']} {h['ref']}: read {old} - {why}"
                            f2.setdefault("text_fixes", []).append({"field": fld, "value": val, "how": how, "by": "sage_digits", "at": stamp})
                            changes.append({"field": fld, "old": old, "new": val})
                        if changes:
                            sc2.setdefault("edits", []).append({"at": stamp, "by": "sage_digits (reading slip against Sage)", "changes": changes})
                            tmp = js.with_name(js.name + ".sd.tmp")
                            tmp.write_text(json.dumps(sc2, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                            tmp.replace(js)
                        else:
                            result = "edited meanwhile"
                except sidecar_lock.Locked:
                    result = "locked (next run)"
            if h is None:
                cnt[note] += 1
            else:
                cnt[result if fixes else ("agrees with Sage" if not note or note == "agrees with Sage" else "differs from Sage (not a reading slip)")] += 1
                for fld, (_, why) in fixes.items():
                    cnt[f"  {fld}: {why.split(' (')[0]}"] += 1
            rows.append({"file": str(js.relative_to(ROOT)), "supplier": f.get("supplier_name"), "invoice": f.get("invoice_number"),
                         "sage": f"{h['account']} {h['ref']} {h['date']} {h['amount']}" if h else "",
                         "fixes": "; ".join(f"{k}: {f.get(k)} -> {v} ({w})" for k, (v, w) in fixes.items()),
                         "left": note, "result": result})
    out = ROOT / "reports" / f"sage digits {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)
    summ = {"applied": a.apply, "sage_ap_at": sage_at, "invoices": len(rows), "results": dict(cnt.most_common()), "report": f"{out}.csv"}
    json.dump(summ, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Invoices on the order holds -> Supplier Invoices (Dino 29 Oct 09:32: a Digitrac invoice to Mining Pressure Systems,
"Inv 8231 Ref A79166", sat on the Purchase Orders hold; send it and others like it, and other invoices to Mining
Pressure, to the Supplier Invoices queue to be read there).

A hold document (Purchase / Polymer Orders) is an invoice to the group when
  - its title area (first lines) says INVOICE / TAX INVOICE (not pro forma, quotation, statement or order form), or its
    file name says "Inv <number>" / "Invoice", and
  - the page names Mining Pressure (Systems/Supplies), Kusasa or Polymer, or carries MPS's VAT number 4320123039, and
  - it is not issued BY the group (its title area does not open with Mining Pressure's letterhead while showing no
    other VAT number) - those are listed as "mps_issued", not moved.
Moved like hold_resort does it: the PDF is copied to in/SupplierInvoices/ with a _meta note (rerouted_from, original
folder) so the invoice reader reads it, and the hold copy goes to deleted/<Type>/<yyyy-mm>/ with the reason (recoverable).
Left alone: order forms with an order number, checked, edited by a person on the dashboard, uploaded or locked documents.
    python3 inv_to_si.py [--apply] [--jobs 4]
v2 (09:45): exclusions tightened after the first dry run (pro formas, quotations, test certs, order acknowledgements, POPs).
Report: reports/invoices to supplier invoices <stamp>[ dry run].csv / .json
"""
import argparse, csv, glob, json, os, re, shutil, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import presort                                          # noqa: E402  _title, VATNO, OWN_VAT
import route_sort                                       # noqa: E402  text_of() (OCR cache), remove()
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
FORMS = {"mps_order", "polymer_order", "internal_group_transfer", "ronin_order"}
SCRIPT_EDIT = re.compile(r"^(po_autocheck|route_sort|hold_resort|od_sort|requeue_po12|numbers_pass|fix_|supplier rule|ronin_|coalesce_hold|regroup_|orphan_|\d+-)", re.I)
GROUP = re.compile(r"\bMINING PRESSURE\b|\bKUSASA\b|\bPOLYMER (SYSTEMS|LININGS|ENGINEERING|PRODUCTS)?\b|(?<!\d)4320123039(?!\d)")
TITLE_INV = re.compile(r"\b(TAX ?|VAT )?INVOICE\b|\bTAXINVOICE\b")
STRONG_INV = re.compile(r"\bTAX ?INVOICE\b|\bVAT INVOICE\b|\bINVOICE ?(NO|NUMBER|NR|#)\b|\bINVOICE ?:")
HARD_NOT = re.compile(r"\bPRO ?-?FORMA\b|\bNOT A (VAT|TAX) INVOICE\b|\bCERTIFICATE\b|\bORDER ACKNOWLEDGE?MENT\b")
SOFT_NOT = re.compile(r"\bQUOT ?ATION\b|\bSTATEMENT\b|\bPURCHASE ORDER\b|\bDELIVERY NOTE\b|\bREMITTANCE\b|\bJOB CARD\b|\bTERMS (AND|&) CONDITIONS\b")
NAME_INV = re.compile(r"(?<![A-Z])(INV|INVOICE|SINV|TAX INV)[\s_\-.#]*\d|\bINVOICE\b", re.I)
NAME_NOT = re.compile(r"\bPOP\b|proof of payment|pro ?-?forma|quot|statement", re.I)
NAME_GROUP = re.compile(r"mining pressure|kusasa|\bpolymer\b|\bMPS\b", re.I)
MPS_HEAD = re.compile(r"^\W*(\S+\s+){0,1}MINING PRESSURE (SYSTEMS|SUPPLIES)")


def classify(sc, pdf):
    """v2 (09:45): pro formas, quotations, certificates, order acknowledgements, proofs of payment and terms pages out;
    a tax invoice with a supplier VAT number in MPS's archive counts even when OCR lost the group's name."""
    text = route_sort.text_of(pdf)
    T = presort._title(text)
    B = re.sub(r"\s+", " ", text or "").upper()
    name = f"{sc.get('source_file') or ''} {pdf.name}"
    f = sc.get("fields") or {}
    read_as = str(f.get("library_doc_type") or f.get("po_doc_type") or "")
    other_vat = [v for v in dict.fromkeys(presort.VATNO.findall(B)) if v not in presort.OWN_VAT]
    strong = bool(STRONG_INV.search(T))
    if (HARD_NOT.search(T) or (SOFT_NOT.search(T) and not strong) or NAME_NOT.search(name)
            or read_as in ("Proof of Payment", "Pro Forma")):
        return "not an invoice", T[:160], other_vat
    if not (TITLE_INV.search(T) or NAME_INV.search(name)):
        return "not an invoice", T[:160], other_vat
    if MPS_HEAD.search(T):
        return "mps_issued", T[:160], other_vat
    if GROUP.search(B) or NAME_GROUP.search(name):
        return "invoice to the group", T[:160], other_vat
    if strong and other_vat:
        return "invoice to the group", T[:160], other_vat       # a supplier's tax invoice in MPS's own order archive
    return "invoice, group not named", T[:160], other_vat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    cands, rows, cnt = [], [], Counter()
    for t in ("PurchaseOrders", "PolymerOrders", "RoninOrders"):
        for js in sorted(glob.glob(str(ROOT / "out" / t / "hold" / "*.json"))):
            js = Path(js)
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except Exception:
                continue
            f = sc.get("fields") or {}
            pdf = js.parent / str(sc.get("file") or "")
            if not pdf.is_file():
                continue
            why = None
            if f.get("form") in FORMS and f.get("order_number"):
                why = "kept: order form"
            elif f.get("checked"):
                why = "kept: checked"
            elif [e for e in (sc.get("edits") or []) if not SCRIPT_EDIT.match(str(e.get("by") or ""))]:
                why = "kept: edited on the dashboard"
            elif Path(str(js) + ".uploaded").exists():
                why = "kept: uploaded"
            if why:
                continue
            cands.append((t, js, sc, pdf))

    def one(c):
        try:
            return c, classify(c[2], c[3])
        except Exception as e:
            return c, (f"error: {e}"[:120], "", [])
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = list(ex.map(one, cands))
    route_sort.save_index()
    d, md = ROOT / "in" / "SupplierInvoices", ROOT / "in" / "SupplierInvoices" / "_meta"
    for (t, js, sc, pdf), (kind, title, vats) in res:
        result = "stays"
        if kind == "invoice to the group":
            result = "would move" if not a.apply else "moved"
            if a.apply:
                try:
                    with sidecar_lock.locked(js, "inv_to_si"):
                        md.mkdir(parents=True, exist_ok=True)
                        stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(sc.get("source_file") or pdf.name)).stem)[:110].strip()
                        page = sc.get("source_page")
                        name, k = f"{stem}{f'_p{page}' if page else ''}.pdf", 2
                        while (d / name).exists() or (md / (name + ".json")).exists():
                            name = f"{stem}{f'_p{page}' if page else ''}_{k}.pdf"
                            k += 1
                        note = {"original_name": sc.get("source_file") or pdf.name,
                                "rerouted_from": f"out/{t}/hold/{pdf.name} (invoice to the group, Dino 29 Oct)", "routed_at": stamp}
                        if sc.get("source_folder"):
                            note["source_folder"] = sc["source_folder"]
                        (md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
                        shutil.copy2(pdf, d / (name + ".part"))
                        os.replace(d / (name + ".part"), d / name)
                        route_sort.remove(js, sc, t, f"invoice to the group -> Supplier Invoices as {name} (Dino 29 Oct)", stamp)
                except sidecar_lock.Locked:
                    result = "locked (next run)"
                except Exception as e:
                    result = f"error: {e}"[:120]
        cnt[f"{kind} -> {result}"] += 1
        f = sc.get("fields") or {}
        rows.append({"type": t, "file": pdf.name, "source_file": sc.get("source_file"), "source_folder": sc.get("source_folder"),
                     "read_as": f.get("library_doc_type") or f.get("po_doc_type"), "kind": kind, "result": result,
                     "other_vat": ", ".join(vats[:3]), "title": title})
    out = ROOT / "reports" / f"invoices to supplier invoices {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)
    summ = {"applied": a.apply, "hold_documents_looked_at": len(cands), "results": dict(cnt.most_common()), "report": f"{out}.csv"}
    json.dump(summ, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

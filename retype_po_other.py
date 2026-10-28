#!/usr/bin/env python3
"""Sorts out the "Other" documents the Purchase Orders reader left in its queue and hold queue (Dino 26 Oct):
  * pro formas, quotations and cancelled orders get their own Document Type ("Pro Forma", "Quotation",
    "Cancelled Order"); order_in_sage records whether their A-number is a real order in Sage (sage_po.csv). The PC sync
    uploads them only when their A-number is "linked": a Purchase Order document the pipeline holds (queue, hold or
    synced) has that A-number (Sage does not hold MPS's A-numbers). One on hold that qualifies moves to the queue.
  * delivery notes are sent to the Delivery Notes queue, job transfers to the Job Transfers queue (PDF copied to
    in/<Type>/ with a _meta note "rerouted_from"; the PO copy moved to deleted/PurchaseOrders/<yyyy-mm>/ + .rerouted.json).
Run after reroute_inv.py (the likely invoices are gone by then). Pro forma / quotation wording comes from the PDF text
scored by reroute_inv.py's dry run (--report) where there is one, else from the reader's type and the file name.
Writes reports/retype po other <date>.csv. Dry run unless --apply.

    python3 retype_po_other.py [--report "reports/reroute invoices 2026-10-26.csv"] [--apply]
"""
import argparse, csv, json, os, re, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tc_orders  # noqa: E402  (sage_orders: A-number -> Sage documents)

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--report", help="reroute_inv.py dry-run report (its 'why' column has the PDF wording)")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--no-dn", action="store_true", help="leave delivery notes where they are (supplier delivery notes)")
ap.add_argument("--only-dn", action="store_true", help="only send the delivery notes to the Delivery Notes queue (Dino 27 Oct)")
a = ap.parse_args()
root = Path(a.root)
KEEP = {"purchase_order", "internal_transfer", "proof_of_payment", "polymer_order"}
why_of = {}
if a.report:
    with open(a.report, encoding="utf-8-sig", newline="") as fh:
        why_of = {(r["where"], r["file"]): (r.get("why") or "") for r in csv.DictReader(fh)}
sage = tc_orders.sage_orders()
orders = set()                                   # A-numbers of the Purchase Order documents the pipeline holds
for pat in ("out/PurchaseOrders/*.json", "out/PurchaseOrders/hold/*.json", "synced/PurchaseOrders/*/*.json"):
    for p in root.glob(pat):
        try:
            g = (json.loads(p.read_text(encoding="utf-8")).get("fields") or {})
        except Exception:
            continue
        on = str(g.get("order_number") or "").strip().upper()
        if re.fullmatch(r"A\d{5}", on) and "Purchase Order" in str(g.get("library_doc_type") or "Purchase Order"):
            orders.add(on)
print(f"A-numbers on Purchase Order documents: {len(orders)}; in Sage: {len(sage)}")


def candidate(f):
    if f.get("library_doc_type") == "Other":
        return True
    pdt = str(f.get("po_doc_type") or "")
    return bool(pdt) and pdt not in KEEP and not f.get("order_number")


def decide(where, js, sc):
    f = sc.get("fields") or {}
    pdt = str(f.get("po_doc_type") or "")
    name = f"{sc.get('source_file') or ''} {js.name}".upper()
    why = why_of.get((where, js.name), "")
    if pdt == "job_transfer" or re.match(r"\s*JT[\s_-]*\d", str(sc.get("source_file") or ""), re.I):
        return "route", "JobTransfers", "reader: job transfer" if pdt == "job_transfer" else "file name JT"
    if pdt == "delivery_note":
        if a.no_dn:
            return None, None, "delivery note: left where it is (--no-dn)"
        return "route", "DeliveryNotes", "reader: delivery note"
    if "PROOF OF PAYMENT" in name:
        return None, None, "proof of payment (file name): left as it is"
    if re.search(r"CANCEL", name) or f.get("cancelled") is True:
        return "type", "Cancelled Order", "cancelled (file name)" if "CANCEL" in name else "cancelled (reader)"
    if pdt == "proforma_invoice" or "pro forma" in why or re.search(r"PRO\s*-?\s*FORMA", name):
        return "type", "Pro Forma", "pro forma (" + ("reader" if pdt == "proforma_invoice" else "text" if "pro forma" in why else "file name") + ")"
    if pdt == "quotation" or "quotation" in why or re.search(r"\bQUOT(?:E|ATION)|\bQUO\d", name):
        return "type", "Quotation", "quotation (" + ("reader" if pdt == "quotation" else "text" if "quotation" in why else "file name") + ")"
    return None, None, f"left as Other (reader: {pdt or '-'})"


cands = []
for where, pat in (("queue", "out/PurchaseOrders/*.json"), ("hold", "out/PurchaseOrders/hold/*.json")):
    for js in sorted(root.glob(pat)):
        try:
            sc = json.loads(js.read_text(encoding="utf-8"))
        except Exception:
            continue
        if candidate(sc.get("fields") or {}):
            cands.append((where, js, sc))
print(f"{len(cands)} candidate(s): queue {sum(1 for c in cands if c[0] == 'queue')}, hold {sum(1 for c in cands if c[0] == 'hold')}")

rows, tally, seen = [], {}, set()
stamp = datetime.now().isoformat(timespec="seconds")
gone = root / "deleted" / "PurchaseOrders" / datetime.now().strftime("%Y-%m")
for where, js, sc in cands:
    f = sc.setdefault("fields", {})
    act, to, how = decide(where, js, sc)
    if a.only_dn and to != "DeliveryNotes":
        continue
    on = str(f.get("order_number") or "").strip().upper()
    in_sage = bool(re.fullmatch(r"A\d{5}", on)) and on in sage
    linked = bool(re.fullmatch(r"A\d{5}", on)) and (on in orders or in_sage)
    row = {"where": where, "file": js.name, "source": sc.get("source_file"), "reader_type": f.get("po_doc_type"),
           "order_number": on, "order_linked": linked, "order_in_sage": in_sage, "action": act or "none", "to": to or "", "how": how, "result": ""}
    key = f"{where} -> {to or 'unchanged'}"
    tally[key] = tally.get(key, 0) + 1
    pdf = js.with_name(str(sc.get("file") or ""))
    if act == "route":
        if not pdf.is_file():
            row["result"] = "PDF missing"
        elif a.apply:
            dest, meta_dir = root / "in" / to, root / "in" / to / "_meta"
            dest.mkdir(parents=True, exist_ok=True)
            meta_dir.mkdir(parents=True, exist_ok=True)
            src = str(sc.get("source_file") or pdf.name)
            stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(src).stem)[:120]
            name = stem + (f"_p{sc['source_page']}" if sc.get("source_page") else "") + ".pdf"
            k = 2
            while (dest / name).exists() or (meta_dir / (name + ".json")).exists():
                name = f"{stem}_{k}.pdf"
                k += 1
            meta = {"original_name": src, "rerouted_from": str(js.relative_to(root))}
            if sc.get("source_folder"):
                meta["source_folder"] = sc["source_folder"]
            (meta_dir / (name + ".json")).write_text(json.dumps(meta, indent=1), encoding="utf-8")
            shutil.copy2(pdf, dest / (name + ".tmp"))
            os.replace(dest / (name + ".tmp"), dest / name)
            gone.mkdir(parents=True, exist_ok=True)
            for q in (js, pdf):
                t = gone / q.name
                if t.exists():
                    t = gone / f"{q.stem}_rr{q.suffix}"
                shutil.move(str(q), str(t))
            (gone / (js.stem + ".rerouted.json")).write_text(json.dumps({"to": f"in/{to}/{name}", "from": where, "at": stamp}), encoding="utf-8")
            row["result"] = f"sent to in/{to}/{name}"
    elif act == "type":
        row["result"] = "uploads (A-number linked to an order)" if linked else "stays: no linked A-number"
        if a.apply:
            f["retyped"] = {"from": f.get("library_doc_type"), "to": to, "how": how, "at": stamp}
            f["library_doc_type"] = to
            f["order_in_sage"] = in_sage
            f["order_linked"] = linked
            tmp = js.with_name(js.name + ".rt.tmp")
            tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, js)
            if where == "hold" and linked and pdf.is_file():
                q = root / "out" / "PurchaseOrders"
                if not (q / js.name).exists() and not (q / pdf.name).exists():
                    shutil.move(str(pdf), str(q / pdf.name))
                    shutil.move(str(js), str(q / js.name))
                    row["result"] += "; moved from hold to the queue"
    rows.append(row)
    print(f"{where:5s} {act or '-':5s} {to or '':16s} {'link' if linked else '    '} {js.name}  ({how})", flush=True)

rep = root / "reports" / f"retype po other {'applied' if a.apply else 'dry run'} {datetime.now():%Y-%m-%d %H%M}.csv"
with open(rep, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
for k in sorted(tally):
    print(f"  {k}: {tally[k]}")
up = sum(1 for r in rows if r["action"] == "type" and r["order_linked"])
for t in ("Pro Forma", "Quotation", "Cancelled Order"):
    tr = [r for r in rows if r["to"] == t]
    print(f"  {t}: {len(tr)}, linked A-number {sum(1 for r in tr if r['order_linked'])} (queue {sum(1 for r in tr if r['order_linked'] and r['where'] == 'queue')}, hold {sum(1 for r in tr if r['order_linked'] and r['where'] == 'hold')})")
print(f"{up} retyped document(s) with a linked A-number (these upload); report: {rep}"
      + ("" if a.apply else "  (dry run: add --apply)"))

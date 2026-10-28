#!/usr/bin/env python3
"""Sends supplier invoices that the Purchase Orders reader put aside as "Other" to the Supplier Invoices queue
(Dino 26 Oct: "don't upload any other document types from the queue to SharePoint; check if they are likely invoices,
and if likely send them to the Supplier Invoice queue").

Candidates: Purchase Orders sidecars typed "Other" (or read as invoice / not an order / etc.) in the queue
(out/PurchaseOrders), on hold (out/PurchaseOrders/hold) and, with --synced, already synced (synced/PurchaseOrders).
Each PDF's text (its text layer, else Tesseract on the first 2 pages) is scored:
    TAX INVOICE +3 · the word invoice +1 · the reader said invoice +2 · invoice no / inv no +1 · a supplier VAT number +1
    pro forma -3 · credit note -3 · statement -2 · quotation -2 (unless TAX INVOICE) · purchase order form -2
Score >= 3 is "likely an invoice": the PDF is COPIED to in/SupplierInvoices/ with a _meta note (original name, source
folder, rerouted_from) so the invoice reader reads it; the PO copy (queue or hold) is moved to
deleted/PurchaseOrders/<yyyy-mm>/ with a .rerouted.json note. Synced ones are only copied (their SharePoint item stays).
Writes reports/reroute invoices <date>.csv. Dry run unless --apply.

    python3 reroute_inv.py [--root /srv/ocr/scans/pipeline] [--synced] [--apply]
"""
import argparse, csv, json, os, re, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cpu_invoice  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--config", default="/srv/ocr/pipeline/config.json")
ap.add_argument("--synced", action="store_true", help="also 'Other' documents already synced to the Purchase Orders library")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--min-score", type=int, default=3)
ap.add_argument("--from-report", help="a dry run's report: send its likely invoices without reading the PDFs again")
a = ap.parse_args()
root = Path(a.root)
cfg = json.load(open(a.config, encoding="utf-8"))
own = set()
for t in (cfg.get("doc_types") or {}).values():
    own |= {str(v) for v in (t.get("cpu") or {}).get("own_vat_numbers", ())}
dest = root / "in" / "SupplierInvoices"
meta_dir = dest / "_meta"
KEEP = {"purchase_order", "internal_transfer", "proof_of_payment", "polymer_order"}


def candidate(sc):
    f = sc.get("fields") or {}
    if f.get("library_doc_type") == "Other":
        return True
    pdt = str(f.get("po_doc_type") or "")
    return bool(pdt) and pdt not in KEEP and not f.get("order_number")


def score(text, pdt):
    t = re.sub(r"\s+", " ", text or "").upper()
    s, why = 0, []
    def add(n, w):
        nonlocal s
        s += n
        why.append(f"{w} {n:+d}")
    tax = bool(re.search(r"TAX\s*INVOICE", t))
    if tax:
        add(3, "TAX INVOICE")
    elif re.search(r"\bINVOICE\b", t):
        add(1, "invoice")
    if pdt == "invoice":
        add(2, "reader: invoice")
    if re.search(r"\bINV(?:OICE)?\.?\s*(?:NO|NUMBER|NR|#)\b", t):
        add(1, "invoice no")
    vat = [v for v in dict.fromkeys(cpu_invoice.VATNO.findall(t)) if not cpu_invoice._is_own(v, own)]
    if vat:
        add(1, "supplier VAT " + vat[0])
    if re.search(r"PRO\s*-?\s*FORMA", t) or pdt == "proforma_invoice":
        add(-3, "pro forma")
    if re.search(r"CREDIT\s*NOTE", t):
        add(-3, "credit note")
    if re.search(r"\bSTATEMENT\b", t) and not tax:
        add(-2, "statement")
    if (re.search(r"\bQUOTATION\b|\bQUOTE\s*NO", t) or pdt == "quotation") and not tax:
        add(-2, "quotation")
    if re.search(r"MINING\s*PRESSURE\s*SYSTEMS.{0,200}PURCHASE\s*ORDER|OFFICIAL\s*ORDER", t) and not tax:
        add(-2, "PO form")
    return s, "; ".join(why)


cands = []
for where, pat in (("queue", "out/PurchaseOrders/*.json"), ("hold", "out/PurchaseOrders/hold/*.json")) + \
        ((("synced", "synced/PurchaseOrders/*/*.json"),) if a.synced else ()):
    for js in sorted(root.glob(pat)):
        if js.name.endswith((".deleted.json", ".rerouted.json")):
            continue
        try:
            sc = json.loads(js.read_text(encoding="utf-8"))
        except Exception:
            continue
        if candidate(sc):
            cands.append((where, js, sc))
print(f"{len(cands)} candidate(s): " + ", ".join(f"{w} {sum(1 for c in cands if c[0] == w)}" for w in ("queue", "hold", "synced")))

prev = None
if a.from_report:
    with open(a.from_report, encoding="utf-8-sig", newline="") as fh:
        prev = {(r["where"], r["file"]): r for r in csv.DictReader(fh)}
    print(f"using {a.from_report}: {sum(1 for r in prev.values() if r.get('likely') == 'yes')} likely invoice(s) in it")
rows, seen, n = [], set(), 0
for where, js, sc in cands:
    f = sc.get("fields") or {}
    pdf = js.with_name(str(sc.get("file") or ""))
    if not pdf.is_file():
        rows.append({"where": where, "file": js.name, "reader_type": f.get("po_doc_type"), "score": "", "likely": "", "why": "PDF missing"})
        continue
    if prev is not None:
        r0 = prev.get((where, js.name))
        if not r0:
            continue                                          # not in the dry run: left for the next one
        s, why, how = int(r0.get("score") or 0), r0.get("why") or "", r0.get("text") or ""
        likely = r0.get("likely") == "yes"
    else:
        try:
            text, how = cpu_invoice.get_text(pdf, max_pages=2)
        except Exception as ex:
            text, how = "", f"text failed: {ex}"
        s, why = score(text, str(f.get("po_doc_type") or ""))
        likely = s >= a.min_score
    key = (sc.get("source_sha256"), sc.get("source_page"))
    dup = likely and key in seen and key != (None, None)
    seen.add(key)
    rows.append({"where": where, "file": js.name, "source": sc.get("source_file"), "reader_type": f.get("po_doc_type"),
                 "supplier": f.get("supplier"), "score": s, "likely": "yes" if likely else "no", "text": how,
                 "why": why + ("; same page already sent" if dup else "")})
    print(f"{'INV' if likely else ' --'} {s:3d}  {where:6s} {js.name}  ({why})", flush=True)
    if not likely or dup:
        continue
    n += 1
    if not a.apply:
        continue
    dest.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    src = str(sc.get("source_file") or pdf.name)
    stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(src).stem)[:120]
    name = stem + (f"_p{sc['source_page']}" if sc.get("source_page") else "") + ".pdf"
    k = 2
    while (dest / name).exists() or (meta_dir / (name + ".json")).exists():
        name = f"{stem}_{k}.pdf"
        k += 1
    meta = {"original_name": src, "rerouted_from": str(js.relative_to(root)), "reroute_score": s}
    if sc.get("source_folder"):
        meta["source_folder"] = sc["source_folder"]
    (meta_dir / (name + ".json")).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    shutil.copy2(pdf, dest / (name + ".tmp"))
    os.replace(dest / (name + ".tmp"), dest / name)
    rows[-1]["sent_as"] = f"in/SupplierInvoices/{name}"
    if where in ("queue", "hold"):
        gone = root / "deleted" / "PurchaseOrders" / datetime.now().strftime("%Y-%m")
        gone.mkdir(parents=True, exist_ok=True)
        for q in (js, pdf):
            target = gone / q.name
            if target.exists():
                target = gone / f"{q.stem}_rr{q.suffix}"
            shutil.move(str(q), str(target))
        (gone / (js.stem + ".rerouted.json")).write_text(json.dumps({"to": f"in/SupplierInvoices/{name}", "from": where,
                                                                      "at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")

rep = root / "reports" / f"reroute invoices {'sent' if a.apply else 'dry run'} {datetime.now():%Y-%m-%d %H%M}.csv"
cols = ["where", "file", "source", "reader_type", "supplier", "score", "likely", "text", "why", "sent_as"]
with open(rep, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
print(f"{n} likely invoice(s) {'sent to in/SupplierInvoices' if a.apply else 'would be sent (dry run: add --apply)'}; report: {rep}")

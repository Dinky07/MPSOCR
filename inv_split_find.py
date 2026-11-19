#!/usr/bin/env python3
"""Finds supplier invoices that were read as ONE invoice although the scan holds several (Dino 19 Nov 2026: "there is
a file here with a few invoices but it is not split and only the first page is captured ... possibly some already
saved in the SharePoint"). Report only: nothing is moved, re-queued or deleted.

Looks at every Supplier Invoices sidecar with a PDF of 2+ pages
    out/SupplierInvoices/ and out/SupplierInvoices/hold/      waiting (not uploaded yet)
    out/SupplierInvoices/*.json.uploaded present               uploaded, not moved to synced/ yet
    synced/SupplierInvoices/<yyyy-mm>/                        in SharePoint
and the scans still queued in in/SupplierInvoices/ (they will be split when the new worker reads them).
Each page is read once (text layer, else Tesseract) and inv_split.groups() decides where each invoice starts, exactly
as the worker now does. Writes reports/multi-invoice scans <date>.csv with one row per affected file: where it is, its
pages, the invoices found (pages and numbers), the number the pipeline captured, and the SharePoint item (webUrl) for
uploaded ones.

    python3 inv_split_find.py [--root /srv/ocr/scans/pipeline] [--type SupplierInvoices] [--since 2026-11-01] [--limit N]
"""
import argparse, csv, glob, json, os, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inv_split  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
    ap.add_argument("--type", default="SupplierInvoices")
    ap.add_argument("--since", help="only sidecars processed on or after this date (yyyy-mm-dd)")
    ap.add_argument("--limit", type=int, default=0, help="stop after reading this many multi-page files")
    a = ap.parse_args()
    R, T = Path(a.root), a.type

    cands = []                                                   # (where, sidecar or None, pdf)
    for where, pat in (("waiting", f"out/{T}/*.json"), ("hold", f"out/{T}/hold/*.json"), ("synced", f"synced/{T}/*/*.json")):
        for p in sorted(glob.glob(str(R / pat))):
            js = Path(p)
            if js.name.endswith((".deleted.json", ".rerouted.json", ".cpu.json")):
                continue
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if sc.get("doc_type") != "supplier_invoice" or sc.get("invoice_split"):
                continue                                         # already one part of a split file
            if a.since and str(sc.get("processed_at") or "")[:10] < a.since:
                continue
            w = "uploaded" if where == "waiting" and Path(p + ".uploaded").exists() else where
            cands.append((w, js, sc, js.with_name(str(sc.get("file") or ""))))
    for p in sorted((R / "in" / T).glob("*.pdf")):
        cands.append(("queued", None, {}, p))

    rows, read, multi_page = [], 0, 0
    for where, js, sc, pdf in cands:
        if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
            continue
        try:
            n = inv_split.npages(pdf)
        except Exception as e:
            print(f"  cannot read {pdf.name}: {e}")
            continue
        if n < 2:
            continue
        multi_page += 1
        if a.limit and read >= a.limit:
            break
        read += 1
        try:
            texts = inv_split.page_texts(pdf)
        except Exception as e:
            print(f"  OCR failed on {pdf.name}: {e}")
            continue
        grps, infos = inv_split.groups([t for t, _ in texts])
        if len(grps) < 2:
            continue
        sp = {}
        if js is not None and Path(str(js) + ".uploaded").exists():
            try:
                sp = json.load(open(str(js) + ".uploaded", encoding="utf-8-sig"))
            except (OSError, ValueError):
                pass
        f = sc.get("fields") or {}
        nos = [infos[g[0] - 1]["invoice_number"] or "?" for g in grps]
        rows.append({"where": where, "sidecar": str(js.relative_to(R)) if js else "", "pdf": str(pdf.relative_to(R)),
                     "source_file": sc.get("source_file") or pdf.name, "pages": n, "invoices": len(grps),
                     "groups": " ".join("-".join(map(str, (g[0], g[-1]) if len(g) > 1 else (g[0],))) for g in grps),
                     "invoice_numbers_found": " ".join(nos), "captured_supplier": f.get("supplier_name") or "",
                     "captured_invoice_number": f.get("invoice_number") or "", "captured_total": f.get("total") or "",
                     "checked": bool(f.get("checked")), "processed_at": sc.get("processed_at") or "",
                     "sharepoint_url": sp.get("webUrl") or "", "drive_id": sp.get("driveId") or "", "item_id": sp.get("itemId") or ""})
        print(f"{where:8} {rows[-1]['source_file']}: {n} pages -> {len(grps)} invoices {nos}  (captured {rows[-1]['captured_invoice_number'] or '-'})")

    out = R / "reports" / f"multi-invoice scans {datetime.now():%Y-%m-%d %H%M}.csv"
    if rows:
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    by = {}
    for r in rows:
        by[r["where"]] = by.get(r["where"], 0) + 1
    print(json.dumps({"sidecars_and_queued": len(cands), "multi_page": multi_page, "read": read,
                      "multi_invoice_files": len(rows), "by_where": by,
                      "invoices_inside": sum(r["invoices"] for r in rows), "report": str(out) if rows else None}, indent=1))


if __name__ == "__main__":
    main()

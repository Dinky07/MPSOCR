#!/usr/bin/env python3
"""Hand-labelled test set for the pre-sort / sorter (review 27 Oct: score on raw scans, labelled by a person, not on the
pipeline's own labels). Picks ~400 pages of ORIGINAL scans from done/<Queue>/<yyyy-mm>/ - mostly the Purchase Orders
queue (where misfiled documents were), weighted towards pages the pipeline did not read as an order, plus multi-document
delivery-note batches, test certificates, workshop sheets, job transfers, supplier invoices and proofs of payment - and
writes work/sorter/testset/:
  testset.xlsx   one row per page: the picture (link), what the pipeline made of it (pre-filled), and two columns for
                 Dino: TYPE (dropdown, pre-filled) and NEW DOC? (Y = this page starts a new document, pre-filled)
  png/<id>.png   each page at 110 dpi;  pdf/<id>.pdf  the original file
  pages.json     the selection (file, page, sha256, pre-fill) for presort_eval.py
Read-only for the pipeline: copies only.

    python3 testset_build.py [--pages 400] [--seed 27]
"""
import argparse, collections, csv, glob, hashlib, json, os, random, re, shutil, subprocess
from pathlib import Path

ROOT = Path("/srv/ocr/scans/pipeline")
OUT = ROOT / "work" / "sorter" / "testset"
WIN = r"O:\pipeline\work\sorter\testset"
TYPES = [("purchase_order", "MPS / Kusasa purchase order or IGT form (incl. cancelled / amended)"),
         ("polymer_order", "Polymer Liners order"), ("pro_forma", "pro forma invoice"), ("quotation", "supplier quotation"),
         ("supplier_invoice", "supplier tax invoice"), ("credit_note", "supplier credit note"), ("statement", "supplier statement"),
         ("proof_of_payment", "bank payment confirmation / proof of payment"),
         ("delivery_note", "MPS's own delivery note (IN number)"), ("supplier_delivery_note", "a supplier's delivery note / waybill"),
         ("job_transfer", "MPS job transfer note"), ("workshop_sheet", "workshop / fabrication drawing sheet"),
         ("test_cert", "mill test / inspection certificate"), ("other", "anything else (letter, enquiry, contract, blank page...)")]
WANT = {"PurchaseOrders": 260, "DeliveryNotes": 40, "TestCerts": 25, "WorkshopSheets": 20, "JobTransfers": 20,
        "SupplierInvoices": 20, "ProofOfPayments": 15}
MAX_PAGES = 6


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def pages_of(p):
    try:
        return int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True, timeout=60).stdout).group(1))
    except Exception:
        return 0


def kind_of(sc, folder):
    """What the pipeline made of it, in test-set types."""
    dt, f = sc.get("doc_type"), sc.get("fields") or {}
    if folder == "PolymerOrders" or f.get("form") == "polymer_order":
        return "polymer_order"
    if dt == "po":
        lib, pdt = f.get("library_doc_type") or "", f.get("po_doc_type") or ""
        for k, v in (("Proof of Payment", "proof_of_payment"), ("Pro Forma", "pro_forma"), ("Quotation", "quotation"),
                     ("Purchase Order", "purchase_order"), ("IGT", "purchase_order"), ("Cancelled Order", "purchase_order")):
            if lib.startswith(k):
                return v
        return {"invoice": "supplier_invoice", "delivery_note": "supplier_delivery_note", "job_transfer": "job_transfer",
                "quotation": "quotation", "proforma_invoice": "pro_forma"}.get(pdt, "other")
    if dt == "delivery_note":
        return "other" if "not_a_delivery_note" in (sc.get("flags") or []) else "delivery_note"
    return {"supplier_invoice": "supplier_invoice", "pop": "proof_of_payment", "jt": "job_transfer", "ws": "workshop_sheet",
            "test_cert": "test_cert"}.get(dt, "other")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=400)
    ap.add_argument("--seed", type=int, default=27)
    a = ap.parse_args()
    random.seed(a.seed)
    # what the pipeline made of every original page: source_sha256 -> {page: kind}
    idx = collections.defaultdict(dict)
    reroute = {}
    for p in glob.glob(str(ROOT / "deleted" / "**" / "*.rerouted.json"), recursive=True):
        try:
            to = json.load(open(p)).get("to", "")
            reroute[Path(p).name.replace(".rerouted.json", ".json")] = to.split("/")[1] if to.count("/") >= 2 else ""
        except Exception:
            pass
    q2kind = {"SupplierInvoices": "supplier_invoice", "DeliveryNotes": "delivery_note", "JobTransfers": "job_transfer"}
    pats = ["out/*/*.json", "out/*/hold/*.json", "synced/*/*/*.json", "deleted/**/*.json"]
    for pat in pats:
        for p in glob.glob(str(ROOT / pat), recursive=True):
            if p.endswith((".rerouted.json", ".deleted.json")):
                continue
            try:
                sc = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(sc, dict) or not sc.get("source_sha256"):
                continue
            folder = Path(p).relative_to(ROOT).parts[1]
            k = kind_of(sc, folder)
            if Path(p).name in reroute:                      # sent on to another queue this week
                k = q2kind.get(reroute[Path(p).name], k)
                if k == "delivery_note" and (sc.get("fields") or {}).get("po_doc_type") == "delivery_note":
                    k = "supplier_delivery_note"
            sp = sc.get("source_pages") or (sc.get("fields") or {}).get("source_pages")
            pages = []
            if isinstance(sp, list):
                pages = [int(x) for x in sp if str(x).isdigit()]
            elif sp:                                         # "3", "3, 4" or "3-5"
                for part in re.split(r"[,;\s]+", str(sp)):
                    m = re.fullmatch(r"(\d+)-(\d+)", part)
                    pages += list(range(int(m.group(1)), int(m.group(2)) + 1)) if m else ([int(part)] if part.isdigit() else [])
            if not pages:
                pages = [int(sc["source_page"])] if str(sc.get("source_page") or "").isdigit() else [1]
            for i, pg in enumerate(pages):
                idx[sc["source_sha256"].lower()].setdefault(pg, (k, i == 0))
    print(f"pipeline labels for {len(idx)} original files")
    rows, n = [], 0
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "png").mkdir(parents=True)
    (OUT / "pdf").mkdir()
    for queue, want in WANT.items():
        files = glob.glob(str(ROOT / "done" / queue / "*" / "*.pdf"))
        random.shuffle(files)
        cand = []
        for f in files[:3000]:
            h = sha256(f).lower()
            lab = idx.get(h)
            if not lab:
                continue
            not_po = any(k != "purchase_order" for k, _ in lab.values())
            cand.append((0 if (queue == "PurchaseOrders" and not_po) else 1, random.random(), f, h, lab))
        cand.sort()
        got = 0
        # PO queue: about 55 % of its pages from files that were not (only) orders, the rest ordinary orders (the common
        # case, where a wrong verdict costs most); other queues: as they come
        if queue == "PurchaseOrders":
            plan = [(c, int(want * 0.55)) for c in cand if c[0] == 0] + [(c, want) for c in cand if c[0] == 1]
        else:
            plan = [(c, want) for c in cand]
        for (_, _, f, h, lab), budget in plan:
            if got >= budget:
                continue
            np_ = pages_of(f)
            if not np_:
                continue
            fid = f"{queue[:3].upper()}{n:04d}"
            n += 1
            shutil.copy2(f, OUT / "pdf" / f"{fid}.pdf")
            for pg in range(1, min(np_, MAX_PAGES) + 1):
                pid = f"{fid}-p{pg}"
                subprocess.run(["pdftoppm", "-r", "110", "-png", "-f", str(pg), "-l", str(pg), "-singlefile", f, str(OUT / "png" / pid)],
                               capture_output=True, timeout=120)
                k, first = lab.get(pg, (None, False))
                if k is None:                                # a page the reader did not output on its own: continuation
                    prev = rows[-1]["pipeline"] if rows and rows[-1]["file_id"] == fid else ""
                    k, first = prev or "other", pg == 1
                rows.append({"id": pid, "file_id": fid, "queue": queue, "original": Path(f).name, "page": pg, "pages": np_,
                             "sha256": h, "pipeline": k, "new_doc": "Y" if (first or pg == 1) else "N"})
                got += 1
        print(f"{queue}: {got} pages")
    json.dump(rows, open(OUT / "pages.json", "w"), indent=0)

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pages"
    head = ["ID", "Queue", "Original file", "Page", "of", "Picture", "PDF", "Pipeline says", "TYPE (correct me)", "NEW DOC? (Y/N)", "Note"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    fill = PatternFill("solid", fgColor="FFF6D5")
    for r in rows:
        ws.append([r["id"], r["queue"], r["original"], r["page"], r["pages"], "picture", "PDF", r["pipeline"], r["pipeline"], r["new_doc"], ""])
        i = ws.max_row
        ws.cell(i, 6).hyperlink = f"{WIN}\\png\\{r['id']}.png"
        ws.cell(i, 7).hyperlink = f"{WIN}\\pdf\\{r['file_id']}.pdf"
        for col in (6, 7):
            ws.cell(i, col).font = Font(color="0563C1", underline="single")
        for col in (9, 10):
            ws.cell(i, col).fill = fill
    dv = DataValidation(type="list", formula1="=Types!$A$2:$A$%d" % (len(TYPES) + 1), allow_blank=False)
    dv2 = DataValidation(type="list", formula1='"Y,N"', allow_blank=False)
    ws.add_data_validation(dv)
    ws.add_data_validation(dv2)
    dv.add(f"I2:I{ws.max_row}")
    dv2.add(f"J2:J{ws.max_row}")
    for col, w in zip("ABCDEFGHIJK", (12, 15, 48, 6, 5, 9, 6, 22, 22, 14, 30)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    t = wb.create_sheet("Types")
    t.append(["Type", "Meaning"])
    for k, d in TYPES:
        t.append([k, d])
    t.column_dimensions["A"].width = 24
    t.column_dimensions["B"].width = 80
    h = wb.create_sheet("How")
    for line in ["Check each page: open the picture, then set TYPE and NEW DOC?.",
                 "TYPE and NEW DOC? are pre-filled with what the pipeline made of the page - correct only what is wrong.",
                 "NEW DOC? = Y when this page starts a new document (page 1 of an invoice, the first page of the next delivery note in a batch); N when it continues the page above.",
                 "Pages of the same file are listed one after another. Use 'other' for blank pages and anything that is not one of the types.",
                 "Save the file under the same name when done; presort_eval.py reads it from O:\\pipeline\\work\\sorter\\testset."]:
        h.append([line])
    h.column_dimensions["A"].width = 140
    wb.save(OUT / "testset.xlsx")
    c = collections.Counter(r["pipeline"] for r in rows)
    print(f"{len(rows)} pages from {n} files -> {OUT / 'testset.xlsx'}")
    print("pre-filled types:", dict(c.most_common()))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Labelling sheet for the sorter (Dino 28 Oct 17:34): about 150 documents on the Purchase / Polymer Orders hold that the
pre-sort could not place ("unsure"), plus some it only thought "likely" (kinds not trusted to move on their own), for
Dino to label like testset.xlsx. The labels then go into the pre-sort's rules / scoring (presort eval), and the hold is
re-sorted once it scores well on them.

    python3 sorter_labels.py [--n 150] [--likely 30] [--seed 28]

Reads the latest applied hold re-sort report (reports/hold resort <stamp>.csv, the pre-sort's verdict per hold document)
and keeps documents still on hold, not checked, not uploaded. Mixes them across what the pipeline made of them, at most
2 per original scan. Writes work/sorter/labels/ (O:\\pipeline\\work\\sorter\\labels):
  labels.xlsx    one row per document: page-1 picture, the PDF, what the pipeline and the pre-sort said, and TYPE for Dino
                 (blank for unsure ones, pre-filled with the pre-sort's guess for likely ones), plus MORE THAN ONE DOC?
  png/<id>.png   page 1 at 110 dpi;  pdf/<id>.pdf  the hold PDF (a copy)
  items.json     id -> hold file, original file, sha256 / content hash, verdict
Types: the Types sheet of work/sorter/testset/testset.xlsx (Dino's list, with the types he added). Read-only for the
pipeline: copies only.
"""
import argparse, csv, glob, json, random, re, shutil, subprocess, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/srv/ocr/scans/pipeline")
OUT = ROOT / "work" / "sorter" / "labels"
TESTSET = ROOT / "work" / "sorter" / "testset" / "testset.xlsx"
BASE_TYPES = [("purchase_order", "MPS / Kusasa purchase order or IGT form (incl. cancelled / amended)"),
              ("polymer_order", "Polymer Liners order"), ("pro_forma", "pro forma invoice"), ("quotation", "supplier quotation"),
              ("supplier_invoice", "supplier tax invoice"), ("credit_note", "supplier credit note"), ("statement", "supplier statement"),
              ("proof_of_payment", "bank payment confirmation / proof of payment"),
              ("delivery_note", "MPS's own delivery note (IN number)"), ("supplier_delivery_note", "a supplier's delivery note / waybill"),
              ("job_transfer", "MPS job transfer note"), ("workshop_sheet", "workshop / fabrication drawing sheet"),
              ("test_cert", "mill test / inspection certificate"), ("other", "anything else (letter, enquiry, contract, blank page...)")]


def types():
    """Dino's list from the test set (Types sheet + any TYPE value he used), else the base list."""
    out = dict(BASE_TYPES)
    try:
        import openpyxl
        wb = openpyxl.load_workbook(TESTSET, read_only=True, data_only=True)
        if "Types" in wb.sheetnames:
            for r in list(wb["Types"].iter_rows(values_only=True))[1:]:
                if r and r[0]:
                    out.setdefault(str(r[0]).strip(), str(r[1] or "").strip() if len(r) > 1 else "")
        it = wb["Pages"].iter_rows(values_only=True)
        hdr = [str(x or "") for x in next(it)]
        ti = next(i for i, h in enumerate(hdr) if h.upper().startswith("TYPE"))
        for r in it:
            if r and len(r) > ti and r[ti]:
                out.setdefault(str(r[ti]).strip(), "(added by Dino in the test set)")
    except Exception as e:
        print("test set types not read:", e)
    return list(out.items())


def pipeline_says(sc):
    f = sc.get("fields") or {}
    return f.get("library_doc_type") or f.get("po_doc_type") or f.get("form") or sc.get("doc_type") or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--likely", type=int, default=30)
    ap.add_argument("--seed", type=int, default=28)
    ap.add_argument("--allowed", default="delivery_note:sure,supplier_invoice:sure,job_transfer:sure,proof_of_payment:likely,"
                                          "test_cert:likely,workshop_sheet:likely")
    a = ap.parse_args()
    random.seed(a.seed)
    reps = sorted(p for p in glob.glob(str(ROOT / "reports" / "hold resort *.csv")) if "dry run" not in p)
    assert reps, "no applied hold re-sort report"
    verdict = {}
    for p in reps:                                       # later reports win
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            verdict[(r["type"], r["file"])] = r
    print("verdicts from", len(reps), "reports, latest", Path(reps[-1]).name)
    allowed = set(x for x in a.allowed.split(",") if x)
    unsure, likely = defaultdict(list), defaultdict(list)
    for t in ("PurchaseOrders", "PolymerOrders"):
        for js in glob.glob(str(ROOT / "out" / t / "hold" / "*.json")):
            js = Path(js)
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            f = sc.get("fields") or {}
            pdf = js.parent / str(sc.get("file") or "")
            v = verdict.get((t, pdf.name))
            if not v or f.get("checked") or not pdf.is_file() or Path(str(js) + ".uploaded").exists():
                continue
            if f.get("form") in ("mps_order", "polymer_order", "internal_group_transfer") and f.get("order_number"):
                continue
            item = (t, js, sc, pdf, v)
            key = f"{v['kind']}:{v['strength']}"
            if v["kind"] in ("unsure", "error", "") or v["strength"] == "unsure":
                unsure[pipeline_says(sc)].append(item)
            elif key not in allowed:
                likely[key].append(item)
    print("unsure on hold:", sum(map(len, unsure.values())), dict(Counter({k: len(v) for k, v in unsure.items()}).most_common(12)))
    print("likely, not trusted:", {k: len(v) for k, v in likely.items()})

    def pick(groups, n):
        for g in groups.values():
            random.shuffle(g)
        got, per_src, keys = [], Counter(), sorted(groups, key=lambda k: -len(groups[k]))
        while len(got) < n and any(groups[k] for k in keys):
            for k in keys:
                while groups[k]:
                    it = groups[k].pop()
                    src = it[2].get("source_sha256") or it[3].name
                    if per_src[src] >= 2:
                        continue
                    per_src[src] += 1
                    got.append(it)
                    break
                if len(got) >= n:
                    break
        return got
    chosen = [("unsure", x) for x in pick(unsure, max(a.n - a.likely, 0))]
    chosen += [("likely", x) for x in pick(likely, a.n - len(chosen))]
    if len(chosen) < a.n:                                # not enough likely ones: more unsure
        chosen += [("unsure", x) for x in pick(unsure, a.n - len(chosen))]

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "png").mkdir(parents=True)
    (OUT / "pdf").mkdir()
    items = []
    for i, (bucket, (t, js, sc, pdf, v)) in enumerate(chosen, 1):
        iid = f"L{i:04d}"
        shutil.copy2(pdf, OUT / "pdf" / f"{iid}.pdf")
        subprocess.run(["pdftoppm", "-r", "110", "-png", "-f", "1", "-l", "1", "-singlefile", str(pdf), str(OUT / "png" / iid)],
                       capture_output=True, timeout=120)
        try:
            npages = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True,
                                                                     timeout=60).stdout).group(1))
        except Exception:
            npages = None
        f = sc.get("fields") or {}
        items.append({"id": iid, "bucket": bucket, "type": t, "hold_file": f"out/{t}/hold/{pdf.name}", "json": js.name,
                      "source_file": sc.get("source_file"), "source_folder": sc.get("source_folder"), "pages": npages,
                      "source_sha256": sc.get("source_sha256"), "content_hash": f.get("content_hash"),
                      "pipeline": pipeline_says(sc), "kind": v.get("kind"), "strength": v.get("strength"),
                      "signals": v.get("signals"), "summary": f.get("summary") or ""})
    json.dump(items, open(OUT / "items.json", "w"), indent=1)

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.datavalidation import DataValidation
    tl = types()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Documents"
    head = ["ID", "Queue", "Original file", "Original folder", "Pages", "Picture (p1)", "PDF", "Pipeline says", "Pre-sort guess",
            "Why (pre-sort)", "TYPE", "MORE THAN ONE DOC? (Y)", "Note"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
    fill = PatternFill("solid", fgColor="FFF6D5")
    for it in items:
        guess = "" if it["bucket"] == "unsure" else f"{it['kind']} ({it['strength']})"
        ws.append([it["id"], it["type"].replace("Orders", " Orders hold"), it["source_file"], it["source_folder"], it["pages"],
                   "picture", "PDF", it["pipeline"], guess, str(it["signals"] or "")[:200],
                   it["kind"] if it["bucket"] == "likely" else "", "", ""])
        r = ws.max_row
        ws.cell(r, 6).hyperlink = f"png/{it['id']}.png"   # relative to the workbook (Excel reads O:\\ as a URL)
        ws.cell(r, 7).hyperlink = f"pdf/{it['id']}.pdf"
        for col in (6, 7):
            ws.cell(r, col).font = Font(color="0563C1", underline="single")
        for col in (11, 12):
            ws.cell(r, col).fill = fill
        ws.cell(r, 10).alignment = Alignment(wrap_text=False)
    dv = DataValidation(type="list", formula1="=Types!$A$2:$A$%d" % (len(tl) + 1), allow_blank=True)
    dv2 = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(dv)
    ws.add_data_validation(dv2)
    dv.add(f"K2:K{ws.max_row}")
    dv2.add(f"L2:L{ws.max_row}")
    for col, w in zip("ABCDEFGHIJKLM", (8, 20, 44, 34, 6, 10, 6, 22, 22, 40, 22, 12, 30)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    t = wb.create_sheet("Types")
    t.append(["Type", "Meaning"])
    for k, d in tl:
        t.append([k, d])
    t.column_dimensions["A"].width = 26
    t.column_dimensions["B"].width = 80
    h = wb.create_sheet("How")
    for line in ["Documents on the Purchase / Polymer Orders hold that the sorter could not place. One row per document.",
                 "Open the picture (page 1) or the PDF, then set TYPE from the list (Types sheet). Same types as testset.xlsx.",
                 "Rows with a pre-sort guess have TYPE pre-filled with it: correct only what is wrong.",
                 "MORE THAN ONE DOC? = Y when the PDF holds more than one document (e.g. an order with an invoice behind it).",
                 "Note: anything useful, e.g. who it is from or where it should go.",
                 "Save under the same name when done (O:\\pipeline\\work\\sorter\\labels\\labels.xlsx) and tell Claude."]:
        h.append([line])
    h.column_dimensions["A"].width = 130
    wb.save(OUT / "labels.xlsx")
    print(json.dumps({"documents": len(items), "unsure": sum(1 for i in items if i["bucket"] == "unsure"),
                      "likely": sum(1 for i in items if i["bucket"] == "likely"),
                      "pipeline_says": dict(Counter(i["pipeline"] for i in items).most_common(12)),
                      "guesses": dict(Counter(f"{i['kind']}:{i['strength']}" for i in items if i["bucket"] == "likely")),
                      "types": len(tl), "sheet": str(OUT / "labels.xlsx")}, indent=1))


if __name__ == "__main__":
    main()

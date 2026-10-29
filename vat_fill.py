#!/usr/bin/env python3
"""Supplier VAT numbers the reader missed (Dino 29 Oct 12:46: Daimler 1715229263 has it, 1715236354 not - it is printed
plainly, "VAT Reg No. 4020282630", but the scan has no text layer, so fix_from_text could not help).

For every supplier invoice waiting in out/SupplierInvoices (and its hold) with no supplier VAT number:
  1. the page text: the PDF's text layer, else the Tesseract text (route_sort / od_sort cache) - a "VAT (Reg) No"
     followed by a 4xxxxxxxxx number that is not MPS's own (4320123039). Taken when there is exactly one such number,
     or when one of them is already known for this supplier (Sage supplier list or earlier invoices).
  2. else the same supplier: the supplier name matches one Sage supplier (suppliers.csv) with a VAT number, or every
     earlier invoice from this supplier name that has a VAT number agrees on one number (at least 2).
Only empty fields are filled. Each fill is recorded in fields.text_fixes ({field, value, how}) and in edits, so the
dashboard shows it and the PC sync's -ReingestFixed knows the document changed.
    python3 vat_fill.py [--apply] [--jobs 4]
Report: reports/vat fill <stamp>[ dry run].csv / .json
"""
import argparse, csv, glob, json, re, subprocess, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402  text_of() (Tesseract text, cached)
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
OWN = {"4320123039"}
VAT_RX = re.compile(r"(?i)\bV\.?A\.?T\.?\s*(?:Reg(?:istration)?\.?\s*)?(?:No|Number|Nr|#)?\.?\s*[:.,]*\s*(4\d{3}\s?\d{3}\s?\d{3})\b")
SKIP = {"THE", "PTY", "LTD", "CC", "AND", "TA", "INC", "SA", "GROUP", "SOUTHERN", "AFRICA", "SOUTH", "LIMITED", "HOLDINGS"}


def words(name):
    return [w for w in (re.sub(r"[^A-Z0-9]", "", x) for x in re.split(r"[\s\-_,/&().]+", (name or "").upper())) if len(w) >= 3 and w not in SKIP]


def text_layer(pdf):
    try:
        return subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    # Sage suppliers with a VAT number, by their name words
    sage = []
    with open(ROOT / "suppliers.csv", newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            v = re.sub(r"\D", "", r.get("VatNo") or "")
            if len(v) == 10 and v not in OWN:
                sage.append((set(words(r.get("Name"))), v, (r.get("Name") or "").strip()))
    sage_vats = {v for _, v, _ in sage}
    # what earlier invoices say per supplier name
    seen = defaultdict(Counter)
    todo = []
    for pat in ("synced/SupplierInvoices*/*/*.json", "out/SupplierInvoices*/*.json", "out/SupplierInvoices*/hold/*.json"):
        for p in glob.glob(str(ROOT / pat)):
            if p.endswith((".tmp", ".lock")):
                continue
            try:
                sc = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            if sc.get("doc_type") != "supplier_invoice" or "supplier_is_mps" in (sc.get("flags") or []):
                continue
            f = sc.get("fields") or {}
            v = re.sub(r"\D", "", str(f.get("supplier_vat_no") or ""))
            name = " ".join(words(f.get("supplier_name")))
            if v:
                if name:
                    seen[name][v] += 1
            elif "/out/" in p and not Path(p + ".uploaded").exists():
                pdf = Path(p).parent / str(sc.get("file") or "")
                if pdf.is_file():
                    todo.append((Path(p), sc, pdf))

    def read(it):
        js, sc, pdf = it
        t = text_layer(pdf)
        if not re.search(r"\d", t or ""):
            try:
                t = route_sort.text_of(pdf)
            except Exception:
                t = ""
        return it, t
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = list(ex.map(read, todo))
    route_sort.save_index()
    rows, cnt = [], Counter()
    for (js, sc, pdf), text in res:
        f = sc.get("fields") or {}
        name = f.get("supplier_name")
        nw = set(words(name))
        known = set(seen.get(" ".join(words(name)), {}))
        sage_hit = [(v, n) for ws, v, n in sage if nw and ws and (nw == ws or (len(nw & ws) >= 2 and (nw <= ws or ws <= nw)))]
        known |= {v for v, _ in sage_hit}
        cands = list(dict.fromkeys(re.sub(r"\s", "", m.group(1)) for m in VAT_RX.finditer(text or "")))
        cands = [c for c in cands if c not in OWN]
        vat, how = None, None
        if len(cands) == 1:
            vat, how = cands[0], "page text"
        elif len(cands) > 1 and len(set(cands) & known) == 1:
            vat, how = (set(cands) & known).pop(), "page text (the supplier's known number)"
        elif not cands:
            sv = {v for v, _ in sage_hit}
            if len(sv) == 1:
                vat, how = sv.pop(), f"Sage supplier {sage_hit[0][1]}"
            else:
                c = seen.get(" ".join(words(name)))
                if c and len(c) == 1 and sum(c.values()) >= 2:
                    vat, how = next(iter(c)), f"earlier invoices from {name} ({sum(c.values())})"
        result = "no VAT found" if not vat else ("would fill" if not a.apply else "filled")
        if vat and a.apply:
            try:
                with sidecar_lock.locked(js, "vat_fill"):
                    sc2 = json.load(open(js, encoding="utf-8"))
                    f2 = sc2.setdefault("fields", {})
                    if f2.get("supplier_vat_no"):
                        result = "filled meanwhile"
                    else:
                        f2["supplier_vat_no"] = vat
                        f2.setdefault("text_fixes", []).append({"field": "supplier_vat_no", "value": vat, "how": how, "by": "vat_fill", "at": stamp})
                        sc2.setdefault("edits", []).append({"at": stamp, "by": f"vat_fill ({how})",
                                                            "changes": [{"field": "supplier_vat_no", "old": None, "new": vat}]})
                        tmp = js.with_name(js.name + ".vf.tmp")
                        tmp.write_text(json.dumps(sc2, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                        tmp.replace(js)
            except sidecar_lock.Locked:
                result = "locked (next run)"
        cnt[f"{result}{' - ' + how.split(' (')[0].split(' from')[0] if vat else ''}"] += 1
        rows.append({"file": js.name, "supplier": name, "invoice": f.get("invoice_number"), "vat": vat, "how": how,
                     "candidates": ", ".join(cands), "in_sage": (vat in sage_vats) if vat else None, "result": result})
    out = ROOT / "reports" / f"vat fill {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)
    summ = {"applied": a.apply, "invoices_without_vat": len(todo), "results": dict(cnt.most_common()), "report": f"{out}.csv"}
    json.dump(summ, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

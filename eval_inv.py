#!/usr/bin/env python3
"""Supplier-invoice model A/B: the worker's GPU invoice read (extract_builtin + normalise, as used when the CPU
reader is not sure) with qwen3-vl:8b-instruct against qwen2.5vl:7b, scored against Sage AP (sage_ap.csv): the
transaction whose reference is the invoice number and whose amount matches is the truth for invoice number,
total incl. VAT, VAT and supplier; the Sage transaction date is usually the invoice date (scored, but read it as
approximate).

  python3 eval_inv.py [--root /srv/ocr/scans/pipeline] [--n 40] [--out eval_inv.json]

Documents: invoices the pipeline has already read (synced/ and out/ of SupplierInvoices and SupplierInvoicesArchive),
those with a Sage match, scanned ones (no text layer) first since that is where the model matters.
"""
import argparse, csv, difflib, glob, json, os, random, re, sys, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worker

MODELS = ["qwen3-vl:8b-instruct", "qwen2.5vl:7b"]


def norm(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def num(v):
    try:
        return None if v in (None, "") else round(float(str(v).replace(",", "")), 2)
    except ValueError:
        return None


def load_sage(root):
    by_ref = {}
    with open(os.path.join(root, "sage_ap.csv"), encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            k = norm(r.get("Reference"))
            if k:
                by_ref.setdefault(k, []).append(r)
    return by_ref


def truth_for(f, by_ref):
    """The Sage AP row for a read invoice: same reference (exact or with a leading-zero / prefix difference) and,
    when several, the amount closest to the read total."""
    inv = norm(f.get("invoice_number"))
    if len(inv) < 3:
        return None
    rows = by_ref.get(inv) or []
    if not rows:
        digits = re.sub(r"\D", "", inv).lstrip("0")
        if len(digits) >= 4:
            rows = [r for k, rs in by_ref.items() if re.sub(r"\D", "", k).lstrip("0") == digits for r in rs][:5]
    if not rows:
        return None
    tot = num(f.get("total"))
    rows = sorted(rows, key=lambda r: abs((num(r["AmountIncl"]) or 0) - (tot or 0)))
    r = rows[0]
    if tot is not None and abs((num(r["AmountIncl"]) or 0) - tot) > max(1.0, 0.02 * abs(tot)):
        return None                       # a reference match with a different amount: not trusted as truth
    return r


def same_ref(read, sage):
    """Sage keeps references as captured ("383945" for CIN0383945, "22876" for EV-INV22876): equal, or the same digits."""
    a, b = norm(read), norm(sage)
    if not a or not b:
        return False
    da, db = re.sub(r"\D", "", a).lstrip("0"), re.sub(r"\D", "", b).lstrip("0")
    return a == b or (len(db) >= 4 and da == db)


def score(f, t):
    s = {"invoice_number": int(same_ref(f.get("invoice_number"), t["Reference"])),
         "total": int(num(f.get("total")) is not None and abs(num(f.get("total")) - num(t["AmountIncl"])) < 0.015),
         "vat": int(num(f.get("vat")) is not None and abs(num(f.get("vat")) - (num(t["VatAmount"]) or 0)) < 0.015),
         "date": int(str(f.get("invoice_date") or "")[:10] == t["TxDate"][:10]),
         "supplier": int(difflib.SequenceMatcher(None, norm(f.get("supplier_name")), norm(t["VendorName"])).ratio() >= 0.6)}
    if t.get("VatNo"):
        s["vat_no"] = int(norm(f.get("supplier_vat_no")) == norm(t["VatNo"]))
    s["all_key"] = int(s["invoice_number"] and s["total"] and s["vat"])
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--out", default="eval_inv.json")
    ap.add_argument("--pdf-dir", help="a folder of invoice PDFs to sample instead (scanned ones only); the truth is the Sage "
                    "row that either model's reading matches on reference AND amount")
    a = ap.parse_args()
    cfg = worker.load_config()
    by_ref = load_sage(a.root)
    if a.pdf_dir:
        return run_dir(a, cfg, by_ref)
    cands = []
    for pat in ("synced/SupplierInvoices*/*/*.json", "out/SupplierInvoices*/*.json", "out/SupplierInvoices*/hold/*.json"):
        for js in glob.glob(os.path.join(a.root, pat)):
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if sc.get("doc_type") != "supplier_invoice":
                continue
            pdf = os.path.join(os.path.dirname(js), str(sc.get("file") or ""))
            if not pdf.lower().endswith(".pdf") or not os.path.isfile(pdf):
                continue
            t = truth_for(sc.get("fields") or {}, by_ref)
            if not t:
                continue
            scanned = len(worker.pdf_text(Path(pdf), 2) or "") < 200
            cands.append({"pdf": pdf, "folder": sc.get("folder"), "truth": t, "scanned": scanned, "read_by": sc.get("model")})
    random.Random(23).shuffle(cands)
    seen, picked = set(), []
    for c in sorted(cands, key=lambda c: not c["scanned"]):          # scanned first
        k = norm(c["truth"]["Reference"]) + norm(c["truth"]["VendorAccount"])
        if k in seen:
            continue
        seen.add(k)
        picked.append(c)
        if len(picked) >= a.n:
            break
    print(f"{len(cands)} invoices with a Sage match; testing {len(picked)} ({sum(c['scanned'] for c in picked)} scanned)", flush=True)
    res = {m: [] for m in MODELS}
    for m in MODELS:
        for c in picked:
            tcfg = dict(cfg["doc_types"].get(c["folder"] or "SupplierInvoices") or cfg["doc_types"]["SupplierInvoices"])
            tcfg["model"] = m
            t0 = time.time()
            try:
                raw, _ = worker.extract_builtin("supplier_invoice", Path(c["pdf"]), cfg, tcfg)
                f, _ = worker.normalise("supplier_invoice", raw, cfg, tcfg.get("cpu", {}).get("own_vat_numbers", ()))
                err = None
            except Exception as e:
                f, err = {}, str(e)[:200]
            secs = time.time() - t0
            s = score(f, c["truth"])
            res[m].append({"pdf": os.path.basename(c["pdf"]), "scanned": c["scanned"], "secs": round(secs, 1), "score": s, "error": err,
                           "read": {k: f.get(k) for k in ("supplier_name", "supplier_vat_no", "invoice_number", "invoice_date", "total", "vat")},
                           "truth": {k: c["truth"][k] for k in ("VendorName", "VatNo", "Reference", "TxDate", "AmountIncl", "VatAmount")}})
            print(f"{m:22} {os.path.basename(c['pdf'])[:45]:45} {secs:5.1f}s  key={s['all_key']}" + (f"  ERROR {err}" if err else ""), flush=True)
    summary = {}
    for m, rs in res.items():
        for subset, rows in (("all", rs), ("scanned", [r for r in rs if r["scanned"]]), ("text layer", [r for r in rs if not r["scanned"]])):
            if not rows:
                continue
            keys = sorted({k for r in rows for k in r["score"]})
            summary[f"{m} / {subset}"] = dict({k: round(sum(r["score"].get(k, 0) for r in rows) / max(1, sum(1 for r in rows if k in r["score"])), 3) for k in keys},
                                              docs=len(rows), secs_per_doc=round(sum(r["secs"] for r in rows) / len(rows), 1),
                                              errors=sum(1 for r in rows if r["error"]))
    json.dump({"summary": summary, "results": res}, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    cols = ["docs", "all_key", "invoice_number", "total", "vat", "date", "supplier", "vat_no", "secs_per_doc", "errors"]
    print("\n" + f"{'model / subset':38}" + "".join(f"{c[:12]:>13}" for c in cols))
    for k, s in summary.items():
        print(f"{k:38}" + "".join(f"{s.get(c, ''):>13}" for c in cols))


def run_dir(a, cfg, by_ref):
    pdfs = sorted(glob.glob(os.path.join(a.pdf_dir, "*.pdf")))
    random.Random(23).shuffle(pdfs)
    picked = []
    for p in pdfs:
        if len(worker.pdf_text(Path(p), 1) or "") < 200:
            picked.append(p)
        if len(picked) >= a.n:
            break
    print(f"{len(pdfs)} PDFs in {a.pdf_dir}; reading {len(picked)} scanned ones with both models", flush=True)
    tcfg0 = cfg["doc_types"]["SupplierInvoices"]
    reads = {m: {} for m in MODELS}
    for m in MODELS:
        tcfg = dict(tcfg0); tcfg["model"] = m
        for p in picked:
            t0 = time.time()
            try:
                raw, _ = worker.extract_builtin("supplier_invoice", Path(p), cfg, tcfg)
                f, _ = worker.normalise("supplier_invoice", raw, cfg, tcfg.get("cpu", {}).get("own_vat_numbers", ()))
                err = None
            except Exception as e:
                f, err = {}, str(e)[:200]
            reads[m][p] = (f, round(time.time() - t0, 1), err)
            print(f"{m:22} {os.path.basename(p)[:50]:50} {reads[m][p][1]:5.1f}s" + (f"  ERROR {err}" if err else ""), flush=True)
    res = {m: [] for m in MODELS}
    truthless = 0
    for p in picked:
        t = None
        for m in MODELS:
            t = t or truth_for(reads[m][p][0], by_ref)
        if not t:
            truthless += 1
            continue
        for m in MODELS:
            f, secs, err = reads[m][p]
            res[m].append({"pdf": os.path.basename(p), "scanned": True, "secs": secs, "score": score(f, t), "error": err,
                           "read": {k: f.get(k) for k in ("supplier_name", "supplier_vat_no", "invoice_number", "invoice_date", "total", "vat")},
                           "truth": {k: t[k] for k in ("VendorName", "VatNo", "Reference", "TxDate", "AmountIncl", "VatAmount")}})
    print(f"\n{len(picked) - truthless} of {len(picked)} invoices matched a Sage transaction (scored); {truthless} without a match left out", flush=True)
    for m in MODELS:
        print(f"  {m}: {sum(1 for x in reads[m].values() if x[2])} read errors, {sum(x[1] for x in reads[m].values()) / max(1, len(picked)):.1f} s per invoice")
    summary = {}
    for m, rows in res.items():
        if rows:
            keys = sorted({k for r in rows for k in r["score"]})
            summary[m] = dict({k: round(sum(r["score"].get(k, 0) for r in rows) / max(1, sum(1 for r in rows if k in r["score"])), 3) for k in keys},
                              docs=len(rows), secs_per_doc=round(sum(r["secs"] for r in rows) / len(rows), 1), errors=sum(1 for r in rows if r["error"]))
    json.dump({"summary": summary, "results": res, "sampled": len(picked), "scored": len(picked) - truthless}, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    cols = ["docs", "all_key", "invoice_number", "total", "vat", "date", "supplier", "vat_no", "secs_per_doc", "errors"]
    print("\n" + f"{'model':24}" + "".join(f"{c[:12]:>13}" for c in cols))
    for k, s in summary.items():
        print(f"{k:24}" + "".join(f"{s.get(c, ''):>13}" for c in cols))


if __name__ == "__main__":
    main()

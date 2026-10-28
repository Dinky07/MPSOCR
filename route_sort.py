#!/usr/bin/env python3
"""Routing rules for documents the readers can't place (Dino 28 Oct 2026, testset.xlsx notes + 14:40 decisions).

Every unsynced document in the order / payment / invoice queues and holds is read (text layer or Tesseract, cached in
work/od_text) and checked against route_rules.json. Actions:
  remove           out of the pipeline into deleted/<Type>/<yyyy-mm>/ (recoverable), never synced:
                   MPS's own sales invoices, Sage sales orders (file copies), bank confirmation letters, supplier
                   invoices made out to Polymer, test certs marked SAMPLE
  quotes           MPS / Kusasa sales quotations -> out/SalesQuotes (synced to the Quotes library on MPS Sales)
  other_documents  Saheti / Alenti / SARS -> out/OtherDocuments (private library), with a summary. Only documents read
                   after "od_new_since": the backlog goes through od_sort.py's review sheet.
  tag              orders: fields.cod (C.O.D stamp) / fields.cancelled (CANCELLED stamp); also on synced orders, for the
                   library columns (the PC sync's -BackfillFields writes them)
Order forms the readers recognised (MPS A-number, Polymer PL-number, IGT) are never removed or moved; they only get tags.

    python3 route_sort.py [--apply] [--only remove,quotes,other_documents,tag] [--no-synced]
Dry run without --apply. Report: reports/route sort <stamp>[ dry run].csv / .json.
"""
import argparse, base64, csv, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                    # noqa: E402
import od_sort                                         # noqa: E402  ocr_text (cache), summarise, file_hash

HERE = Path(__file__).resolve().parent
ROOT = Path("/srv/ocr/scans/pipeline")
TYPES = ["PurchaseOrders", "PolymerOrders", "ProofOfPayments", "SupplierInvoices", "SupplierInvoicesArchive", "DeliveryNotes"]
ORDER_FORMS = {"mps_order", "polymer_order", "internal_group_transfer"}
HASH_INDEX = ROOT / "work" / "od_text" / "_index.json"

DEFAULT = {
    "od_new_since": "2026-10-28T14:15:00",
    "rules": [
        {"name": "MPS sales invoice", "action": "remove", "queues": ["SupplierInvoices", "SupplierInvoicesArchive", "PurchaseOrders", "PolymerOrders", "ProofOfPayments"],
         "head": r"MINING\s+PRESSURE\s+SYSTEMS|KUSASA\s+PRESSURE", "all": [r"TAX\s+INVOICE", r"\bIN\s?\d{5,7}\b"],
         "confirm": "Is this a tax invoice ISSUED BY Mining Pressure Systems or Kusasa Pressure Systems as the SELLER (their own name and logo at the top as the company invoicing)? Answer only yes or no.",
         "why": "MPS's own sales invoice: can be reprinted from Sage"},
        {"name": "Sage sales order", "action": "remove", "all": [r"SALES\s+ORDER\s+FILE\s+COPY"],
         "why": "Sales order file copy (Polymer / MPS): the order is in Sage"},
        {"name": "Bank confirmation letter", "action": "remove",
         "any": [r"CONFIRMATION\s+OF\s+BANK", r"BANK\s+CONFIRMATION\s+LETTER", r"THIS\s+LETTER\s+(SERVES\s+TO\s+)?CONFIRMS?\b.{0,80}\bACCOUNT",
                 r"ACCOUNT\s+CONFIRMATION\s+LETTER"], "none": [r"TAX\s+INVOICE"],
         "confirm": "Is this a letter from a bank confirming someone's bank account details? Answer only yes or no.",
         "why": "bank confirmation letter: not needed"},
        {"name": "Supplier invoice to Polymer", "action": "remove",
         "queues": ["SupplierInvoices", "SupplierInvoicesArchive", "PurchaseOrders", "PolymerOrders", "ProofOfPayments"],
         "all": [r"\bINVOICE\b", r"POLYMER\s+(LINERS|LINING)"], "none": [r"MINING\s+PRESSURE\s+SYSTEMS", r"PRO\s*-?\s*FORMA", r"QUOTATION"],
         "confirm": "Is this an invoice addressed TO Polymer Liners / Polymer Lining Systems as the customer being billed? Answer only yes or no.",
         "why": "supplier invoice made out to Polymer Lining Systems: not MPS's"},
        {"name": "Test cert marked SAMPLE", "action": "remove", "queues": ["PurchaseOrders", "PolymerOrders", "ProofOfPayments", "SupplierInvoices"],
         "all": [r"\bSAMPLE\b", r"TEST\s+CERTIFICATE|MILL\s+TEST|INSPECTION\s+CERTIFICATE|EN\s*10204|CHEMICAL\s+COMPOSITION"],
         "confirm": "Is the word SAMPLE stamped, typed large or written ACROSS this page as a mark on the document (not just a column heading such as 'Sample No' or 'Sample position' in a table)? Answer only yes or no.",
         "why": "test certificate marked SAMPLE: not imported"},
        {"name": "Sales quotation", "action": "quotes", "queues": ["PurchaseOrders", "PolymerOrders", "SupplierInvoices", "ProofOfPayments"],
         "file": r"QUOTE\s+APPROVAL|MPSHQ|_QP_",
         "head_or": r"KUSASA\s+PRESSURE|MINING\s+PRESSURE\s+SYSTEMS", "all_or": [r"QUOTATION|QUOTE\s*#|QUOTE\s+NO"],
         "confirm": "Is this a quotation ISSUED BY Mining Pressure Systems or Kusasa Pressure Systems (their letterhead at the top, quoting prices to a customer)? Answer only yes or no.",
         "why": "sales quotation by MPS / Kusasa: Quotes library"},
        {"name": "Saheti", "action": "other_documents", "category": "Saheti", "any": [r"\bSAHETI\b"]},
        {"name": "Alenti", "action": "other_documents", "category": "Alenti", "any": [r"\bALENTI\b"]},
        {"name": "SARS", "action": "other_documents", "category": "SARS", "any": [r"SOUTH\s+AFRICAN\s+REVENUE\s+SERVICE", r"\bSARS\b", r"\beFILING\b"]},
        {"name": "COD", "action": "tag", "field": "cod", "orders_only": True, "any": [r"\bC\s?[.,]\s?[O0]\s?[.,]\s?D\b", r"CASH\s+ON\s+DELIVERY"],
         "vision": "Is there a C.O.D stamp (cash on delivery), or the words C.O.D / CASH ON DELIVERY stamped or written, on this order form? Answer only yes or no."},
        {"name": "Cancelled", "action": "tag", "field": "cancelled", "orders_only": True, "any": [r"\bCANCELL?ED\b"],
         "confirm": "Is this order marked, stamped or written across as CANCELLED? Answer only yes or no."},
    ]}


def config():
    p = HERE / "route_rules.json"
    if not p.exists():
        p.write_text(json.dumps(DEFAULT, indent=1), encoding="utf-8")
    c = json.load(open(p, encoding="utf-8"))
    for r in c["rules"]:
        for k in ("all", "any", "none", "all_or"):
            r["_" + k] = [re.compile(x, re.I | re.S) for x in r.get(k, [])]
        for k in ("head", "head_or", "file"):
            r["_" + k] = re.compile(r[k], re.I) if r.get(k) else None
    return c


# ---- the text cache is keyed by the file's SHA-256; remember path/size/mtime -> hash so a run doesn't re-hash 6,000 PDFs
_idx = None


def file_key(pdf):
    global _idx
    if _idx is None:
        try:
            _idx = json.load(open(HASH_INDEX))
        except Exception:
            _idx = {}
    st = pdf.stat()
    k = f"{pdf}|{st.st_size}|{int(st.st_mtime)}"
    if k not in _idx:
        _idx[k] = od_sort.file_hash(pdf)
    return _idx[k]


def save_index():
    if _idx is not None:
        tmp = HASH_INDEX.with_suffix(".tmp")
        tmp.write_text(json.dumps(_idx), encoding="utf-8")
        os.replace(tmp, HASH_INDEX)


def text_of(pdf):
    od_sort.CACHE.mkdir(parents=True, exist_ok=True)
    c = od_sort.CACHE / f"{file_key(pdf)}.txt"
    if c.exists():
        return c.read_text(encoding="utf-8", errors="replace")
    return od_sort.ocr_text(pdf)            # writes the same cache file


def docs(include_synced):
    for t in TYPES:
        for where in ("", "hold/"):
            for js in sorted(glob.glob(str(ROOT / "out" / t / where / "*.json"))):
                yield t, Path(js), "hold" if where else "queue"
    if include_synced:
        for t in ("PurchaseOrders", "PolymerOrders"):
            for js in sorted(glob.glob(str(ROOT / "synced" / t / "*" / "*.json"))):
                yield t, Path(js), "synced"


def matches(r, text, head, fname, own):
    """-> (evidence, found_in_file_name) or None"""
    if r.get("queues") and r["_t"] not in r["queues"]:
        return None
    full = text + "\n" + (fname if r["action"] == "tag" else own)
    if r["_file"] is not None and r["_file"].search(fname):
        return f"file name: {r['_file'].search(fname).group(0)}", True
    if r.get("file") and not (r.get("all_or") or r.get("head_or")):
        return None
    if r.get("head_or") or r.get("all_or"):
        if r["_head_or"] is not None and not r["_head_or"].search(head):
            return None
        if not all(rx.search(full) for rx in r["_all_or"]):
            return None
    if r["_head"] is not None and not r["_head"].search(head):
        return None
    if not all(rx.search(full) for rx in r["_all"]):
        return None
    if r["_any"] and not any(rx.search(full) for rx in r["_any"]):
        return None
    if any(rx.search(full) for rx in r["_none"]):
        return None
    hit = next((rx.search(full) for rx in (r["_any"] + r["_all"] + r["_all_or"]) if rx.search(full)), None)
    return (od_sort.evidence(full, re.compile(re.escape(hit.group(0)))) if hit else "matched"), False


def ask_yes_no(pdf, question, cfg):
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-png", "-scale-to", "1400", "-f", "1", "-l", "1", "-singlefile", str(pdf), os.path.join(td, "p")],
                       capture_output=True, timeout=300)
        body = {"model": cfg["model"], "stream": False, "keep_alive": "15m",
                "options": {"temperature": 0, "num_ctx": cfg["num_ctx"], "num_predict": 5},
                "messages": [{"role": "user", "content": question,
                              "images": [base64.b64encode(open(os.path.join(td, "p.png"), "rb").read()).decode()]}]}
        req = urllib.request.Request(cfg["ollama_url"].rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())["message"]["content"].strip().lower().startswith("yes")


QUOTE_PROMPT = """This is a sales quotation issued by Mining Pressure Systems or its HDPE division Kusasa Pressure Systems.
Return JSON: {"quote_number": "as printed after QUOTE # / QUOTE NO (e.g. MPSHQ 03968) or null", "revision": "rev / version or null",
 "customer": "who the quote is addressed to", "date": "YYYY-MM-DD or null", "total": number or null (incl VAT if shown),
 "accepted": true if the customer signed / dated / wrote acceptance on it, else false,
 "summary": "one sentence: what is quoted (sizes, product) and for whom"}
Never invent values."""


def read_quote(pdf, text, cfg, fname):
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-png", "-scale-to", "1600", "-f", "1", "-l", "1", "-singlefile", str(pdf), os.path.join(td, "p")],
                       capture_output=True, timeout=300)
        body = {"model": cfg["model"], "stream": False, "format": "json", "keep_alive": "15m",
                "options": {"temperature": 0, "num_ctx": cfg["num_ctx"], "num_predict": 500},
                "messages": [{"role": "system", "content": QUOTE_PROMPT},
                             {"role": "user", "content": "Read this quotation.\n\nOCR text (hint):\n" + (text or "")[:3000],
                              "images": [base64.b64encode(open(os.path.join(td, "p.png"), "rb").read()).decode()]}]}
        req = urllib.request.Request(cfg["ollama_url"].rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = json.loads(r.read())["message"]["content"]
    m = re.search(r"\{.*\}", raw, re.S)
    q = json.loads(m.group(0) if m else raw)
    ref = str(q.get("quote_number") or "").strip() or None
    fm = re.search(r"(?:QUOTE\s+APPROVAL|MPSHQ)\s*-?\s*(\d{4,6})", fname, re.I)
    if fm and (not ref or fm.group(1) not in ref):
        ref = ref or f"MPSHQ {fm.group(1)}"
    date = str(q.get("date") or "")
    total = q.get("total")
    try:
        total = float(str(total).replace(",", "").replace("R", "").strip()) if total not in (None, "") else None
    except ValueError:
        total = None
    return {"quote_ref": ref, "quote_revision": (str(q.get("revision")).strip() if q.get("revision") else None),
            "customer": q.get("customer"), "date": date if re.fullmatch(r"\d{4}-\d\d-\d\d", date) else None, "total": total,
            "kusasa": bool(re.search(r"KUSASA", text or "", re.I) or re.search(r"KUSASA", fname, re.I)),
            "accepted": bool(q.get("accepted")) or bool(re.search(r"APPROVAL|ACCEPTED", fname, re.I)),
            "summary": q.get("summary")}


def move_out(js, sc, dest_type, fields, stamp, how):
    """PDF first, new sidecar last, old sidecar removed; the old reading is kept under "previous"."""
    dest = ROOT / "out" / dest_type
    dest.mkdir(parents=True, exist_ok=True)
    pdf = js.parent / str(sc.get("file") or "")
    name, n = pdf.name, 2
    while (dest / name).exists() or (dest / (Path(name).stem + ".json")).exists():
        name = f"{pdf.stem}_{n}{pdf.suffix}"
        n += 1
    new = {"schema_version": 1, "doc_id": sc.get("doc_id"), "doc_type": {"OtherDocuments": "other_doc", "SalesQuotes": "sales_quote"}[dest_type],
           "folder": dest_type, "file": name, "source_file": sc.get("source_file"),
           **({"source_folder": sc["source_folder"]} if sc.get("source_folder") else {}),
           "source_sha256": sc.get("source_sha256"), "source_page": sc.get("source_page"), "processed_at": sc.get("processed_at"),
           "model": "qwen2.5vl:7b (summary)", "extractor": "route_sort", "needs_review": False, "flags": [], "fields": fields,
           "previous": {"folder": sc.get("folder"), "sidecar": str(js.relative_to(ROOT)), "doc_type": sc.get("doc_type"), "fields": sc.get("fields")},
           "edits": (sc.get("edits") or []) + [{"at": stamp, "by": f"route_sort ({how})", "changes": [{"field": "folder", "old": sc.get("folder"), "new": dest_type}]}]}
    shutil.move(str(pdf), str(dest / name))
    tmp = dest / (Path(name).stem + ".json.tmp")
    tmp.write_text(json.dumps(new, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, dest / (Path(name).stem + ".json"))
    if Path(str(js) + ".attempts").exists():
        os.remove(str(js) + ".attempts")
    os.remove(js)
    return name


def remove(js, sc, t, why, stamp):
    dest = ROOT / "deleted" / t / datetime.now().strftime("%Y-%m")
    dest.mkdir(parents=True, exist_ok=True)

    def free(n):
        p, k = dest / n, 2
        while p.exists():
            p = dest / f"{Path(n).stem}_{k}{Path(n).suffix}"
            k += 1
        return p
    sc["deleted_at"], sc["deleted_why"] = stamp, why
    tmp = js.with_name(js.name + ".rs.tmp")
    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, js)
    for q in [js.parent / str(sc.get("file") or ""), Path(str(js) + ".attempts"), js]:
        if q.is_file():
            shutil.move(str(q), str(free(q.name)))
    (dest / (js.stem + ".deleted.json")).write_text(json.dumps({"name": js.name, "at": stamp, "why": why,
                                                               "source_file": sc.get("source_file"), "by": "route_sort"}), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--no-synced", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--vision", action="store_true", help="dry run: also ask the model the C.O.D question (slow)")
    a = ap.parse_args()
    c = config()
    only = set(x for x in a.only.split(",") if x)
    pcfg = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))
    vcfg = {"model": "qwen2.5vl:7b", "ollama_url": pcfg.get("ollama_url", "http://127.0.0.1:11434"), "num_ctx": int(pcfg.get("num_ctx", 49152))}
    stamp = datetime.now().isoformat(timespec="seconds")
    items = []
    for t, js, where in docs(not a.no_synced):
        if js.name.endswith((".tmp", ".lock")):
            continue
        try:
            sc = json.load(open(js, encoding="utf-8"))
        except Exception:
            continue
        pdf = js.parent / str(sc.get("file") or "")
        if not pdf.is_file():
            continue
        f = sc.get("fields") or {}
        is_order = f.get("form") in ORDER_FORMS and bool(f.get("order_number"))
        uploaded = Path(str(js) + ".uploaded").exists() or where == "synced"
        if where == "synced" and not is_order:
            continue
        items.append((t, js, where, sc, pdf, is_order, uploaded))

    def read(it):
        try:
            return it, text_of(it[4])
        except Exception as e:
            return it, ""
    rows, res = [], Counter()
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        texts = list(ex.map(read, items))
    save_index()
    for (t, js, where, sc, pdf, is_order, uploaded), text in texts:
        f = sc.get("fields") or {}
        head = "\n".join((text or "").splitlines()[:12])[:700]
        fname = f"{sc.get('source_file') or ''} {pdf.name}"
        own = " ".join(str(v) for v in f.values() if isinstance(v, str))     # the reader's values, not the key names
        for r in c["rules"]:
            if only and r["action"] not in only:
                continue
            r["_t"] = t
            if r["action"] == "tag":
                if not is_order or f.get(r["field"]) is True:
                    continue
                if r.get("vision") and f.get(r["field"] + "_checked"):
                    continue                                    # the model already said no for this order
            else:
                if is_order or uploaded or f.get("checked"):
                    continue
                if r["action"] == "other_documents" and str(sc.get("processed_at") or "") < c.get("od_new_since", ""):
                    continue                                    # the backlog goes through od_sort's review sheet
            m = matches(r, text or "", head, fname, own)
            if not m and r.get("vision") and (a.apply or a.vision):
                # the text layer misses stamps over printed lines (27 Oct testset: 3 of 6 C.O.D stamps): ask the model once
                try:
                    yes = ask_yes_no(pdf, r["vision"], vcfg)
                except Exception:
                    res[f"{r['name']}: model not reached"] += 1
                    continue
                if not yes:
                    res[f"{r['name']}: no (model)"] += 1
                    if a.apply:
                        try:
                            with sidecar_lock.locked(js, "route_sort"):
                                sc2 = json.load(open(js, encoding="utf-8"))
                                sc2.setdefault("fields", {})[r["field"] + "_checked"] = True
                                sc2["fields"].setdefault(r["field"], False)
                                tmp = js.with_name(js.name + ".rs.tmp")
                                tmp.write_text(json.dumps(sc2, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                                os.replace(tmp, js)
                        except sidecar_lock.Locked:
                            pass
                    continue
                m = ("model: yes", True)
            if not m:
                continue
            ev, via_file = m
            if r.get("confirm") and not via_file:
                try:
                    if not ask_yes_no(pdf, r["confirm"], vcfg):
                        res[f"{r['name']}: not confirmed by the model"] += 1
                        continue
                except Exception as e:
                    res[f"{r['name']}: model not reached"] += 1
                    continue
            result = "would " + r["action"] if not a.apply else r["action"]
            if a.apply:
                try:
                    with sidecar_lock.locked(js, "route_sort"):
                        sc = json.load(open(js, encoding="utf-8"))
                        f = sc.get("fields") or {}
                        if r["action"] == "tag":
                            old = f.get(r["field"])
                            f[r["field"]] = True
                            if r.get("vision"):
                                f[r["field"] + "_checked"] = True
                            sc["fields"] = f
                            sc.setdefault("edits", []).append({"at": stamp, "by": f"route_sort (tag {r['name']})",
                                                               "changes": [{"field": r["field"], "old": old, "new": True}]})
                            tmp = js.with_name(js.name + ".rs.tmp")
                            tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                            os.replace(tmp, js)
                        elif r["action"] == "remove":
                            remove(js, sc, t, f"{r['name']}: {r.get('why', '')}", stamp)
                        elif r["action"] == "quotes":
                            q = read_quote(pdf, text, vcfg, fname)
                            q.update(content_hash=f.get("content_hash") or od_sort.file_hash(pdf).upper(), review=None,
                                     library_doc_type="Sales Quotation", moved_from=f"{t} ({where})", route_rule=r["name"])
                            result = "quotes -> " + move_out(js, sc, "SalesQuotes", q, stamp, r["name"])
                        elif r["action"] == "other_documents":
                            s = od_sort.summarise(pdf, text, vcfg)
                            fields = {"summary": s.get("summary"), "od_doc_type": s.get("doc_type"), "od_from": s.get("from"),
                                      "date": s.get("date"), "order_number": s.get("order_number"), "job_number": s.get("job_number"),
                                      "od_category": r.get("category", "Other"), "od_rule": r["name"], "od_evidence": ev[:250],
                                      "moved_from": f"{t} ({where})", "content_hash": f.get("content_hash") or od_sort.file_hash(pdf).upper(),
                                      "review": None, "library_doc_type": "Other Document"}
                            result = "other documents -> " + move_out(js, sc, "OtherDocuments", fields, stamp, r["name"])
                except sidecar_lock.Locked:
                    result = "locked (next run)"
                except Exception as e:
                    result = f"error: {e}"[:120]
            res[f"{r['name']}: {result.split(' -> ')[0]}"] += 1
            rows.append({"rule": r["name"], "action": r["action"], "result": result, "type": t, "where": where,
                         "file": pdf.name, "source_file": sc.get("source_file"), "evidence": (ev or "")[:200]})
            if r["action"] != "tag":
                break                                           # a document gets one routing action
    out = ROOT / "reports" / f"route sort {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["rule", "action", "result", "type", "where", "file", "source_file", "evidence"])
        w.writeheader()
        w.writerows(rows)
    summary = {"applied": a.apply, "documents_read": len(items), "results": dict(res.most_common()), "report": f"{out}.csv"}
    json.dump(summary, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()

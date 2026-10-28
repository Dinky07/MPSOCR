#!/usr/bin/env python3
"""Payment confirmations already waiting in the Purchase Orders queue (read as "not an order", Document Type "Other"):
re-tag them Document Type "Proof of Payment", take the payee from the scan's supplier folder or the bank page, drop the
"not an order" flag and rename the pair SUPPLIER-ORDER-dd-mm-yyyy-POP. Held ones whose order number is an A-number go
back to the out queue. Only documents not yet uploaded are touched.

  python3 fix_pop.py [--root /srv/ocr/scans/pipeline] [--apply]
"""
import argparse, datetime, glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pop_reader, po_reader, worker

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--type", default="PurchaseOrders")
a = ap.parse_args()

found = moved = 0
for p in sorted(glob.glob(f"{a.root}/out/{a.type}/*.json") + glob.glob(f"{a.root}/out/{a.type}/hold/*.json")):
    if os.path.exists(p + ".uploaded"):
        continue
    try:
        with open(p, encoding="utf-8-sig") as fh:
            side = json.load(fh)
    except (OSError, ValueError):
        continue
    f, flags = side.get("fields") or {}, list(side.get("flags") or [])
    if side.get("doc_type") != "po" or "not_an_order" not in flags or f.get("library_doc_type") == "Proof of Payment":
        continue
    pdf = os.path.join(os.path.dirname(p), str(side.get("file") or ""))
    if not os.path.isfile(pdf):
        continue
    first = pop_reader.ocr_pages(pdf, 1, upright=True)         # page 1 decides; the rest only for a payment file
    if not first or not pop_reader.is_payment_confirmation(first[0]):
        continue
    texts = pop_reader.ocr_pages(pdf, 4, upright=True)
    pages = [i + 1 for i, t in enumerate(texts) if pop_reader.is_payment_page(t)]
    if 1 not in pages:
        continue
    found += 1
    old = {k: f.get(k) for k in ("supplier", "library_doc_type")}
    f, flags = worker.Worker.keep_pop_fields(f, flags, pages, texts, True, side)
    side["flags"] = flags
    side["needs_review"] = bool(side["flags"])
    held = "/hold/" in p
    back = held and re.fullmatch(r"A\d{5}", str(f.get("order_number") or "").strip())
    stem = po_reader.library_name(f, side.get("source_file") or side.get("file") or "document", "")[:150]
    print(f"{os.path.relpath(p, a.root)}{' [held]' if held else ''}: supplier {old['supplier'] or '-'} -> {f.get('supplier') or '-'}, "
          f"type {old['library_doc_type']} -> {f['library_doc_type']}, name -> {stem}{' (back to the queue)' if back else ''}")
    if not a.apply:
        continue
    side.pop("hold_reason", None) if back else None
    side.setdefault("edits", []).append({"at": datetime.datetime.now().isoformat(timespec="seconds"), "by": "fix_pop.py",
                                         "changes": [{"field": "library_doc_type", "old": old["library_doc_type"], "new": f["library_doc_type"]},
                                                     {"field": "supplier", "old": old["supplier"], "new": f.get("supplier")}]})
    side["flags"] = [x for x in side["flags"] if not (back and str(x).startswith("held:"))]
    side["needs_review"] = bool(side["flags"])
    side["fields"] = f
    dest = os.path.join(a.root, "out", a.type) if back else os.path.dirname(p)
    ext = os.path.splitext(pdf)[1].lower()
    cand, k = os.path.join(dest, stem + ext), 2
    while os.path.exists(cand) or os.path.exists(os.path.splitext(cand)[0] + ".json"):
        cand, k = os.path.join(dest, f"{stem}_{k}{ext}"), k + 1
    os.replace(pdf, cand)
    side["file"] = os.path.basename(cand)
    new_js = os.path.splitext(cand)[0] + ".json"
    with open(new_js + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(side, fh, indent=2, ensure_ascii=False)
    os.replace(new_js + ".tmp", new_js)
    os.remove(p)
    if os.path.exists(p + ".attempts"):
        os.replace(p + ".attempts", new_js + ".attempts")
    moved += bool(back)
print(f"{found} payment confirmation(s) {'re-tagged' if a.apply else 'to re-tag (dry run: add --apply)'}"
      + (f", {moved} back from hold" if a.apply else ""))

#!/usr/bin/env python3
"""One file = one document, for what is already read (Dino 29 Oct 07:41): the page documents of each scan in the order
queues (Purchase / Polymer / Ronin, queue and hold, not uploaded) are regrouped the way the worker now does it:

  - each order form (with its number) is a document; the scan's other pages are its supporting pages, behind it in one
    PDF (order page first, then the rest in scan order); several orders in one scan: each order takes the pages after it,
    pages before the first order go with the first order
  - no order form in the scan: one document, the first page's reading, the rest supporting
The order's sidecar is kept as it is - its reading, checks, Checked tick and edits - with fields.supporting /
supporting_text / pages / source_pages / content_hash added, and its PDF replaced by the grouped one (same file name, same
queue or hold). Documents put back together this morning (coalesce_hold) count with all their pages.
The supporting page documents go to deleted/<Type>/<yyyy-mm>/ (recoverable).
Left alone: scans with anything uploaded, a supporting page that is checked or edited on the dashboard, or a lock held.

    python3 regroup_scans.py [--apply] [--limit N]
Report: reports/regroup scans <stamp>[ dry run].csv / .json
"""
import argparse, csv, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402  remove()
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
TYPES = ("PurchaseOrders", "PolymerOrders", "RoninOrders")
FORMS = {"mps_order", "polymer_order", "internal_group_transfer", "ronin_order"}
SCRIPT_EDIT = re.compile(r"^(po_autocheck|route_sort|hold_resort|od_sort|requeue_po12|numbers_pass|fix_|supplier rule|ronin_|coalesce_hold|regroup_|\d+-)", re.I)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def kind_of(f, sc):
    if f.get("form") in FORMS and f.get("order_number"):
        return f.get("form")
    if sc.get("doc_type") == "pop":
        return "proof_of_payment"
    return f.get("library_doc_type") or f.get("po_doc_type") or "other"


def summary(u):
    """Supporting-page entries for a unit: a merged document lists its pages; a page document is one entry."""
    if u["sc"].get("merged_from"):
        return [{"page": m.get("page"), "kind": m.get("doc_type") or (m.get("form") if m.get("form") not in (None, "", "not_an_order") else "other"),
                 **({"order_number": m["order_number"]} if m.get("order_number") else {}),
                 **({"supplier": m["supplier"]} if m.get("supplier") else {})} for m in u["sc"]["merged_from"]]
    f = u["sc"].get("fields") or {}
    keep = {k: f.get(k) for k in ("order_number", "supplier", "supplier_name", "account_name", "invoice_number", "date",
                                  "total", "amount", "order_value") if f.get(k) not in (None, "", [], {})}
    return [{"page": u["pages"][0] if u["pages"] else None, "kind": kind_of(f, u["sc"]), **keep}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    scans = defaultdict(list)
    uploaded = set()
    for pat, place in (("synced/{t}/*/*.json", "synced"), ("out/{t}/*.json", "queue"), ("out/{t}/hold/*.json", "hold")):
        for t in TYPES:
            for p in glob.glob(str(ROOT / pat.format(t=t))):
                if p.endswith((".tmp", ".lock")):
                    continue
                try:
                    sc = json.load(open(p, encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                h = str(sc.get("source_sha256") or "").lower()
                if not h:
                    continue
                if place == "synced" or Path(p + ".uploaded").exists():
                    uploaded.add(h)
                    continue
                pages = [m.get("page") for m in sc.get("merged_from") or []] or ([int(sc["source_page"])] if sc.get("source_page") else [])
                if not pages:
                    continue                                     # a scan that was never split: nothing to regroup
                f = sc.get("fields") or {}
                scans[h].append({"json": Path(p), "sc": sc, "type": t, "place": place, "pages": sorted(x for x in pages if x),
                                 "order": f.get("order_number") if f.get("form") in FORMS and f.get("order_number") else None})
    rows, cnt, n_done = [], Counter(), 0
    for h, units in sorted(scans.items()):
        if len(units) < 2:
            continue
        if a.limit and n_done >= a.limit:
            break
        units.sort(key=lambda u: (u["pages"] or [0])[0])
        src = units[0]["sc"].get("source_file")
        if h in uploaded:
            cnt["left: part of the scan is uploaded"] += 1
            rows.append({"source_file": src, "result": "left: part of the scan is uploaded"})
            continue
        mains = []
        for i, u in enumerate(units):
            if u["order"] and not (mains and units[mains[-1]]["order"] == u["order"]):
                mains.append(i)
        if not mains:
            mains = [0]
        segs = [[] for _ in mains]
        for i in range(len(units)):
            segs[max([j for j, m in enumerate(mains) if m <= i], default=0)].append(i)
        for seg, mi in zip(segs, mains):
            if len(seg) < 2:
                continue
            main, others = units[mi], [units[i] for i in seg if i != mi]
            why = None
            for u in others:
                f = u["sc"].get("fields") or {}
                if f.get("checked"):
                    why = "a supporting page is checked"
                elif [e for e in (u["sc"].get("edits") or []) if not SCRIPT_EDIT.match(str(e.get("by") or ""))]:
                    why = "a supporting page was edited on the dashboard"
            for u in [main] + others:
                if not (u["json"].parent / str(u["sc"].get("file") or "")).is_file():
                    why = "a PDF is missing"
            res = ("left: " + why) if why else ("would group" if not a.apply else "grouped")
            if a.apply and not why:
                locks = []
                try:
                    for u in [main] + others:
                        cm = sidecar_lock.locked(u["json"], "regroup_scans")
                        cm.__enter__()
                        locks.append(cm)
                    sc = json.load(open(main["json"], encoding="utf-8"))
                    pdf = main["json"].parent / sc["file"]
                    with tempfile.TemporaryDirectory() as td:
                        out = Path(td) / "g.pdf"
                        r = subprocess.run(["pdfunite", str(pdf)] + [str(u["json"].parent / u["sc"]["file"]) for u in others] + [str(out)],
                                           capture_output=True, text=True, timeout=300)
                        if r.returncode != 0 or not out.exists():
                            raise RuntimeError(f"pdfunite: {r.stderr[:200]}")
                        shutil.copy2(out, pdf.with_name(pdf.name + ".part"))
                    f = sc.setdefault("fields", {})
                    sup = [x for u in others for x in summary(u)]
                    old_sup = f.get("supporting") or []
                    f["supporting"] = old_sup + sup
                    f["supporting_text"] = "; ".join(
                        f"p{x.get('page')} {str(x.get('kind')).replace('_', ' ')}" + (f" {x.get('invoice_number') or x.get('order_number') or ''}".rstrip())
                        for x in f["supporting"])[:250]
                    pages = main["pages"] + [p for u in others for p in u["pages"]]
                    f["pages"] = len(pages)
                    f["source_pages"] = ", ".join(map(str, pages))
                    old_hash = f.get("content_hash")
                    f["content_hash"] = sha256(pdf.with_name(pdf.name + ".part")).upper()
                    sc["grouped_pages"] = pages
                    sc["merged"] = True                          # several pages now: never fed back to a reader (it would split it)
                    sc.setdefault("edits", []).append({"at": stamp, "by": "regroup_scans (Dino 29 Oct: one file = one document)",
                                                       "changes": [{"field": "pages", "old": len(main["pages"]), "new": len(pages)},
                                                                   {"field": "content_hash", "old": old_hash, "new": f["content_hash"]}]})
                    os.replace(pdf.with_name(pdf.name + ".part"), pdf)
                    tmp = main["json"].with_name(main["json"].name + ".rg.tmp")
                    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                    os.replace(tmp, main["json"])
                    for u in others:
                        route_sort.remove(u["json"], u["sc"], u["type"],
                                          f"page(s) {', '.join(map(str, u['pages']))} of {src}: supporting pages behind {sc['file']}", stamp)
                except Exception as e:
                    res = f"error: {e}"[:160]
                finally:
                    for cm in reversed(locks):
                        cm.__exit__(None, None, None)
            n_done += 1
            cnt[res.split(":")[0] if res.startswith("error") else res] += 1
            rows.append({"source_file": src, "document": main["json"].name, "where": f"{main['type']} {main['place']}",
                         "order_number": main["order"], "main_pages": ", ".join(map(str, main["pages"])),
                         "supporting_pages": ", ".join(str(p) for u in others for p in u["pages"]),
                         "supporting_files": len(others), "result": res})
    out = ROOT / "reports" / f"regroup scans {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys or ["source_file"])
        w.writeheader()
        w.writerows(rows)
    summ = {"applied": a.apply, "documents": sum(1 for r in rows if "document" in r),
            "supporting_files": sum(int(r.get("supporting_files") or 0) for r in rows if str(r.get("result", "")).startswith(("would", "grouped"))),
            "results": dict(cnt.most_common()), "report": f"{out}.csv"}
    json.dump(summ, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

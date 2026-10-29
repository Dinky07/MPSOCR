#!/usr/bin/env python3
"""Orphan pages on the order holds (Dino 29 Oct 09:04: "delete them, we will pick them up from other sources").

An orphan is a page document on hold (out/<Type>/hold) of a multi-page scan whose other pages went elsewhere earlier
(to the queue as an order, to Other Documents, to another queue, or uploaded), so there is nothing left on hold to
put it back together with. They go to deleted/<Type>/<yyyy-mm>/ with a reason (recoverable), like a dashboard delete.
Kept: an order form with its order number (that is a document, not a supporting page), a merged/grouped document,
a page that is checked, edited on the dashboard by a person, uploaded or locked, and a page with another page of the
same scan still on hold (coalesce_hold's job).
    python3 orphan_remove.py [--apply]
Report: reports/orphan pages removed <stamp>[ dry run].csv / .json
"""
import argparse, csv, glob, json, re, sys
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    where = defaultdict(list)                       # scan sha -> (place, type, page, json path)
    for pat, place in (("out/*/*.json", "queue"), ("out/*/hold/*.json", "hold"), ("synced/*/*/*.json", "synced")):
        for p in glob.glob(str(ROOT / pat)):
            if p.endswith((".tmp", ".lock")):
                continue
            try:
                sc = json.load(open(p, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            h = str(sc.get("source_sha256") or "").lower()
            if h:
                pages = sc.get("grouped_pages") or [m.get("page") for m in sc.get("merged_from") or []] or [sc.get("source_page")]
                where[h].append({"place": place, "type": Path(p).relative_to(ROOT).parts[1], "pages": pages, "json": Path(p)})
    rows, cnt = [], Counter()
    for t in TYPES:
        for p in sorted(glob.glob(str(ROOT / "out" / t / "hold" / "*.json"))):
            if p.endswith((".tmp", ".lock")):
                continue
            jp = Path(p)
            try:
                sc = json.load(open(jp, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if sc.get("merged") or not sc.get("source_page"):
                continue                            # whole scans and put-together documents are not orphans
            f = sc.get("fields") or {}
            h = str(sc.get("source_sha256") or "").lower()
            others = [w for w in where.get(h, []) if w["json"] != jp]
            why = None
            if f.get("form") in FORMS and f.get("order_number"):
                why = "kept: an order form"
            elif not others:
                why = "kept: no other page of the scan found (not an orphan)"
            elif [w for w in others if w["place"] == "hold" and w["type"] == t]:
                why = "kept: another page of the scan is still on hold"
            elif f.get("checked"):
                why = "kept: checked"
            elif [e for e in (sc.get("edits") or []) if not SCRIPT_EDIT.match(str(e.get("by") or ""))]:
                why = "kept: edited on the dashboard"
            elif Path(p + ".uploaded").exists():
                why = "kept: uploaded"
            elsewhere = "; ".join(f"p{','.join(str(x) for x in w['pages'] if x)} {w['type']} {w['place']}" for w in others)[:300]
            res = why or ("removed" if a.apply else "would remove")
            if a.apply and not why:
                try:
                    with sidecar_lock.locked(jp, "orphan_remove"):
                        route_sort.remove(jp, sc, t, f"orphan page {sc.get('source_page')} of {sc.get('source_file')} on hold; "
                                                     f"rest of the scan: {elsewhere} (Dino 29 Oct: delete, picked up from other sources)", stamp)
                except Exception as e:
                    res = f"error: {e}"[:160]
            cnt[res.split(":")[0] if res.startswith("error") else res] += 1
            rows.append({"type": t, "document": jp.name, "source_file": sc.get("source_file"), "page": sc.get("source_page"),
                         "read_as": f.get("library_doc_type") or f.get("po_doc_type") or f.get("form"),
                         "rest_of_scan": elsewhere, "result": res})
    out = ROOT / "reports" / f"orphan pages removed {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["document"])
        w.writeheader()
        w.writerows(rows)
    summ = {"applied": a.apply, "results": dict(cnt.most_common()), "report": f"{out}.csv"}
    json.dump(summ, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

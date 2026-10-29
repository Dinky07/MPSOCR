#!/usr/bin/env python3
"""Contracts already uploaded to the order libraries (Dino 29 Oct 10:00): Cartrack contracts / cancellations and other
contract documents that synced to Purchase Orders / Polymer Orders before the rules existed go to Other Documents too.

Each synced document (not an order form) is checked with the same route_rules ("Cartrack contract", "Contract"; the
model confirms where the rule asks). --apply: the document goes to out/OtherDocuments like route_sort moves it (summary,
supplier, type, category), carrying the old SharePoint item under "previous_sharepoint"; the old synced sidecar and its
.uploaded marker go to deleted/<Type>/<yyyy-mm>/ (recoverable). The report lists the old SharePoint links: once Other
Documents is synced, those items are removed from the order library (sp_remove list).
    python3 synced_contracts.py [--apply]
Report: reports/synced contracts <stamp>[ dry run].csv / .json
"""
import argparse, csv, glob, json, re, shutil, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import od_sort                                          # noqa: E402
import route_sort                                       # noqa: E402
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
RULES = ("Cartrack contract", "Contract")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    c = route_sort.config()
    rules = [r for r in c["rules"] if r["name"] in RULES]
    pcfg = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))
    vcfg = {"model": "qwen2.5vl:7b", "ollama_url": pcfg.get("ollama_url", "http://127.0.0.1:11434"), "num_ctx": int(pcfg.get("num_ctx", 49152))}
    stamp = datetime.now().isoformat(timespec="seconds")
    items = []
    for t in ("PurchaseOrders", "PolymerOrders"):
        for js in sorted(glob.glob(str(ROOT / "synced" / t / "*" / "*.json"))):
            js = Path(js)
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except Exception:
                continue
            f = sc.get("fields") or {}
            if f.get("form") in route_sort.ORDER_FORMS and f.get("order_number"):
                continue
            pdf = js.parent / str(sc.get("file") or "")
            if pdf.is_file():
                items.append((t, js, sc, pdf))

    def read(it):
        try:
            return it, route_sort.text_of(it[3])
        except Exception:
            return it, ""
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        texts = list(ex.map(read, items))
    route_sort.save_index()
    rows, cnt = [], Counter()
    for (t, js, sc, pdf), text in texts:
        f = sc.get("fields") or {}
        head = "\n".join((text or "").splitlines()[:12])[:700]
        fname = f"{sc.get('source_file') or ''} {pdf.name}"
        own = " ".join(str(v) for v in f.values() if isinstance(v, str))
        for r in rules:
            r["_t"] = t
            m = route_sort.matches(r, text or "", head, fname, own)
            if not m:
                continue
            ev, _ = m
            if r.get("confirm"):
                try:
                    if not route_sort.ask_yes_no(pdf, r["confirm"], vcfg):
                        cnt[f"{r['name']}: not confirmed by the model"] += 1
                        rows.append({"rule": r["name"], "type": t, "file": pdf.name, "result": "not confirmed", "evidence": ev[:200]})
                        break
                except Exception as e:
                    cnt[f"{r['name']}: model not reached"] += 1
                    break
            mk = Path(str(js) + ".uploaded")
            sp = json.load(open(mk, encoding="utf-8-sig")) if mk.exists() else {}
            result = "would move"
            if a.apply:
                try:
                    with sidecar_lock.locked(js, "synced_contracts"):
                        s = od_sort.summarise(pdf, text, vcfg)
                        dt = next((lab for rx, lab in (r.get("doc_types") or []) if re.search(rx, text, re.I | re.S)), None)
                        fields = {"summary": s.get("summary"), "od_doc_type": dt or s.get("doc_type"), "od_from": r.get("from") or s.get("from"),
                                  "date": s.get("date"), "order_number": s.get("order_number"), "job_number": s.get("job_number"),
                                  "od_category": r.get("category", "Other"), "od_rule": r["name"], "od_evidence": ev[:250],
                                  "moved_from": f"{t} (synced: {sp.get('webUrl') or 'SharePoint'})",
                                  "content_hash": f.get("content_hash") or od_sort.file_hash(pdf).upper(), "review": None,
                                  "library_doc_type": "Other Document"}
                        keep = json.loads(json.dumps(sc))
                        name = route_sort.move_out(js, sc, "OtherDocuments", fields, stamp, r["name"] + " (synced)")
                        nj = ROOT / "out" / "OtherDocuments" / (Path(name).stem + ".json")
                        new = json.load(open(nj, encoding="utf-8"))
                        new["previous_sharepoint"] = {"type": t, "driveId": sp.get("driveId"), "itemId": sp.get("itemId"),
                                                      "webUrl": sp.get("webUrl"), "name": sp.get("name"), "remove": True}
                        tmp = nj.with_name(nj.name + ".sc.tmp")
                        tmp.write_text(json.dumps(new, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                        tmp.replace(nj)
                        # the old reading and its marker are kept in deleted/ (move_out removed the synced sidecar)
                        dest = ROOT / "deleted" / t / datetime.now().strftime("%Y-%m")
                        dest.mkdir(parents=True, exist_ok=True)
                        keep["deleted_at"], keep["deleted_why"] = stamp, f"{r['name']}: moved to Other Documents as {name} (was synced)"
                        (dest / js.name).write_text(json.dumps(keep, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                        if mk.exists():
                            shutil.move(str(mk), str(dest / mk.name))
                        result = "moved -> " + name
                except sidecar_lock.Locked:
                    result = "locked"
                except Exception as e:
                    result = f"error: {e}"[:150]
            cnt[f"{r['name']}: {result.split(' -> ')[0]}"] += 1
            rows.append({"rule": r["name"], "type": t, "file": pdf.name, "source_file": sc.get("source_file"),
                         "result": result, "old_sharepoint": sp.get("webUrl"), "drive_id": sp.get("driveId"),
                         "item_id": sp.get("itemId"), "evidence": ev[:200]})
            break
    out = ROOT / "reports" / f"synced contracts {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    keys = list(dict.fromkeys(k for r in rows for k in r)) or ["file"]
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    summ = {"applied": a.apply, "synced_documents_looked_at": len(items), "results": dict(cnt.most_common()), "report": f"{out}.csv"}
    json.dump(summ, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

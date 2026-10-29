#!/usr/bin/env python3
"""Put the Purchase Orders hold back together (Dino 29 Oct 07:32): the reader splits a multi-page scan into one document
per page, so the hold shows page 1, page 2 ... of the same scan as separate files. Every original scan with two or more
pages on hold becomes ONE hold document again:

  - one file per order: a scan of several orders (a batch) is cut where the next order form starts, so each order keeps
    its own file with the pages behind it; a scan with one order (or none) becomes one file
  - all its pages are on hold and it is one file: the original scan itself (from done/<queue>/<month>/, by SHA-256)
  - some pages went elsewhere (an order page to the queue, a Saheti page to Other Documents ...): the pages that are on
    hold, joined in page order (pdfunite); the sidecar lists which pages and where the others are
The merged document keeps the reading of its best page (an order form with an order number, else the first page) as its
fields, and every page's own reading under "merged_from" (page, type read, order number, supplier, checks). It stays on
hold, marked "merged" so the night re-read and the re-sort leave it alone (re-reading would split it again).
The page documents go to deleted/<Type>/<yyyy-mm>/ with a reason file (recoverable).
Left alone: scans with a page that is checked, edited on the dashboard by a person, uploaded or locked; single pages.

    python3 coalesce_hold.py [--type PurchaseOrders] [--apply] [--limit N]
Report: reports/coalesce hold <stamp>[ dry run].csv / .json
"""
import argparse, csv, glob, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402  remove()
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
ORDER_FORMS = {"mps_order", "polymer_order", "internal_group_transfer", "ronin_order"}
SCRIPT_EDIT = re.compile(r"^(po_autocheck|route_sort|hold_resort|od_sort|requeue_po12|numbers_pass|fix_|supplier rule|ronin_)", re.I)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def npages(p):
    try:
        return int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True, timeout=60).stdout).group(1))
    except Exception:
        return None


def original(db, h):
    """The original scan in done/<folder>/<month>/ (state.db: sha256 -> folder, file name), checked by hash."""
    row = db.execute("SELECT folder, source FROM processed WHERE sha256=?", (h,)).fetchone()
    if not row:
        return None
    for p in sorted(glob.glob(str(ROOT / "done" / row[0] / "*" / glob.escape(row[1])))):
        if sha256(p).lower() == h:
            return Path(p)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="PurchaseOrders")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    t = a.type
    stamp = datetime.now().isoformat(timespec="seconds")
    cfg = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))
    db = sqlite3.connect(cfg.get("state_db", "/srv/ocr/pipeline/state.db"), timeout=30)
    # every page document of every scan, wherever it is now (to say where the other pages went)
    where = defaultdict(list)
    for pat, place in (("out/*/*.json", "queue"), ("out/*/hold/*.json", "hold"), ("synced/*/*/*.json", "synced")):
        for p in glob.glob(str(ROOT / pat)):
            if p.endswith((".tmp", ".lock")):
                continue
            try:
                sc = json.load(open(p, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            h = str(sc.get("source_sha256") or "").lower()
            if h and sc.get("source_page"):
                where[h].append({"place": place, "type": Path(p).relative_to(ROOT).parts[1], "page": int(sc["source_page"]),
                                 "json": Path(p), "sc": sc})
    rows, cnt = [], Counter()
    done = 0
    segments = []
    for h, docs in sorted(where.items()):
        hold_pages = sorted([d for d in docs if d["place"] == "hold" and d["type"] == t], key=lambda d: d["page"])
        if len(hold_pages) < 2:
            continue
        # one file per order: a new order form (with an order number different from the current file's) starts a new file
        segs, cur, cur_on = [], [], None
        for d in hold_pages:
            f = d["sc"].get("fields") or {}
            on = f.get("order_number") if f.get("form") in ORDER_FORMS else None
            if on and cur and cur_on and on != cur_on:
                segs.append(cur)
                cur, cur_on = [], None
            cur.append(d)
            cur_on = cur_on or on
        segs.append(cur)
        for seg in segs:
            segments.append((h, docs, seg, len(segs)))
    for h, docs, hp, nseg in segments:
        if len(hp) < 2:
            cnt["single page left as it is (scan cut into orders)"] += 1
            continue
        if a.limit and done >= a.limit:
            break
        others = sorted([d for d in docs if d not in hp], key=lambda d: d["page"])
        sc0 = hp[0]["sc"]
        why = None
        for d in hp:
            f = d["sc"].get("fields") or {}
            if f.get("checked"):
                why = f"page {d['page']} is checked"
            elif [e for e in (d["sc"].get("edits") or []) if not SCRIPT_EDIT.match(str(e.get("by") or ""))]:
                why = f"page {d['page']} was edited on the dashboard"
            elif Path(str(d["json"]) + ".uploaded").exists():
                why = f"page {d['page']} is uploaded"
            elif not (d["json"].parent / str(d["sc"].get("file") or "")).is_file():
                why = f"page {d['page']} PDF missing"
            if why:
                break
        pages = [d["page"] for d in hp]
        orig = original(db, h)
        total = npages(orig) if orig else None
        whole = bool(nseg == 1 and orig and total and pages == list(range(1, total + 1)))
        res = ("left: " + why) if why else ("would merge" if not a.apply else "merged") + (" (original scan)" if whole else " (pages joined)")
        best = next((d for d in hp if (d["sc"].get("fields") or {}).get("form") in ORDER_FORMS
                     and (d["sc"].get("fields") or {}).get("order_number")), hp[0])
        stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(sc0.get("source_file") or "scan")).stem)[:110].strip()
        label = "" if whole else (f" (p{pages[0]}-{pages[-1]})" if pages == list(range(pages[0], pages[-1] + 1)) else f" (p{','.join(map(str, pages))})"[:40])
        newname = ""
        if a.apply and not why:
            locks = []
            try:
                for d in hp:
                    cm = sidecar_lock.locked(d["json"], "coalesce_hold")
                    cm.__enter__()
                    locks.append(cm)
                hold = ROOT / "out" / t / "hold"
                name, k = f"{stem}{label}.pdf", 2
                while (hold / name).exists() or (hold / name).with_suffix(".json").exists():
                    name = f"{stem}{label}_{k}.pdf"
                    k += 1
                tmp_pdf = hold / (name + ".part")
                if whole:
                    shutil.copy2(orig, tmp_pdf)
                else:
                    with tempfile.TemporaryDirectory() as td:
                        out = Path(td) / "m.pdf"
                        r = subprocess.run(["pdfunite"] + [str(d["json"].parent / d["sc"]["file"]) for d in hp] + [str(out)],
                                           capture_output=True, text=True, timeout=300)
                        if r.returncode != 0 or not out.exists():
                            raise RuntimeError(f"pdfunite: {r.stderr[:200]}")
                        shutil.copy2(out, tmp_pdf)
                b = json.loads(json.dumps(best["sc"]))
                f = b.setdefault("fields", {})
                f["content_hash"] = sha256(tmp_pdf).upper()
                f["pages"] = npages(tmp_pdf)
                f["source_pages"] = "all" if whole else ", ".join(map(str, pages))
                b.update(file=name, doc_id=f"{h[:16]}-m", source_page=None, merged=True, merged_at=stamp,
                         merged_from=[{"page": d["page"], "doc_id": d["sc"].get("doc_id"), "file": d["sc"].get("file"),
                                       "form": (d["sc"].get("fields") or {}).get("form"),
                                       "doc_type": (d["sc"].get("fields") or {}).get("library_doc_type") or (d["sc"].get("fields") or {}).get("po_doc_type"),
                                       "order_number": (d["sc"].get("fields") or {}).get("order_number"),
                                       "supplier": (d["sc"].get("fields") or {}).get("supplier"),
                                       "hold_reason": d["sc"].get("hold_reason"),
                                       "checks": (d["sc"].get("fields") or {}).get("checks")} for d in hp],
                         other_pages=[{"page": d["page"], "where": f"{d['type']} {d['place']}", "file": d["sc"].get("file")}
                                      for d in sorted(others, key=lambda d: d["page"])])
                b["hold_reason"] = (f"scan of {total or '?'} pages put back together ({len(hp)} on hold"
                                    + (f"; page(s) {', '.join(str(d['page']) for d in others)} elsewhere" if others else "") + ")")
                b["flags"] = ["held: " + b["hold_reason"]] + [x for x in (b.get("flags") or []) if not str(x).startswith("held: ")]
                b.setdefault("edits", []).append({"at": stamp, "by": "coalesce_hold (Dino 29 Oct: one file per scan on hold)",
                                                  "changes": [{"field": "pages", "old": len(hp), "new": f["pages"]}]})
                j = (hold / name).with_suffix(".json")
                os.replace(tmp_pdf, hold / name)
                jt = j.with_name(j.name + ".tmp")
                jt.write_text(json.dumps(b, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                os.replace(jt, j)
                for d in hp:
                    route_sort.remove(d["json"], d["sc"], t, f"page {d['page']} of {stem}: put back into one file as {name}", stamp)
                newname = name
            except Exception as e:
                res = f"error: {e}"[:160]
            finally:
                for cm in reversed(locks):
                    cm.__exit__(None, None, None)
        done += 1
        cnt[re.sub(r"page \d+", "a page", res.split(":")[0] if res.startswith("error") else res)] += 1
        rows.append({"source_file": sc0.get("source_file"), "pages_on_hold": ", ".join(map(str, pages)), "pages_in_scan": total,
                     "other_pages": "; ".join(f"p{d['page']} {d['type']} {d['place']}" for d in sorted(others, key=lambda d: d["page"])),
                     "orders_in_scan": nseg, "kept_reading_of": f"page {best['page']}", "order_number": (best["sc"].get("fields") or {}).get("order_number"),
                     "result": res, "new_file": newname})
    out = ROOT / "reports" / f"coalesce hold {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["source_file"])
        w.writeheader()
        w.writerows(rows)
    summ = {"type": t, "applied": a.apply, "scans": len(rows), "page_documents": sum(len(r["pages_on_hold"].split(",")) for r in rows),
            "results": dict(cnt.most_common()), "report": f"{out}.csv"}
    json.dump(summ, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Re-sort the Purchase Orders / Polymer Orders hold (Dino 28 Oct 15:45): documents held as "not an order" go through
the pre-sort again and, where it is confident, to the queue they belong in (test certificates, workshop sheets,
supplier invoices, proofs of payment, delivery notes, job transfers) to be read by that queue's reader.

    python3 hold_resort.py eval                       # the pre-sort on Dino's labelled test set: precision per kind
    python3 hold_resort.py run [--apply] [--allow kind:strength,...]

eval: page 1 of every test-set document (work/sorter/testset) through presort.verdict(), against the TYPE Dino set in
testset.xlsx. Prints, per verdict kind and strength, how many were right. -> reports/presort eval <stamp>.json
run: every hold document that is not a recognised order form (MPS A-number, Polymer PL-number, IGT), not checked and
not uploaded; text from the OCR cache (route_sort / od_sort). A verdict whose kind:strength is allowed and whose queue
is not Purchase Orders sends the document to in/<queue>/ (copy, with the usual _meta note "rerouted_from"), and the
hold copy goes to deleted/<Type>/<yyyy-mm>/ with the reason (recoverable). Default --allow: what eval shows at >= 95 %.
Report: reports/hold resort <stamp>[ dry run].csv / .json
"""
import argparse, csv, glob, json, os, re, shutil, subprocess, sys, tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import presort                                          # noqa: E402
import route_sort                                       # noqa: E402  text_of() (OCR cache)
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
TS = ROOT / "work" / "sorter" / "testset"
ORDER_FORMS = {"mps_order", "polymer_order", "internal_group_transfer"}
LABEL = {"sales_quotation": "quotation", "pro_forma": "quotation", "internal_group_transfer": "purchase_order", "mps_invoice": "supplier_invoice",
         "release_note": "other"}


def label_of(t):
    t = str(t or "").strip().lower()
    return LABEL.get(t, t)


def eval_testset(a):
    import openpyxl
    ws = openpyxl.load_workbook(TS / "testset.xlsx", read_only=True, data_only=True)["Pages"]
    it = ws.iter_rows(values_only=True)
    hdr = [str(x or "") for x in next(it)]
    rows = [dict(zip(hdr, r)) for r in it if r and r[0]]
    ti = next(h for h in hdr if h.upper().startswith("TYPE"))
    jobs = []
    for r in rows:
        m = re.match(r"^(.*)-p(\d+)$", str(r["ID"]))
        if not m or int(m.group(2)) != 1:
            continue
        pdf = TS / "pdf" / f"{m.group(1)}.pdf"
        if pdf.exists():
            jobs.append((r, pdf))
    # 341: the documents Dino moved on the dashboard (Move to...) are labelled pages too; the latest record counts
    n_ts, mv = len(jobs), ROOT / "work" / "sorter" / "moves" / "moves.jsonl"
    if mv.exists():
        last = {}
        for line in open(mv, encoding="utf-8"):
            try:
                m = json.loads(line)
                last[m["id"]] = m
            except (ValueError, KeyError):
                continue
        for m in last.values():
            p = mv.parent / m["pdf"]
            if p.exists():
                jobs.append(({"ID": "MOVE-" + m["id"] + "-p1", ti: m["type"], "Original file": m.get("original_file"), "Queue": m.get("from")}, p))
    n_mv = len(jobs) - n_ts
    # Dino's labelled hold documents (work/sorter/labels/labels.xlsx, 30 Oct) are labelled pages too
    lx = ROOT / "work" / "sorter" / "labels" / "labels.xlsx"
    if lx.exists():
        lit = openpyxl.load_workbook(lx, read_only=True, data_only=True)["Documents"].iter_rows(values_only=True)
        lh = [str(x or "") for x in next(lit)]
        for lr in lit:
            d = dict(zip(lh, lr))
            p = lx.parent / "pdf" / f"{d.get('ID')}.pdf"
            if d.get("ID") and d.get("TYPE") and p.exists():
                jobs.append(({"ID": f"LBL-{d['ID']}-p1", ti: d["TYPE"], "Original file": d.get("Original file"),
                              "Queue": d.get("Original folder")}, p))
    n_lb = len(jobs) - n_ts - n_mv
    print(f"test set {n_ts} pages + {n_mv} moved on the dashboard + {n_lb} labelled hold documents", flush=True)

    def one(j):
        r, pdf = j
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "p1.pdf"
            subprocess.run(["pdfseparate", "-f", "1", "-l", "1", str(pdf), str(p1)], capture_output=True)
            v = presort.verdict(p1 if p1.exists() else pdf, r.get("Original file"), r.get("Queue"))
        v.pop("_page_text", None)
        return r, v
    stats = defaultdict(lambda: [0, 0])
    wrong = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for r, v in ex.map(one, jobs):
            truth = label_of(r[ti])
            k = f"{v['kind']}:{v['strength']}"
            stats[k][1] += 1
            if v["kind"] == truth:
                stats[k][0] += 1
            elif v["kind"] != "unsure":
                wrong.append({"id": r["ID"], "verdict": k, "truth": truth, "signals": v.get("signals")})
    out = {"pages": len(jobs), "from_moves": n_mv, "from_labels": n_lb, "by_verdict": {k: {"right": s[0], "of": s[1], "precision": round(s[0] / s[1], 3)}
                                             for k, s in sorted(stats.items())}, "wrong": wrong}
    p = ROOT / "reports" / f"presort eval {datetime.now():%Y-%m-%d %H%M}.json"
    json.dump(out, open(p, "w"), indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "wrong"}, indent=1))
    print("wrong:", json.dumps(wrong[:40], indent=1))
    print("report:", p)


def run(a):
    allow = {x.strip() for x in a.allow.split(",") if x.strip()}
    stamp = datetime.now().isoformat(timespec="seconds")
    cands = []
    for t in ("PurchaseOrders", "PolymerOrders"):
        for js in sorted(glob.glob(str(ROOT / "out" / t / "hold" / "*.json"))):
            js = Path(js)
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except Exception:
                continue
            f = sc.get("fields") or {}
            if (f.get("form") in ORDER_FORMS and f.get("order_number")) or f.get("checked") or Path(str(js) + ".uploaded").exists() \
                    or sc.get("merged"):
                continue
            pdf = js.parent / str(sc.get("file") or "")
            if pdf.is_file():
                cands.append((t, js, sc, pdf))

    def one(c):
        t, js, sc, pdf = c
        try:
            text = route_sort.text_of(pdf)
            v = presort.verdict(pdf, sc.get("source_file"), sc.get("source_folder"), texts=[text] if text.strip() else None)
            v.pop("_page_text", None)
        except Exception as e:
            v = {"kind": "error", "strength": "unsure", "dest": None, "signals": [str(e)[:120]]}
        return c, v
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = list(ex.map(one, cands))
    route_sort.save_index()
    rows, cnt = [], Counter()
    for (t, js, sc, pdf), v in res:
        key = f"{v.get('kind')}:{v.get('strength')}"
        dest = v.get("dest")
        go = bool(dest) and dest != "PurchaseOrders" and key in allow
        result = ("would move to " if not a.apply else "moved to ") + dest if go else "stays"
        if go and a.apply:
            try:
                with sidecar_lock.locked(js, "hold_resort"):
                    if not js.exists() or not pdf.exists():
                        result = "gone meanwhile"
                    else:
                        d, md = ROOT / "in" / dest, ROOT / "in" / dest / "_meta"
                        md.mkdir(parents=True, exist_ok=True)
                        stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(sc.get("source_file") or pdf.name)).stem)[:110]
                        page = sc.get("source_page")
                        name, k = f"{stem}{f'_p{page}' if page else ''}.pdf", 2
                        while (d / name).exists() or (md / (name + ".json")).exists():
                            name = f"{stem}{f'_p{page}' if page else ''}_{k}.pdf"
                            k += 1
                        note = {"original_name": sc.get("source_file") or pdf.name, "rerouted_from": f"out/{t}/hold/{pdf.name} (hold re-sort)",
                                "presort": v, "routed_at": stamp}
                        if sc.get("source_folder"):
                            note["source_folder"] = sc["source_folder"]
                        (md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
                        shutil.copy2(pdf, d / (name + ".part"))
                        os.replace(d / (name + ".part"), d / name)
                        route_sort.remove(js, sc, t, f"re-sorted to {dest} ({key}): {'; '.join(v.get('signals') or [])}"[:300], stamp)
                        result = f"moved to {dest}"
            except sidecar_lock.Locked:
                result = "locked (next run)"
        cnt[f"{key} -> {result.replace('would move to ', '').replace('moved to ', '')}" if go else f"{key} (stays)"] += 1
        rows.append({"type": t, "file": pdf.name, "source_file": sc.get("source_file"), "kind": v.get("kind"),
                     "strength": v.get("strength"), "dest": dest, "result": result, "signals": "; ".join(v.get("signals") or [])[:300]})
    out = ROOT / "reports" / f"hold resort {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)
    summary = {"applied": a.apply, "allow": sorted(allow), "hold_documents": len(cands), "results": dict(cnt.most_common()),
               "report": f"{out}.csv"}
    json.dump(summary, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("eval")
    r = sp.add_parser("run")
    r.add_argument("--apply", action="store_true")
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--allow", default="delivery_note:sure,supplier_invoice:sure,job_transfer:sure")
    a = ap.parse_args()
    eval_testset(a) if a.cmd == "eval" else run(a)

#!/usr/bin/env python3
"""Dry run of the workshop-sheet stamp gate (27 Oct) on the sheets already read (out/WorkshopSheets, queue and hold):
what ws_stamp.check() would decide for each, nothing changed. Writes reports/ws stamp gate <date>.csv/.json and the
stamp crops of up to --crops sheets to work/ws_stamp/crops (to check the close date reading by eye).
With --apply: writes stamp_check / stamp_ok / the review check into each sidecar and moves sheets that are not clean to
the hold queue (and clean held-only-for-the-stamp ones back) - the same result as the worker's gate for new sheets.

    python3 ws_stamp_eval.py [--limit N] [--crops 40] [--apply]
"""
import argparse, csv, glob, json, os, shutil, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import ws_stamp, ws_reader                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
CFG = json.load(open("/srv/ocr/pipeline/config.json"))
WS_GATE = {"no_stamp": "no IFC stamp: not imported", "check_stamp": "IFC stamp not certain: check",
           "check_date": "stamp date not certain: check"}
ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--crops", type=int, default=40)
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
wc = (CFG["doc_types"].get("WorkshopSheets") or {}).get("ws_reader") or {}
model = wc.get("model", "qwen2.5vl:7b")
crops = ROOT / "work" / "ws_stamp" / "crops"
crops.mkdir(parents=True, exist_ok=True)
files = sorted(glob.glob(str(ROOT / "out" / "WorkshopSheets" / "*.json")) + glob.glob(str(ROOT / "out" / "WorkshopSheets" / "hold" / "*.json")))
if a.limit:
    files = files[: a.limit]
rows, t0, nc = [], time.time(), 0
for n, js in enumerate(files, 1):
    js = Path(js)
    try:
        sc = json.load(open(js, encoding="utf-8"))
    except Exception:
        continue
    pdf = js.with_name(str(sc.get("file") or ""))
    if not pdf.is_file():
        continue
    f = sc.get("fields") or {}
    how = sc.get("ws_how") or ("text_layer" if "text layer" in str(sc.get("reader") or "") else "vision")
    try:
        g = ws_stamp.check(pdf, f, how, CFG["ollama_url"], model, CFG.get("keep_alive", "15m"), 600, ws_reader.stamp_date)
    except Exception as e:
        g = {"status": "check_stamp", "error": str(e)[:200]}
    if nc < a.crops and g.get("box") and g.get("stamp") != "none":
        try:
            st = ws_stamp.find(pdf)
            if st:
                ws_stamp.crop(pdf, st[0], str(crops / (js.stem + ".png")))
                nc += 1
        except Exception:
            pass
    rows.append({"file": js.name, "where": "hold" if js.parent.name == "hold" else "queue", "how": how,
                 "status": g.get("status"), "stamp": g.get("stamp"), "red_ink": g.get("red_ink"), "stamp_words": " ".join(g.get("stamp_words") or []),
                 "model_stamped": g.get("model_stamped"), "date": g.get("date"), "date_page": g.get("date_page"),
                 "date_close": g.get("date_close"), "date_close_as_written": g.get("date_close_as_written"),
                 "date_page_as_written": f.get("stamp_date_as_written"), "warnings": "; ".join(g.get("date_warnings") or []),
                 "error": g.get("error") or g.get("close_read_error") or ""})
    if a.apply:
        sc = json.load(open(js, encoding="utf-8"))           # fresh (dashboard edits)
        f = sc.setdefault("fields", {})
        f["stamp_check"], f["stamp_ok"] = g, g.get("status") == "clean"
        fl = [x for x in (sc.get("flags") or []) if x not in ["check: " + v for v in WS_GATE.values()] and not str(x).startswith("held: ")]
        if g.get("status") != "clean":
            fl.append("check: " + WS_GATE.get(g.get("status"), "IFC stamp: check"))
        held_for = WS_GATE.get(g.get("status")) if g.get("status") != "clean" else None
        if held_for:
            fl.append("held: " + held_for)
            sc["hold_reason"] = held_for
        else:
            sc.pop("hold_reason", None)
        sc["flags"], sc["needs_review"] = fl, bool(fl)
        tmp = js.with_name(js.name + ".st.tmp")
        tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, js)
        want = js.parent.parent / "hold" if (held_for and js.parent.name != "hold") else js.parent.parent if (not held_for and js.parent.name == "hold") else None
        if want is not None and not (want / js.name).exists():
            want.mkdir(exist_ok=True)
            shutil.move(str(pdf), str(want / pdf.name))
            shutil.move(str(js), str(want / js.name))
    if n % 25 == 0:
        print(f"  {n}/{len(files)} {time.time() - t0:.0f}s", flush=True)
c = {}
for r in rows:
    c[r["status"]] = c.get(r["status"], 0) + 1
both = [r for r in rows if r["date_page"] and r["date_close"]]
summary = {"sheets": len(rows), "status": c, "scanned": sum(1 for r in rows if r["how"] != "text_layer"),
           "stamp": {k: sum(1 for r in rows if r["stamp"] == k) for k in ("high", "low", "none")},
           "red_ink": sum(1 for r in rows if r["red_ink"]), "model_stamped": sum(1 for r in rows if r["model_stamped"]),
           "dates_both_read": len(both), "dates_agree": sum(1 for r in both if r["date_page"] == r["date_close"]),
           "secs": round(time.time() - t0), "applied": a.apply}
out = ROOT / "reports" / f"ws stamp gate {datetime.now():%Y-%m-%d}{'' if a.apply else ' dry run'}"
with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
json.dump(summary, open(f"{out}.json", "w"), indent=1)
print(json.dumps(summary, indent=1))

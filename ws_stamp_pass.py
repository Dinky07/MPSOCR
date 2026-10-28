#!/usr/bin/env python3
"""Workshop-sheet stamp pass (Dino 27 Oct): only sheets with the MPS "ISSUED FOR CONSTRUCTION" stamp are imported, and a
sheet is clean when the stamp's date is within a month of the WS date. ws_stamp.check() (v2) decides per sheet with
close reads of the stamp on qwen3-vl. Run after a batch is read (one model load), on the sheets in out/WorkshopSheets
(queue and hold) that have no stamp_check of this version yet; --all re-checks every sheet.

DRY RUN unless --apply: reports/ws stamp pass <stamp>[ dry run].csv/.json, nothing changed.
--apply, per sheet:
  fields.stamp_check (the whole verdict), fields.stamp_ok;
  fields.stamped / stamp_date / stamp_date_as_written from the close read (the full-page reading copied the WS date
    into blank stamps; it is kept once as stamp_date_page / stamp_date_page_as_written / stamped_page);
  fields.stamp_copy: the stamp was found only in black and white (no red ink): a copy (Dino 28 Oct, ws-stamp-3);
  a review check for anything not clean; not clean -> hold with the reason; a clean sheet that was held ONLY by this
  gate goes back to the queue. Sheets held for other reasons stay held.
Workshop sheets sync only on request, so nothing reaches SharePoint from here.

    python3 ws_stamp_pass.py [--all] [--apply] [--limit N]
"""
import argparse, csv, glob, json, os, shutil, sys, time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import ws_stamp                                   # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
CFG = json.load(open("/srv/ocr/pipeline/config.json"))
GATE = {"no_stamp": "no IFC stamp: not imported", "check_stamp": "IFC stamp not certain: check",
        "check_date": "stamp date not within a month of the WS date: check"}
OLD_GATE = {"IFC stamp: check", "no IFC stamp: not imported", "IFC stamp not certain: check", "stamp date not certain: check"}
ap = argparse.ArgumentParser()
ap.add_argument("--all", action="store_true")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--limit", type=int, default=0)
a = ap.parse_args()
wcfg = CFG["doc_types"].get("WorkshopSheets") or {}
model = wcfg.get("stamp_model", "qwen3-vl:8b-instruct")
q = ROOT / "out" / "WorkshopSheets"
files = sorted(glob.glob(str(q / "*.json")) + glob.glob(str(q / "hold" / "*.json")))
gate_texts = set(GATE.values()) | OLD_GATE
rows, t0 = [], time.time()
for js in files:
    if a.limit and len(rows) >= a.limit:
        break
    js = Path(js)
    try:
        sc = json.load(open(js, encoding="utf-8"))
    except Exception:
        continue
    f = sc.get("fields") or {}
    if not a.all and (f.get("stamp_check") or {}).get("version") == ws_stamp.VERSION:
        continue
    pdf = js.with_name(str(sc.get("file") or ""))
    if not pdf.is_file():
        continue
    how = sc.get("ws_how") or ("text_layer" if "text layer" in str(sc.get("reader") or "") else "vision")
    try:
        g = ws_stamp.check(pdf, f, how, CFG["ollama_url"], model, CFG.get("keep_alive", "15m"), 600,
                           name=str(sc.get("source_file") or sc.get("file") or ""))
    except Exception as e:
        g = {"version": ws_stamp.VERSION, "status": "check_stamp", "stamp": "unknown", "why": f"error: {str(e)[:160]}"}
    held = js.parent.name == "hold"
    hold_reason = sc.get("hold_reason")
    rows.append({"file": js.name, "where": "hold" if held else "queue", "hold_reason": hold_reason or "", "how": how,
                 "status": g.get("status"), "why": g.get("why") or "", "red_ink": g.get("red_ink"),
                 "candidates": len(g.get("candidates") or []), "stamp_date": g.get("stamp_date") or "",
                 "as_written": g.get("stamp_date_as_written") or "", "sheet_date": g.get("sheet_date") or "",
                 "gap_days": g.get("gap_days", ""), "copy": g.get("copy", ""), "page_stamped": f.get("stamped"), "page_stamp_date": f.get("stamp_date") or "",
                 "secs": round(time.time() - t0, 1)})
    if len(rows) % 25 == 0:
        print(f"  {len(rows)} sheets, {time.time() - t0:.0f}s", flush=True)
    if not a.apply:
        continue
    sc = json.load(open(js, encoding="utf-8"))          # fresh (dashboard edits)
    f = sc.setdefault("fields", {})
    if "stamp_date_page" not in f:
        f["stamped_page"], f["stamp_date_page"], f["stamp_date_page_as_written"] = f.get("stamped"), f.get("stamp_date"), f.get("stamp_date_as_written")
    f["stamp_check"], f["stamp_ok"] = g, g.get("status") == "clean"
    f["stamp_copy"] = bool(g.get("copy"))          # Dino 28 Oct: black-and-white stamp = a copy of the sheet
    if g.get("stamp") in ("high", "none"):
        f["stamped"] = g.get("stamp") == "high"
        f["stamp_date"] = g.get("stamp_date")
        f["stamp_date_as_written"] = g.get("stamp_date_as_written")
    reason = GATE.get(g.get("status"))
    fl = [x for x in (sc.get("flags") or []) if not any(str(x) in (f"check: {t}", f"held: {t}") for t in gate_texts)]
    if reason:
        fl.append(f"check: {reason}" + (f" ({g['why']})" if g.get("why") and g.get("status") != "no_stamp" else ""))
    move_to = None
    if reason:
        if not held:
            sc["hold_reason"] = reason
            fl.append(f"held: {reason}")
            move_to = q / "hold"
        elif not hold_reason or hold_reason in gate_texts:
            sc["hold_reason"] = reason
            fl.append(f"held: {reason}")
    elif held and hold_reason in gate_texts:
        sc.pop("hold_reason", None)
        move_to = q
    sc["flags"], sc["needs_review"] = fl, bool([x for x in fl if not str(x).startswith("held: ")]) or bool(sc.get("hold_reason"))
    tmp = js.with_name(js.name + ".sp.tmp")
    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, js)
    if move_to is not None and not (move_to / js.name).exists():
        move_to.mkdir(exist_ok=True)
        shutil.move(str(pdf), str(move_to / pdf.name))
        shutil.move(str(js), str(move_to / js.name))

c = Counter(r["status"] for r in rows)
summary = {"sheets": len(rows), "status": dict(c), "applied": a.apply, "model": model,
           "cad_text_layer": sum(1 for r in rows if r["how"] == "text_layer"),
           "scans_with_red_ink": sum(1 for r in rows if r["red_ink"]),
           "copies_black_and_white_stamp": sum(1 for r in rows if r["copy"] is True),
           "check_date_why": dict(Counter(("blank" if "blank" in r["why"] else "not readable" if "readable" in r["why"]
                                           else "outside the month" if "days from" in r["why"] else r["why"][:40])
                                          for r in rows if r["status"] == "check_date")),
           "page_reading_said_stamped": sum(1 for r in rows if r["page_stamped"] is True),
           "secs": round(time.time() - t0)}
out = ROOT / "reports" / f"ws stamp pass {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
json.dump(summary, open(f"{out}.json", "w"), indent=1)
print(json.dumps(summary, indent=1))

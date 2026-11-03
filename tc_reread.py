#!/usr/bin/env python3
"""448 (Dino 3 Nov "Re-read all 95"): every test cert on the Test Certs hold is read again by the current reader (the 445
fix: an empty certificate page is asked once more). The hold PDF goes to in/TestCerts with a _meta note that carries
what the worker takes from a feed note - family and the old-site link (old_columns, old_url, sp_item_id, sp_url,
sync_mode), so a backfill cert still updates its existing library item - plus "reread_of"; the hold copy goes to
deleted/TestCerts/<yyyy-mm>/ (recoverable). Skipped: checked, edited by a person, uploaded, locked. A re-read cert is
taken off tc_incomplete's "released by a person" ledger, so if it still reads incomplete it is held again.
453 (Dino 3 Nov "Yes, re-read them"): --sideways re-reads only the certs whose first page Tesseract finds sideways
(read upright by the reader since 453).
    tc_reread.py [--apply] [--sideways]"""
import csv, glob, json, os, re, shutil, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                       # noqa: E402
import tc_reader                                        # noqa: E402
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
H, IN = ROOT / "out" / "TestCerts" / "hold", ROOT / "in" / "TestCerts"
LEDGER = ROOT / "work" / "tc_incomplete" / "held.csv"
apply = "--apply" in sys.argv
only_sideways = "--sideways" in sys.argv


def sideways(pdf):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return tc_reader.upright(tc_reader.render(str(pdf), 1, 1600, os.path.join(tmp, "p.png")))
        except Exception:
            return 0


stamp = datetime.now().isoformat(timespec="seconds")
md = IN / "_meta"
cnt, rows, fed_hashes = Counter(), [], set()
for js in sorted(H.glob("*.json")):
    try:
        sc = json.load(open(js, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    f = sc.get("fields") or {}
    pdf = H / str(sc.get("file") or "")
    why = ("checked" if f.get("checked") else "edited by a person" if [e for e in sc.get("edits") or [] if "by" not in e] else
           "uploaded" if Path(str(js) + ".uploaded").exists() else "PDF missing" if not pdf.is_file() else None)
    if not why and only_sideways and not sideways(pdf):
        cnt["not sideways"] += 1
        continue
    if why:
        cnt["skipped: " + why] += 1
        rows.append((js.name, "skipped: " + why))
        continue
    stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(sc.get("source_file") or pdf.name)).stem)[:110].strip()
    name, k = stem + ".pdf", 2
    while (IN / name).exists() or (md / (name + ".json")).exists() or list((ROOT / "work").glob(f"TestCerts__{glob.escape(name)}")):
        name, k = f"{stem}_{k}.pdf", k + 1
    if not apply:
        cnt["would re-read"] += 1
        rows.append((js.name, f"would re-read as in/TestCerts/{name}"))
        continue
    try:
        with sidecar_lock.locked(js, "tc_reread", wait=5):
            sc = json.load(open(js, encoding="utf-8"))
            f = sc.get("fields") or {}
            note = {"original_name": sc.get("source_file") or pdf.name, "reread_of": f"out/TestCerts/hold/{js.name}", "reread_at": stamp,
                    "why": "test cert on hold re-read by the current reader (Dino 3 Nov)" + (", sideways: read upright (453)" if only_sideways else ""), "previous_hold_reason": sc.get("hold_reason")}
            for k2 in ("source_folder", "source_modified"):
                if sc.get(k2):
                    note[k2] = sc[k2]
            if f.get("family"):
                note["family"] = f["family"]
            for k2 in ("old_columns", "old_url", "sp_item_id", "sp_url", "sync_mode"):
                if f.get(k2) not in (None, ""):
                    note[k2] = f[k2]
            (md / (name + ".json")).parent.mkdir(parents=True, exist_ok=True)
            (md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
            shutil.copy2(pdf, IN / (name + ".part"))
            try:
                route_sort.remove(js, sc, "TestCerts", f"re-read by the current test-cert reader (3 Nov{', upright' if only_sideways else ''}); fed as in/TestCerts/{name}", stamp)
            except Exception as e:
                (IN / (name + ".part")).unlink(missing_ok=True)
                (md / (name + ".json")).unlink(missing_ok=True)
                raise RuntimeError(f"not moved to deleted/ ({e}); not fed")
            os.replace(IN / (name + ".part"), IN / name)
            fed_hashes.add(str(f.get("content_hash") or ""))
            cnt["re-read"] += 1
            rows.append((js.name, f"in/TestCerts/{name}"))
    except sidecar_lock.Locked:
        cnt["skipped: locked"] += 1
        rows.append((js.name, "skipped: locked"))
    except Exception as e:
        cnt["failed"] += 1
        rows.append((js.name, f"failed: {str(e)[:120]}"))
if apply and fed_hashes and LEDGER.exists():
    keep = [r for r in csv.DictReader(open(LEDGER, encoding="utf-8")) if r["content_hash"] not in fed_hashes]
    with open(LEDGER, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["at", "content_hash"])
        w.writeheader()
        w.writerows(keep)
out = ROOT / "reports" / f"tc reread {datetime.now():%Y-%m-%d %H%M}{' sideways' if only_sideways else ''}{'' if apply else ' dry run'}.csv"
with open(out, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["document", "result"])
    w.writerows(rows)
print(json.dumps({"applied": apply, "results": dict(cnt.most_common()), "report": str(out)}, indent=1))

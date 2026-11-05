#!/usr/bin/env python3
"""Combined test certs already in SharePoint (Dino 5 Nov "Replace"): an old-site item holding several certs is fed again
to be split by test certificate number, its page readings reused (no second read). Each part becomes its own document
with fields.replaces = the combined item (its SharePoint ids and content hash); the PC sync uploads the parts and, once no
part of it is still waiting in the queue or on hold, deletes the combined item, its Test Cert Items lines and its SQL rows
(work/tc_split/replace.jsonl lists them). The synced sidecar and PDF stay where they are until then.
    tc_split_synced.py [--apply] [--limit N]       (dry run unless --apply)"""
import argparse, glob, json, os, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import tc_reader as T                                   # noqa: E402

R = Path("/srv/ocr/scans/pipeline")
IN, MD, WORK = R / "in" / "TestCerts", R / "in" / "TestCerts" / "_meta", R / "work" / "tc_split"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    done = set()
    if (WORK / "replace.jsonl").exists():
        done = {json.loads(l)["content_hash"] for l in open(WORK / "replace.jsonl", encoding="utf-8") if l.strip()}
    n, certs, fed = 0, 0, []
    for p in sorted(glob.glob(str(R / "synced" / "TestCerts" / "*" / "*.json"))):
        js = Path(p)
        if js.name.endswith((".deleted.json", ".rerouted.json")) or not Path(p + ".uploaded").exists():
            continue
        try:
            sc = json.load(open(js, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        f = sc.get("fields") or {}
        h = str(f.get("content_hash") or "").upper()
        if sc.get("doc_type") != "test_cert" or f.get("checked") or not h or h in done:
            continue
        g = T.split_certs(sc.get("tc_pages") or [])
        if len(g) < 2:
            continue
        pdf = js.with_name(str(sc.get("file") or ""))
        if not pdf.is_file():
            print(f"  PDF missing: {js.name}")
            continue
        if a.limit and n >= a.limit:
            break
        n += 1
        certs += len(g)
        sp = json.load(open(str(js) + ".uploaded", encoding="utf-8-sig"))
        name = Path(str(sc.get("source_file") or pdf.name)).name
        if a.apply:
            keep = WORK / f"synced__{js.stem}.json"
            WORK.mkdir(parents=True, exist_ok=True)
            shutil.copy2(js, keep)
            k, nm = 2, name
            while (IN / nm).exists() or (MD / (nm + ".json")).exists():
                nm, k = f"{Path(name).stem}_{k}{Path(name).suffix}", k + 1
            rep = {"content_hash": h, "sidecar": str(js.relative_to(R)), "file": sc.get("file"), "certs": len(g),
                   "drive_id": sp.get("driveId"), "item_id": sp.get("itemId"), "web_url": sp.get("webUrl"), "at": stamp}
            note = {"original_name": nm, "reread_of": rep["sidecar"], "reread_at": stamp, "reuse_tc_pages": str(keep.relative_to(R)),
                    "why": "combined old-site item split by test certificate number (Dino 5 Nov: replace)", "replaces": rep}
            for k2 in ("source_folder", "source_modified"):
                if sc.get(k2):
                    note[k2] = sc[k2]
            MD.mkdir(parents=True, exist_ok=True)
            (MD / (nm + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
            shutil.copy2(pdf, IN / (nm + ".part"))
            os.replace(IN / (nm + ".part"), IN / nm)
            with open(WORK / "replace.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rep) + "\n")
        fed.append(f"{js.name} -> {len(g)} certs")
    print(json.dumps({"applied": a.apply, "combined_items": n, "certs": certs}, indent=1))
    for x in fed[:30]:
        print("  " + x)


if __name__ == "__main__":
    main()

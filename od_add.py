#!/usr/bin/env python3
"""Put one PDF into the Other Documents queue (out/OtherDocuments) with a summary, category, supplier and document type,
like route_sort moves a document there. For documents that only exist in a SharePoint order library (Dino 29 Oct 10:00:
contracts uploaded to Purchase Orders on 16 Oct, outside the pipeline).
    python3 od_add.py --pdf <path> --category Contracts --doc-type Contract [--from "Name"] [--source-folder X] [--apply]"""
import argparse, hashlib, json, os, shutil, sqlite3, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, "/srv/ocr/pipeline")
import od_sort                                          # noqa: E402
import route_sort                                       # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--pdf", required=True); ap.add_argument("--category", required=True); ap.add_argument("--doc-type", required=True)
ap.add_argument("--from", dest="frm"); ap.add_argument("--source-folder"); ap.add_argument("--note", default="")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
pdf = Path(a.pdf)
h = hashlib.sha256(pdf.read_bytes()).hexdigest()
cfg = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))
db = sqlite3.connect(cfg.get("state_db", "/srv/ocr/pipeline/state.db"), timeout=30)
row = db.execute("SELECT folder, source FROM processed WHERE sha256=?", (h,)).fetchone()
print(f"{pdf.name}: sha256 {h[:16]}; read by the pipeline before: {row}")
text = route_sort.text_of(pdf)
vcfg = {"model": "qwen2.5vl:7b", "ollama_url": cfg.get("ollama_url", "http://127.0.0.1:11434"), "num_ctx": int(cfg.get("num_ctx", 49152))}
s = od_sort.summarise(pdf, text, vcfg)
fields = {"summary": s.get("summary"), "od_doc_type": a.doc_type, "od_from": a.frm or s.get("from"), "date": s.get("date"),
          "order_number": s.get("order_number"), "job_number": s.get("job_number"), "od_category": a.category,
          "od_rule": "od_add (Dino 29 Oct)", "od_evidence": a.note[:250], "moved_from": a.note[:200],
          "content_hash": h.upper(), "review": None, "library_doc_type": "Other Document"}
print(json.dumps(fields, indent=1, ensure_ascii=False))
if not a.apply:
    sys.exit(0)
dest = Path("/srv/ocr/scans/pipeline/out/OtherDocuments"); dest.mkdir(parents=True, exist_ok=True)
name, n = pdf.name, 2
while (dest / name).exists() or (dest / (Path(name).stem + ".json")).exists():
    name = f"{pdf.stem}_{n}{pdf.suffix}"; n += 1
stamp = datetime.now().isoformat(timespec="seconds")
sc = {"schema_version": 1, "doc_id": h[:16] + "-od", "doc_type": "other_doc", "folder": "OtherDocuments", "file": name,
      "source_file": pdf.name, **({"source_folder": a.source_folder} if a.source_folder else {}), "source_sha256": h,
      "source_page": None, "processed_at": stamp, "model": "qwen2.5vl:7b (summary)", "extractor": "od_add", "needs_review": False,
      "flags": [], "fields": fields, "edits": [{"at": stamp, "by": "od_add (Dino 29 Oct)", "changes": [{"field": "folder", "old": None, "new": "OtherDocuments"}]}]}
shutil.copy2(pdf, dest / (name + ".part")); os.replace(dest / (name + ".part"), dest / name)
tmp = dest / (Path(name).stem + ".json.tmp")
tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8"); os.replace(tmp, dest / (Path(name).stem + ".json"))
print("added ->", dest / name)

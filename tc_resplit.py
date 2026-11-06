#!/usr/bin/env python3
"""532 (Dino 6 Nov): a scanned test-cert batch already split into parts (out/TestCerts and its hold) is split again with
the current tc_reader.split_certs, its page readings reused (no GPU): the batch PDF (done/TestCerts) goes back to
in/TestCerts with a _meta note naming the readings, and its old parts go to deleted/TestCerts. A part re-read on the
hold since (no batch page numbers) gives its newer readings for the pages its hold copy in deleted/ names.
Refused when a part is checked, edited by a person or uploaded, or a batch page has no reading.
    tc_resplit.py <batch file name> [--apply]"""
import ast, glob, hashlib, json, os, re, shutil, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import route_sort                                            # noqa: E402
import sidecar_lock                                          # noqa: E402
import tc_reader as T                                        # noqa: E402

R = Path("/srv/ocr/scans/pipeline")
IN, MD, WORK = R / "in/TestCerts", R / "in/TestCerts/_meta", R / "work/tc_split"
name, apply = sys.argv[1], "--apply" in sys.argv
stamp = datetime.now().isoformat(timespec="seconds")
src = sorted(glob.glob(str(R / "done/TestCerts/*" / glob.escape(name))))
if not src:
    sys.exit(f"no done/TestCerts/*/{name}")
pdf = Path(src[-1])
digest = hashlib.sha256(pdf.read_bytes()).hexdigest()


def pages_of(spec):
    """"DOC.pdf, pages 8; 22 of 24" -> [8, 22]; "pages 15-21; 23-24" -> [15..21, 23, 24]"""
    m = re.search(r"pages (.+?) of \d+", str(spec or ""))
    out = []
    for part in (m.group(1).split(";") if m else []):
        a, _, b = part.strip().partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


dels = {}
for p in glob.glob(str(R / "deleted/TestCerts/*/*.json")):
    if p.endswith(".deleted.json"):
        continue
    try:
        sc = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if str(sc.get("source_sha256") or "").lower() == digest and (sc.get("fields") or {}).get("split_from"):
        dels[str((sc.get("fields") or {}).get("content_hash") or "").upper()] = sc
parts, pages, why = [], {}, []
for d in (R / "out/TestCerts", R / "out/TestCerts/hold"):
    for js in sorted(d.glob("*.json")):
        try:
            sc = json.load(open(js, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        f = sc.get("fields") or {}
        old = dels.get(str(f.get("content_hash") or "").upper())
        if sc.get("doc_type") != "test_cert" or not (str(sc.get("source_sha256") or "").lower() == digest or old):
            continue
        if f.get("checked") or [e for e in sc.get("edits") or [] if "by" not in e] or Path(str(js) + ".uploaded").exists():
            why.append(f"{js.name}: checked / edited / uploaded")
        tp = [q for q in sc.get("tc_pages") or [] if q.get("page")]
        nums = [q.get("batch_page") for q in tp] if all(q.get("batch_page") for q in tp) else pages_of((old or {}).get("fields", {}).get("split_from"))
        if len(nums) != len(tp):
            why.append(f"{js.name}: {len(tp)} readings for batch pages {nums}")
            continue
        for q, b in zip(tp, nums):
            a = q.get("answer")
            if isinstance(a, str):
                try:
                    a = ast.literal_eval(a)
                except (ValueError, SyntaxError):
                    a = {}
            pages[b] = {k: v for k, v in dict(q, page=b, answer=a if isinstance(a, dict) else {}).items() if k != "batch_page"}
        parts.append((js, sc, nums))
n = max(pages) if pages else 0
miss = [i for i in range(1, n + 1) if i not in pages]
print(f"{pdf.relative_to(R)}: {len(parts)} parts, {len(pages)} page readings; missing {miss or 'none'}")
for js, sc, nums in parts:
    print(f"  now: {nums} {js.parent.name}/{js.name}")
seq = [pages[i] for i in sorted(pages)]
new = T.split_certs(seq)
print("  new split:", new)
for g in new:
    sub = [pages[i] for i in g]
    f, _ = T.to_fields(T.merge(sub, name, None), name, None)
    print(f"    {g}: {f.get('cert_no')} | {f.get('mill')} | {f.get('family')} | {f.get('item_count')} items")
if why or miss:
    sys.exit("refused: " + "; ".join(why + ([f"pages {miss} not read"] if miss else [])))
if not apply:
    sys.exit(0)
WORK.mkdir(parents=True, exist_ok=True)
keep = WORK / f"resplit__{pdf.stem}.json"
keep.write_text(json.dumps({"source_sha256": digest, "tc_pages": seq, "pages_read": n, "file": pdf.name}, ensure_ascii=False),
                encoding="utf-8")
nm, k = name, 2
while (IN / nm).exists() or (MD / (nm + ".json")).exists():
    nm, k = f"{Path(name).stem}_{k}{Path(name).suffix}", k + 1
for js, sc, _ in parts:
    with sidecar_lock.locked(js, "tc_resplit", wait=10):
        route_sort.remove(js, json.load(open(js, encoding="utf-8")), "TestCerts",
                          f"split again (532: plate and flange certs in one part); the batch was fed as in/TestCerts/{nm}", stamp)
note = {"original_name": name, "reread_of": str(pdf.relative_to(R)), "reread_at": stamp, "reuse_tc_pages": str(keep.relative_to(R)),
        "why": "batch split again by cert number and mill (Dino 6 Nov: plate and flanges in one part)"}
MD.mkdir(parents=True, exist_ok=True)
(MD / (nm + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
shutil.copy2(pdf, IN / (nm + ".part"))
os.replace(IN / (nm + ".part"), IN / nm)
print(f"fed in/TestCerts/{nm}; {len(parts)} old parts to deleted/TestCerts")

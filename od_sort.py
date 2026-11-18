#!/usr/bin/env python3
"""Other Documents routing (Dino 28 Oct 2026, testset.xlsx notes): documents that are not company paperwork for the
pipeline's libraries go to the private "Other Documents" library on MPS Sales (Dino and Ivor only), with a short
summary. Rules (od_rules.json beside this file, editable):
    Saheti  - anything to do with SAHETI (the school)
    Alenti  - anything related to Alenti
    SARS    - anything from SARS (South African Revenue Service)

    python3 od_sort.py scan                 # OCR + rules over the candidates; summaries for the matches; review sheet
    python3 od_sort.py apply --sheet FILE   # move the rows marked Move = Y to out/OtherDocuments (PDF + new sidecar)

Candidates: every sidecar in the queue or hold of PurchaseOrders, PolymerOrders, ProofOfPayments, SupplierInvoices,
SupplierInvoicesArchive and DeliveryNotes that is not uploaded yet and not ticked Checked. Text = the sidecar's own
values + Tesseract on the first two pages (cached in work/od_text by file hash). Summaries: qwen2.5vl:7b on page 1.
The review sheet (reports/Other Documents review <stamp>.xlsx) lists every match with the rule, the words that
matched, the summary and a link to the PDF; Move is pre-filled Y. Nothing moves until `apply`.
"""
import argparse, base64, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
import model_call                      # every Ollama request goes through here
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                    # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = Path("/srv/ocr/scans/pipeline")
CACHE = ROOT / "work" / "od_text"
TYPES = ["PurchaseOrders", "PolymerOrders", "ProofOfPayments", "SupplierInvoices", "SupplierInvoicesArchive", "DeliveryNotes"]
DEST = "OtherDocuments"
DEFAULT_RULES = [
    {"name": "Saheti", "category": "Saheti", "pattern": r"\bSAHETI\b", "note": "anything to do with Saheti, the school"},
    {"name": "Alenti", "category": "Alenti", "pattern": r"\bALENTI\b", "note": "anything related to Alenti"},
    {"name": "SARS", "category": "SARS", "pattern": r"SOUTH\s+AFRICAN\s+REVENUE\s+SERVICE|\bSARS\b|\beFILING\b",
     "note": "anything from SARS (South African Revenue Service)"},
]


def rules():
    p = HERE / "od_rules.json"
    if not p.exists():
        p.write_text(json.dumps(DEFAULT_RULES, indent=1), encoding="utf-8")
    return [dict(r, rx=re.compile(r["pattern"], re.I)) for r in json.load(open(p, encoding="utf-8"))]


def run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def file_hash(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def ocr_text(pdf, pages=2):
    """Text layer if the PDF has one, else Tesseract on the first pages at 200 dpi (cached by the file's hash)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = file_hash(pdf)
    c = CACHE / f"{key}.txt"
    if c.exists():
        return c.read_text(encoding="utf-8", errors="replace")
    txt = run(["pdftotext", "-l", str(pages), "-layout", str(pdf), "-"], 120).stdout
    if len(txt.strip()) < 80:
        with tempfile.TemporaryDirectory() as td:
            run(["pdftoppm", "-r", "200", "-gray", "-l", str(pages), "-png", str(pdf), os.path.join(td, "p")], 300)
            parts = []
            for png in sorted(glob.glob(os.path.join(td, "p*.png"))):
                r = subprocess.run(["tesseract", png, "-", "-l", "eng", "--psm", "3"], capture_output=True, text=True,
                                   timeout=300, env=dict(os.environ, OMP_THREAD_LIMIT="1"))
                parts.append(r.stdout)
            txt = "\n".join(parts)
    c.write_text(txt, encoding="utf-8")
    return txt


def candidates():
    for t in TYPES:
        for where in ("", "hold/"):
            for js in sorted(glob.glob(str(ROOT / "out" / t / where / "*.json"))):
                js = Path(js)
                if js.name.endswith((".tmp", ".lock")) or Path(str(js) + ".uploaded").exists():
                    continue
                try:
                    sc = json.load(open(js, encoding="utf-8"))
                except Exception:
                    continue
                if (sc.get("fields") or {}).get("checked"):
                    continue
                pdf = js.parent / str(sc.get("file") or "")
                if pdf.is_file():
                    yield t, js, pdf, sc


def evidence(text, rx, width=70):
    m = rx.search(text or "")
    if not m:
        return None
    s = (text[max(0, m.start() - width):m.end() + width]).replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


SUM_PROMPT = """You summarise one scanned business document (page 1 shown, OCR text of the first pages given as a hint).
Return JSON: {"doc_type": "what it is, 2-5 words (e.g. 'Rental invoice', 'Tax clearance letter', 'School newsletter')",
 "from": "the organisation or person it is from", "date": "the document's date as YYYY-MM-DD or null",
 "order_number": "an order / reference number on it or null", "job_number": "an MPS job number (e.g. 32577E) or null",
 "summary": "one or two plain sentences: what the document is and what it says or asks for"}
Never invent values; use null when unsure."""


def summarise(pdf, text, cfg):
    with tempfile.TemporaryDirectory() as td:
        run(["pdftoppm", "-png", "-scale-to", "1400", "-f", "1", "-l", "1", "-singlefile", str(pdf), os.path.join(td, "p")], 300)
        png = os.path.join(td, "p.png")
        body = {"model": cfg["model"], "stream": False, "format": "json", "keep_alive": "15m",
                "options": {"temperature": 0, "num_ctx": cfg["num_ctx"], "num_predict": 600},
                "messages": [{"role": "system", "content": SUM_PROMPT},
                             {"role": "user", "content": "Summarise this document.\n\nOCR text (hint):\n" + (text or "")[:3000],
                              "images": [base64.b64encode(open(png, "rb").read()).decode()]}]}
        raw = model_call.chat_content(cfg["ollama_url"], body, 600)
    m = re.search(r"\{.*\}", raw, re.S)
    d = json.loads(m.group(0) if m else raw)
    out = {k: (str(d.get(k)).strip() if d.get(k) not in (None, "", "null") else None)
           for k in ("doc_type", "from", "date", "order_number", "job_number", "summary")}
    if out["date"] and not re.fullmatch(r"\d{4}-\d\d-\d\d", out["date"]):
        out["date"] = None
    return out


def scan(a):
    pcfg = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))
    cfg = {"model": a.model, "ollama_url": pcfg.get("ollama_url", "http://127.0.0.1:11434"), "num_ctx": int(pcfg.get("num_ctx", 49152))}
    rs = rules()
    cands = list(candidates())
    print(f"{len(cands)} candidate documents", flush=True)
    t0 = time.time()

    def one(c):
        t, js, pdf, sc = c
        try:
            txt = ocr_text(pdf)
        except Exception as e:
            txt = ""
            print("OCR failed:", pdf.name, e, flush=True)
        own = json.dumps(sc.get("fields") or {}, ensure_ascii=False)
        hit = None
        for r in rs:
            ev = evidence(txt, r["rx"]) or evidence(own, r["rx"])
            if ev:
                hit = (r, ev)
                break
        return c, hit

    matches, done = [], 0
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for c, hit in ex.map(one, cands):
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(cands)} read, {len(matches)} matches, {time.time() - t0:.0f}s", flush=True)
            if hit:
                matches.append((c, hit))
    print(f"{len(matches)} matches in {time.time() - t0:.0f}s", flush=True)

    rows = []
    for (t, js, pdf, sc), (r, ev) in matches:
        s = {}
        if not a.no_summary:
            try:
                s = summarise(pdf, ocr_text(pdf), cfg)
            except Exception as e:
                s = {"summary": None}
                print("summary failed:", pdf.name, e, flush=True)
        rel = os.path.relpath(pdf, ROOT / "reports")
        rows.append({"Move": "Y", "Category": r["category"], "Rule": r["name"], "Matched": ev[:250],
                     "Queue": t + (" (hold)" if js.parent.name == "hold" else ""), "File": pdf.name, "link": rel,
                     "Summary": s.get("summary"), "Doc type": s.get("doc_type"), "From": s.get("from"),
                     "Date": s.get("date") or (sc.get("fields") or {}).get("date") or (sc.get("fields") or {}).get("invoice_date"),
                     "Order no": s.get("order_number"), "Job no": s.get("job_number"),
                     "Original file": sc.get("source_file"), "Sidecar": str(js.relative_to(ROOT))})
    rows.sort(key=lambda x: (x["Category"], x["Queue"], x["File"]))
    write_sheet(rows, a)


def write_sheet(rows, a):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Review"
    cols = ["Move", "Category", "Rule", "Matched", "Queue", "File", "Summary", "Doc type", "From", "Date", "Order no",
            "Job no", "Original file", "Sidecar"]
    ws.append(cols)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
    for r in rows:
        ws.append([r.get(k) for k in cols])
        cell = ws.cell(row=ws.max_row, column=cols.index("File") + 1)
        cell.hyperlink = r["link"].replace("/", "\\")
        cell.font = Font(color="0563C1", underline="single")
    widths = {"Move": 7, "Category": 10, "Rule": 8, "Matched": 45, "Queue": 22, "File": 40, "Summary": 70, "Doc type": 20,
              "From": 24, "Date": 11, "Order no": 12, "Job no": 9, "Original file": 30, "Sidecar": 40}
    for i, k in enumerate(cols, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = widths.get(k, 15)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=c.column_letter in ("D", "G"), vertical="top")
    ws.freeze_panes = "B2"
    h = wb.create_sheet("How")
    for line in ["Check each row: open the PDF (File link), read the summary.",
                 "Move = Y: goes to the Other Documents library (MPS Sales, Dino and Ivor only) into a folder per Category.",
                 "Move = N: stays where it is. You can change Category (Saheti / Alenti / SARS or a new name) and the Summary.",
                 "Save and close the sheet, then tell Claude to apply it. Nothing has moved yet."]:
        h.append([line])
    out = ROOT / "reports" / f"Other Documents review {datetime.now():%Y-%m-%d %H%M}.xlsx"
    wb.save(out)
    from collections import Counter
    print(json.dumps({"matches": len(rows), "by_rule": Counter(r["Rule"] for r in rows),
                      "by_queue": Counter(r["Queue"] for r in rows), "sheet": str(out)}, indent=1, default=dict))


def apply(a):
    import openpyxl
    wb = openpyxl.load_workbook(a.sheet, read_only=True, data_only=True)
    ws = wb["Review"]
    it = ws.iter_rows(values_only=True)
    hdr = [str(x or "") for x in next(it)]
    rows = [dict(zip(hdr, r)) for r in it if r and any(r)]
    dest = ROOT / "out" / DEST
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    res = {}
    for r in rows:
        if str(r.get("Move") or "").strip().upper() not in ("Y", "YES"):
            res["left (Move not Y)"] = res.get("left (Move not Y)", 0) + 1
            continue
        js = ROOT / str(r["Sidecar"])
        if not js.exists():
            res["sidecar gone (moved or synced since)"] = res.get("sidecar gone (moved or synced since)", 0) + 1
            continue
        if not a.apply:
            res["would move"] = res.get("would move", 0) + 1
            continue
        try:
            with sidecar_lock.locked(js, "od_sort"):
                sc = json.load(open(js, encoding="utf-8"))
                pdf = js.parent / str(sc.get("file") or "")
                if Path(str(js) + ".uploaded").exists() or (sc.get("fields") or {}).get("checked") or not pdf.is_file():
                    res["skipped (uploaded / checked / no PDF since)"] = res.get("skipped (uploaded / checked / no PDF since)", 0) + 1
                    continue
                name = pdf.name
                n = 2
                while (dest / name).exists() or (dest / (Path(name).stem + ".json")).exists():
                    name = f"{pdf.stem}_{n}{pdf.suffix}"
                    n += 1
                old = sc.get("fields") or {}
                date = str(r.get("Date") or "")[:10] or None
                f = {"summary": r.get("Summary"), "od_doc_type": r.get("Doc type"), "od_from": r.get("From"),
                     "date": date if date and re.fullmatch(r"\d{4}-\d\d-\d\d", date) else None,
                     "order_number": r.get("Order no"), "job_number": r.get("Job no"),
                     "od_category": str(r.get("Category") or "Other").strip() or "Other", "od_rule": r.get("Rule"),
                     "od_evidence": r.get("Matched"), "moved_from": r.get("Queue"),
                     "content_hash": old.get("content_hash") or file_hash(pdf).upper(), "review": None,
                     "library_doc_type": "Other Document"}
                new = {"schema_version": 1, "doc_id": sc.get("doc_id"), "doc_type": "other_doc", "folder": DEST, "file": name,
                       "source_file": sc.get("source_file"), **({"source_folder": sc["source_folder"]} if sc.get("source_folder") else {}),
                       "source_sha256": sc.get("source_sha256"), "source_page": sc.get("source_page"),
                       "processed_at": sc.get("processed_at"), "model": "qwen2.5vl:7b (summary)", "extractor": "od_sort",
                       "needs_review": False, "flags": [], "fields": f, "previous": {"folder": sc.get("folder"),
                       "sidecar": str(js.relative_to(ROOT)), "doc_type": sc.get("doc_type"), "fields": old},
                       "edits": (sc.get("edits") or []) + [{"at": stamp, "by": "od_sort apply (Other Documents)",
                                                            "changes": [{"field": "folder", "old": sc.get("folder"), "new": DEST}]}]}
                shutil.move(str(pdf), str(dest / name))                       # PDF first, JSON last
                tmp = dest / (Path(name).stem + ".json.tmp")
                tmp.write_text(json.dumps(new, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                os.replace(tmp, dest / (Path(name).stem + ".json"))
                for m in (".attempts",):
                    if Path(str(js) + m).exists():
                        os.remove(str(js) + m)
                os.remove(js)
                res["moved"] = res.get("moved", 0) + 1
        except sidecar_lock.Locked:
            res["locked (try again)"] = res.get("locked (try again)", 0) + 1
    print(json.dumps({"applied": a.apply, **res}, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("scan")
    s.add_argument("--jobs", type=int, default=4)
    s.add_argument("--model", default="qwen2.5vl:7b")
    s.add_argument("--no-summary", action="store_true")
    p = sp.add_parser("apply")
    p.add_argument("--sheet", required=True)
    p.add_argument("--apply", action="store_true", help="without it: dry run (counts only)")
    a = ap.parse_args()
    scan(a) if a.cmd == "scan" else apply(a)

#!/usr/bin/env python3
"""Document sorter (Dino 27 Oct: one in-queue, a sorting reader that classifies every document and sends it to its
specialised queue). STEP 1 = SHADOW: classify documents the pipeline has already read and compare the verdict with
what they turned out to be; nothing is routed, nothing in the pipeline changes.

classify(pdf, source_name, source_folder) -> {"type", "how", "confidence", "rules", "vlm", ...}
  1. rules on the first page (CPU): delivery-note barcode (zbarimg), then the page text (text layer, else Tesseract):
     tax invoice / pro forma / quotation / statement / credit note / proof of payment / test certificate / job transfer /
     workshop sheet / MPS or Polymer order form. A rule verdict is "sure" or "likely".
  2. the vision model (the readers' own qwen2.5vl, same context size so Ollama does not reload) on page 1 at 896 px,
     asked for one of the types below - only when the rules are not sure (in shadow mode: always, to measure it).
  3. the file name and scan folder are priors (tie-breakers), recorded separately so their effect can be measured.
Types = the queues: purchase_order (incl. IGT), polymer_order, proof_of_payment, supplier_invoice, pro_forma, quotation,
delivery_note (MPS's own, barcode), supplier_delivery_note, job_transfer, workshop_sheet, test_cert, statement, other.

    sorter.py shadow [--day YYYY-MM-DD] [--sample-per-type N] [--max N]     (timer mps-sorter-shadow, 21:00)
    sorter.py classify FILE.pdf                                             (try one file)
Writes work/sorter/shadow.jsonl (every verdict, cumulative) and reports/sorter shadow <day>.csv/.json.
"""
import argparse, base64, collections, csv, glob, json, os, random, re, subprocess, sys, tempfile, time, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path("/srv/ocr/scans/pipeline")
CFG = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))
OLLAMA = CFG.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
MODEL, NUM_CTX = "qwen2.5vl:7b", 49152          # the readers' model and context: no reload between them
WORK = ROOT / "work" / "sorter"
TYPES = {
    "purchase_order": "an MPS / Kusasa purchase order form or internal (inter-group) transfer form, usually handwritten, with an order number like A12345",
    "polymer_order": "a Polymer Lining Systems purchase order (PL order number)",
    "proof_of_payment": "a bank payment confirmation / proof of payment / remittance (beneficiary, amount paid, bank reference)",
    "supplier_invoice": "a supplier's tax invoice addressed to MPS (invoice number, VAT, totals)",
    "pro_forma": "a pro forma invoice",
    "quotation": "a supplier quotation / quote",
    "delivery_note": "an MPS delivery note (MPS letterhead, a barcode with an IN number, delivered to a customer)",
    "supplier_delivery_note": "a supplier's delivery note / waybill for goods delivered to MPS",
    "job_transfer": "an MPS job transfer note (items moved between jobs, JT number)",
    "workshop_sheet": "a workshop / fabrication drawing sheet (title block, IFC stamp, item marks, dimensions)",
    "test_cert": "a mill test certificate / inspection certificate (heat numbers, chemistry, mechanical tests, EN 10204)",
    "statement": "a supplier statement of account",
    "other": "anything else (letters, enquiries, contracts, drawings without a title block, blank pages)",
}


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def page_text(pdf, td):
    """First page's text: the text layer, else Tesseract at 200 dpi."""
    try:
        t = run(["pdftotext", "-layout", "-f", "1", "-l", "1", str(pdf), "-"], 60).stdout.decode("utf8", "ignore")
        if len(re.sub(r"\s", "", t)) > 80:
            return t, "text layer"
    except Exception:
        pass
    try:
        run(["pdftoppm", "-r", "200", "-gray", "-f", "1", "-l", "1", "-png", "-singlefile", str(pdf), f"{td}/t"], 120)
        t = run(["tesseract", f"{td}/t.png", "-", "--psm", "6"], 180).stdout.decode("utf8", "ignore")
        return t, "tesseract"
    except Exception:
        return "", "no text"


def barcodes(pdf, td):
    try:
        run(["pdftoppm", "-r", "150", "-gray", "-f", "1", "-l", "1", "-png", "-singlefile", str(pdf), f"{td}/b"], 120)
        return run(["zbarimg", "-q", "--raw", f"{td}/b.png"], 60).stdout.decode("utf8", "ignore").split()
    except Exception:
        return []


def rules(text, codes):
    """(type, strength 'sure'/'likely', why) or (None, None, why)."""
    t = re.sub(r"\s+", " ", text or "").upper()
    has = lambda p: re.search(p, t) is not None
    if any(re.fullmatch(r"IN\d{5,7}", c.upper()) for c in codes):
        return "delivery_note", "sure", "delivery-note barcode"
    if has(r"EN\s*10204|INSPECTION CERTIFICATE|MILL TEST|MATERIAL TEST CERT|TEST CERTIFICATE") and has(r"HEAT|CAST"):
        return "test_cert", "sure", "certificate wording + heat"
    if has(r"PRO\s*-?\s*FORMA"):
        return "pro_forma", "sure", "pro forma"
    if has(r"CREDIT\s*NOTE"):
        return "other", "likely", "credit note"
    if has(r"TAX\s*INVOICE") and not has(r"QUOTATION"):
        return "supplier_invoice", "sure", "tax invoice"
    if has(r"PROOF OF PAYMENT|PAYMENT CONFIRMATION|PAYMENT NOTIFICATION|NOTIFICATION OF PAYMENT|REMITTANCE ADVICE") or \
            (has(r"BENEFICIARY") and has(r"AMOUNT") and has(r"BANK")):
        return "proof_of_payment", "sure", "payment confirmation wording"
    if has(r"STATEMENT( OF ACCOUNT)?\b") and has(r"BALANCE|AGEING|CURRENT\s+30"):
        return "statement", "likely", "statement wording"
    if has(r"\bQUOTATION\b|\bQUOTE (NO|NUMBER|REF)"):
        return "quotation", "likely", "quotation wording"
    if has(r"JOB\s*TRANSFER"):
        return "job_transfer", "sure", "job transfer wording"
    if has(r"POLYMER LINING") and has(r"ORDER"):
        return "polymer_order", "likely", "Polymer order wording"
    if has(r"MINING PRESSURE") and has(r"DELIVERY NOTE") and has(r"\bIN\s?\d{5,7}"):
        return "delivery_note", "likely", "MPS delivery note wording, no barcode read"
    if has(r"DELIVERY NOTE|WAYBILL|GOODS RECEIVED|DELIVERY ADVICE") and not has(r"MINING PRESSURE SYSTEMS.{0,40}DELIVERY NOTE"):
        return "supplier_delivery_note", "likely", "delivery note wording"
    if has(r"PURCHASE ORDER|OFFICIAL ORDER|ORDER NO") and has(r"MINING PRESSURE|KUSASA"):
        return "purchase_order", "likely", "order form wording"
    if has(r"\bIFC\b|ISSUED FOR CONSTRUCTION|DRAWN BY|DWG NO|DRAWING NO"):
        return "workshop_sheet", "likely", "drawing title block wording"
    if has(r"\bINVOICE\b") and has(r"\bVAT\b"):
        return "supplier_invoice", "likely", "invoice + VAT"
    return None, None, "no rule"


def prior(name, folder):
    """A hint from the file name / scan folder: (type, why) or (None, '')."""
    s = f"{folder or ''} {name or ''}".upper()
    for pat, ty in ((r"PROOF OF PAYMENT", "proof_of_payment"), (r"PRO\s*FORMA|PROFORMA", "pro_forma"), (r"\bQUOT|\bQUO\d", "quotation"),
                    (r"\bJT\s*\d|JOB TRANSFER", "job_transfer"), (r"TEST ?CERT|\bMTC\b", "test_cert"), (r"WORKSHOP|\bWS\b", "workshop_sheet"),
                    (r"\bINV\d|INVOICE", "supplier_invoice"), (r"DELIVERY NOTE|\bDN\b|\bIN\d{6}\b", "delivery_note"),
                    (r"POLYMER", "polymer_order"), (r"ORDERS-MPS|\bA\d{5}\b", "purchase_order")):
        if re.search(pat, s):
            return ty, f"name/folder '{pat}'"
    return None, ""


_WAIT = {"left": 7200}


def other_model_loaded():
    """The name of another model Ollama holds (the test-certificate reader's qwen3-vl), or None. Asking for qwen2.5vl
    while it is loaded would make Ollama swap models back and forth with the worker."""
    try:
        ms = json.load(urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=10)).get("models") or []
        return next((m["name"] for m in ms if m.get("name") != MODEL), None)
    except Exception:
        return None


def vlm(pdf, td):
    """The vision model's verdict on page 1: (type, confidence 0-1, seconds) or (None, 0, secs)."""
    while _WAIT["left"] > 0 and other_model_loaded():
        time.sleep(60)
        _WAIT["left"] -= 60
    if _WAIT["left"] <= 0:
        return None, 0.0, 0.0                       # waited 2 h in all for the other model: rules only from here
    t0 = time.time()
    try:
        run(["pdftoppm", "-png", "-scale-to", "896", "-f", "1", "-l", "1", "-singlefile", str(pdf), f"{td}/v"], 120)
        img = base64.b64encode(open(f"{td}/v.png", "rb").read()).decode()
        lst = "\n".join(f"- {k}: {v}" for k, v in TYPES.items())
        prompt = ("What kind of business document is this page? It was scanned at Mining Pressure Systems (MPS), a South "
                  "African pipe and pressure-equipment company. Choose exactly one type:\n" + lst +
                  '\nAnswer only JSON: {"type": "<one of the types>", "confidence": <0 to 1>}')
        body = json.dumps({"model": MODEL, "prompt": prompt, "images": [img], "stream": False, "format": "json",
                           "keep_alive": "30m", "options": {"num_ctx": NUM_CTX, "temperature": 0}}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(f"{OLLAMA}/api/generate", body, {"Content-Type": "application/json"}), timeout=300))
        a = json.loads(r.get("response") or "{}")
        ty = str(a.get("type") or "").strip().lower()
        return (ty if ty in TYPES else None), float(a.get("confidence") or 0), round(time.time() - t0, 1)
    except Exception:
        return None, 0.0, round(time.time() - t0, 1)


def classify(pdf, name=None, folder=None, always_vlm=False):
    with tempfile.TemporaryDirectory() as td:
        text, how_text = page_text(pdf, td)
        codes = barcodes(pdf, td)
        r_ty, r_str, r_why = rules(text, codes)
        p_ty, p_why = prior(name or Path(pdf).name, folder)
        v_ty, v_conf, v_secs = (None, 0.0, 0.0)
        if always_vlm or r_str != "sure":
            v_ty, v_conf, v_secs = vlm(pdf, td)
    # decision: a sure rule wins; else the model when confident or when the rule / prior agrees with it; else the rule;
    # else the prior; else the model's guess; else other
    if r_str == "sure":
        final, how = r_ty, "rule"
    elif v_ty and (v_conf >= 0.8 or v_ty in (r_ty, p_ty)):
        final, how = v_ty, "model" + (" + rule" if v_ty == r_ty else " + name" if v_ty == p_ty else "")
    elif r_ty:
        final, how = r_ty, "rule (likely)"
    elif p_ty:
        final, how = p_ty, "name/folder"
    elif v_ty:
        final, how = v_ty, "model (unsure)"
    else:
        final, how = "other", "nothing"
    return {"type": final, "how": how, "rules": r_ty, "rules_strength": r_str, "rules_why": r_why, "text": how_text,
            "barcodes": codes[:3], "prior": p_ty, "vlm": v_ty, "vlm_conf": v_conf, "vlm_secs": v_secs}


def actual(sc, folder):
    """What the pipeline (and Dino's checking / retyping) says the document is, in sorter types."""
    dt, f = sc.get("doc_type"), sc.get("fields") or {}
    if folder == "PolymerOrders" or f.get("form") == "polymer_order":
        return "polymer_order"
    if dt == "po":
        lib, pdt = f.get("library_doc_type") or "", f.get("po_doc_type") or ""
        m = {"Purchase Order": "purchase_order", "IGT": "purchase_order", "Cancelled Order": "purchase_order",
             "Proof of Payment": "proof_of_payment", "Pro Forma": "pro_forma", "Quotation": "quotation"}
        for k, v in m.items():
            if lib.startswith(k):
                return v
        return {"invoice": "supplier_invoice", "delivery_note": "supplier_delivery_note", "job_transfer": "job_transfer",
                "quotation": "quotation", "proforma_invoice": "pro_forma", "purchase_order": "purchase_order",
                "internal_transfer": "purchase_order", "proof_of_payment": "proof_of_payment"}.get(pdt, "other")
    if dt == "delivery_note":
        return "other" if "not_a_delivery_note" in (sc.get("flags") or []) else "delivery_note"
    return {"supplier_invoice": "supplier_invoice", "pop": "proof_of_payment", "jt": "job_transfer", "ws": "workshop_sheet",
            "test_cert": "test_cert"}.get(dt, "other")


def sidecars():
    for pat in ("out/*/*.json", "out/*/hold/*.json", "synced/*/*/*.json"):
        for p in glob.glob(str(ROOT / pat)):
            folder = Path(p).relative_to(ROOT).parts[1]
            yield Path(p), folder


def shadow(day, per_type, cap):
    WORK.mkdir(parents=True, exist_ok=True)
    log = WORK / "shadow.jsonl"
    done = set()
    if log.exists():
        for l in open(log, encoding="utf-8"):
            try:
                done.add(json.loads(l)["key"])
            except Exception:
                pass
    todays, pool = [], collections.defaultdict(list)
    for p, folder in sidecars():
        try:
            sc = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        pdf = p.with_name(str(sc.get("file") or ""))
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
            continue
        key = f"{sc.get('source_sha256')}:{sc.get('source_page')}:{pdf.name}"
        if key in done:
            continue
        item = (p, folder, sc, pdf, key)
        if str(sc.get("processed_at") or "").startswith(day):
            todays.append(item)
        else:
            pool[actual(sc, folder)].append(item)
    random.seed(day)
    picks = todays[:]
    for ty, items in pool.items():                    # every type represented, even if none arrived today
        picks += random.sample(items, min(per_type, len(items)))
    random.shuffle(picks)
    picks = picks[:cap]
    print(f"{len(todays)} read on {day}; {len(picks)} to classify (cap {cap}, up to {per_type} per type from earlier days)", flush=True)
    rows, t0 = [], time.time()
    with open(log, "a", encoding="utf-8") as out:
        for n, (p, folder, sc, pdf, key) in enumerate(picks, 1):
            v = classify(pdf, sc.get("source_file"), sc.get("source_folder"), always_vlm=True)
            act = actual(sc, folder)
            rec = {"key": key, "at": datetime.now().isoformat(timespec="seconds"), "day": day, "queue": folder, "file": pdf.name,
                   "source": sc.get("source_file"), "checked": bool((sc.get("fields") or {}).get("checked")), "actual": act,
                   "today": str(sc.get("processed_at") or "").startswith(day), **v, "right": v["type"] == act}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            rows.append(rec)
            if n % 50 == 0:
                print(f"  {n}/{len(picks)} {time.time() - t0:.0f}s", flush=True)
    summarise(day, rows)


def summarise(day, rows):
    allrows = [json.loads(l) for l in open(WORK / "shadow.jsonl", encoding="utf-8")]
    def acc(rs, k="type"):
        return round(sum(1 for r in rs if r.get(k) == r["actual"]) / len(rs), 3) if rs else None
    per = {}
    for ty in TYPES:
        rs = [r for r in allrows if r["actual"] == ty]
        if rs:
            conf = collections.Counter(r["type"] for r in rs if r["type"] != ty).most_common(3)
            per[ty] = {"n": len(rs), "sorter": acc(rs), "rules_sure_share": round(sum(1 for r in rs if r["rules_strength"] == "sure") / len(rs), 3),
                       "model_alone": acc(rs, "vlm"), "mistaken_for": conf}
    s = {"day": day, "classified_today": len(rows), "today_accuracy": acc(rows), "week_total": len(allrows),
         "week_accuracy": acc(allrows), "model_alone_week": acc(allrows, "vlm"),
         "rules_sure_right": acc([r for r in allrows if r["rules_strength"] == "sure"]),
         "secs_model_avg": round(sum(r["vlm_secs"] for r in allrows) / max(1, len(allrows)), 1), "per_type": per}
    out = ROOT / "reports" / f"sorter shadow {day}"
    with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        cols = ["queue", "file", "source", "checked", "actual", "type", "right", "how", "rules", "rules_strength", "rules_why", "prior", "vlm", "vlm_conf", "text"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["right"], r["actual"])))
    json.dump(s, open(f"{out}.json", "w"), indent=1)
    print(json.dumps(s, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["shadow", "classify"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--sample-per-type", type=int, default=25)
    ap.add_argument("--max", type=int, default=500)
    a = ap.parse_args()
    if a.command == "classify":
        print(json.dumps(classify(Path(a.file), always_vlm=True), indent=1))
    else:
        shadow(a.day, a.sample_per_type, a.max)


if __name__ == "__main__":
    main()

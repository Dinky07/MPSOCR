#!/usr/bin/env python3
"""paddle_text: PaddleOCR text of a scan (mps-upright POST /ocr, RTX 2070 Super) as the GPU reader's hint, and the
check that the invoice number and total the GPU read are printed in it (runner 761, 17 Nov 2026).

Dino, 17 Nov: "can PaddleOCR do the same as the CPU OCR and we use it as a check and verify?"  Measured on scanned
supplier invoices against Sage AP (runners 756-758, O:\\pipeline\\work\\textcmp):
  - the right invoice number is in the text 96 % of the time (Tesseract 82 %), the total 92 % (Tesseract 80 %);
  - "number and total must be in the PaddleOCR text" flags 6 of 101 right reads and 11 of 16 reads whose total is
    not Sage's (the same check on Tesseract text flags 41 of 101 right reads).
So, for a scanned supplier invoice the CPU reader turned down: the GPU model gets this text as its hint instead of
Tesseract's, and the reading gets a check flag when its number or total is not in this text.

read(pdf, max_pages) -> text or None. Lines are rebuilt from PaddleOCR's boxes (boxes whose vertical centres are
close form one line, left to right), like pdftotext -layout. Never raises: anything going wrong gives None and the
worker keeps Tesseract's text, as before.
verify(fields, text) -> list of check texts (empty when both are found).
The container sees /srv/ocr/scans as /scans (read-only), so the PDF is copied to work/paddle/ for the call and
removed after.
Test:  python3 paddle_text.py <pdf> [invoice number] [total]
"""
import json, os, re, shutil, statistics, subprocess, sys, time, urllib.request
from pathlib import Path

URL = os.environ.get("MPS_UPRIGHT_URL", "http://127.0.0.1:8091")
SCANS = "/srv/ocr/scans"
PROBE = Path("/srv/ocr/scans/pipeline/work/paddle")


def alnum(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def amounts(text):
    out = set()
    for m in re.finditer(r"(?<![\d.,])(\d{1,3}(?:[ ,.\u00a0]\d{3})*|\d+)[.,](\d{2})(?![\d])", text or ""):
        try:
            out.add(round(float(re.sub(r"[ ,.\u00a0]", "", m.group(1)) + "." + m.group(2)), 2))
        except ValueError:
            pass
    return out


def _lines(page):
    bs = [(l["box"][0], l["box"][1], l["box"][2], l["box"][3], l["text"]) for l in page.get("lines") or [] if str(l.get("text") or "").strip()]
    if not bs:
        return ""
    h = statistics.median(b[3] - b[1] for b in bs) or 10
    rows = []
    for b in sorted(bs, key=lambda b: (b[1] + b[3]) / 2):
        c = (b[1] + b[3]) / 2
        if rows and abs(c - rows[-1]["c"]) <= 0.5 * h:
            rows[-1]["b"].append(b)
            rows[-1]["c"] = statistics.mean((x[1] + x[3]) / 2 for x in rows[-1]["b"])
        else:
            rows.append({"c": c, "b": [b]})
    return "\n".join("   ".join(x[4] for x in sorted(r["b"], key=lambda x: x[0])) for r in rows)


def _npages(pdf):
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"Pages:\s+(\d+)", out)
        return int(m.group(1)) if m else 1
    except Exception:
        return 1


def read(pdf, max_pages=4, timeout=300, log=None):
    tmp = None
    try:
        p = os.path.realpath(str(pdf))
        if not p.startswith(SCANS + "/"):
            PROBE.mkdir(parents=True, exist_ok=True)
            tmp = PROBE / f"{os.getpid()}-{time.time_ns()}.pdf"
            shutil.copyfile(p, tmp)
            os.chmod(tmp, 0o644)
            p = str(tmp)
        body = {"pdf": "/scans/" + os.path.relpath(p, SCANS), "pages": list(range(1, min(_npages(p), int(max_pages)) + 1))}
        req = urllib.request.Request(URL + "/ocr", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            pages = json.load(r)["pages"]
        text = "\n".join(_lines(pg) for pg in pages).strip()
        return text if len(re.sub(r"\s", "", text)) > 40 else None
    except Exception as e:
        if log:
            log.warning("PaddleOCR text not read (%s): Tesseract's text is used", str(e)[:160])
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def verify(fields, text):
    out = []
    num = alnum(fields.get("invoice_number"))
    if num and num not in alnum(text):
        out.append(f"invoice number {fields.get('invoice_number')} is not in the PaddleOCR text of the scan")
    tot = fields.get("total")
    try:
        tot = round(float(tot), 2) if tot not in (None, "") else None
    except (TypeError, ValueError):
        tot = None
    if tot is not None and tot not in amounts(text):
        out.append(f"total {tot:,.2f} is not in the PaddleOCR text of the scan")
    return out


if __name__ == "__main__":
    t0 = time.time()
    t = read(sys.argv[1])
    print(f"{time.time() - t0:.1f} s, {len(t or '')} characters")
    print((t or "")[:3000])
    if len(sys.argv) > 3:
        print("check:", verify({"invoice_number": sys.argv[2], "total": sys.argv[3]}, t or "") or "both found")

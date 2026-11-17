#!/usr/bin/env python3
"""Host side of the mps-upright accuracy test: pick real saved pages per document type and ask Tesseract which way up
they are (the check the readers use today). Read-only on the scans; writes work/upright/eval/sample.json."""
import concurrent.futures as cf, json, os, random, re, subprocess, sys, tempfile
R = "/srv/ocr/scans/pipeline"
OUT = R + "/work/upright/eval/sample.json"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 36
random.seed(17)
TYPES = ["JobTransfers", "RoninJobTransfers", "PurchaseOrders", "PolymerOrders", "WorkshopSheets", "DeliveryNotes", "TestCerts",
         "ReleaseNotes", "CustomerOrders", "SupplierInvoices", "SupplierInvoicesArchive", "ProofOfPayments", "OtherDocuments", "RoninOrders", "SalesQuotes"]

def pdfs(t):
    got = []
    for base in ("out", "synced"):
        for root, _, fs in os.walk(os.path.join(R, base, t)):
            got += [os.path.join(root, f) for f in fs if f.lower().endswith(".pdf")]
    return got

def pages(p):
    try:
        o = subprocess.run(["pdfinfo", p], capture_output=True, text=True, timeout=30).stdout
        return int(re.search(r"Pages:\s+(\d+)", o).group(1))
    except Exception:
        return 0

def osd(job):
    t, p, pg = job
    r = {"type": t, "pdf": "/scans/" + os.path.relpath(p, "/srv/ocr/scans"), "page": pg, "osd_rotate": None, "osd_conf": None}
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["pdftoppm", "-r", "150", "-f", str(pg), "-l", str(pg), "-png", p, td + "/p"], check=True, capture_output=True, timeout=60)
            png = td + "/" + os.listdir(td)[0]
            o = subprocess.run(["tesseract", png, "-", "--psm", "0"], capture_output=True, text=True, timeout=90, env=dict(os.environ, OMP_THREAD_LIMIT="1")).stdout
            a, c = re.search(r"Rotate:\s*(\d+)", o), re.search(r"Orientation confidence:\s*([\d.]+)", o)
            if a and c:
                r["osd_rotate"], r["osd_conf"] = int(a.group(1)), float(c.group(1))
    except Exception as e:
        r["osd_error"] = repr(e)[:100]
    return r

jobs = []
for t in TYPES:
    ps = pdfs(t)
    for p in random.sample(ps, min(N, len(ps))):
        n = pages(p)
        if n < 1:
            continue
        jobs.append((t, p, 1))
        if n > 1:
            jobs.append((t, p, random.randint(2, min(n, 12))))
print(len(jobs), "pages picked", flush=True)
with cf.ThreadPoolExecutor(4) as ex:
    rows = list(ex.map(osd, jobs))
json.dump(rows, open(OUT + ".tmp", "w"))
os.replace(OUT + ".tmp", OUT)
print("done", len(rows), flush=True)

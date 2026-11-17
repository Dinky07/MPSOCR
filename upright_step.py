#!/usr/bin/env python3
"""upright_step: every scan is read and saved the right way up (runner 745, 17 Nov 2026).

Dino, 17 Nov: "at a minimum I would like the 2070 Super to make all the scans saved right side up."

The worker calls upright_copy() on the file it has just taken from the queue, before any reader sees it. The file in
work/ is never changed (its SHA-256 stays the scan's, for the duplicate check and the done/ archive). When pages need
turning, a copy with those pages turned is written to the worker's temp folder and the readers get that copy, so what
they read and every part they save is upright. Pages are turned by the PDF page rotation only: no re-encoding, the
scan's pixels are untouched.

Which way up a page is comes from the mps-upright container on the RTX 2070 Super (PaddleOCR PP-LCNet_x1_0_doc_ori,
each page looked at in four directions). The rule that gave no wrong turn on the 586-page test of 17 Nov:
  - turn a page when all four looks agree and a sure Tesseract check does not say otherwise;
  - three of four looks are enough when a sure Tesseract check agrees;
  - anything else that looks turned is left as scanned and written to the ledger as "flag".
A turned copy is asked about once more; if a turned page does not then stand upright the copy is thrown away.

Anything going wrong here (container down, odd PDF, missing tool) leaves the file exactly as it was: the worker reads
the scan as it always did. Switch off with "upright": false in config.json (whole pipeline or one doc type).

Ledger: work/upright/turned.csv (one row per page turned or flagged).
Test from a shell:  python3 upright_step.py <out folder> <pdf> [<pdf> ...]
"""
import csv, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

URL = os.environ.get("MPS_UPRIGHT_URL", "http://127.0.0.1:8091")
SCANS = "/srv/ocr/scans"                  # the container sees this folder as /scans (read-only)
LEDGER = Path("/srv/ocr/scans/pipeline/work/upright/turned.csv")
PROBE = Path("/srv/ocr/scans/pipeline/work/upright/probe")
OSD_SURE = 5.0                            # Tesseract's orientation confidence from which it counts as sure
BY = "mps-upright PP-LCNet_x1_0_doc_ori (4 looks) + Tesseract check"


def _ask(pdf, pages=None, timeout=240):
    p = os.path.realpath(str(pdf))
    if not p.startswith(SCANS + "/"):
        raise ValueError("not under " + SCANS)
    body = {"pdf": "/scans/" + os.path.relpath(p, SCANS)}
    if pages:
        body["pages"] = list(pages)
    req = urllib.request.Request(URL + "/orient", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["pages"]


def _osd(pdf, page):
    """Tesseract's orientation check on one page as a viewer shows it -> (degrees clockwise to upright, confidence)."""
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["pdftoppm", "-r", "150", "-f", str(page), "-l", str(page), "-png", str(pdf), td + "/p"],
                           check=True, capture_output=True, timeout=90)
            png = os.path.join(td, os.listdir(td)[0])
            out = subprocess.run(["tesseract", png, "-", "--psm", "0"], capture_output=True, text=True, timeout=120,
                                 env=dict(os.environ, OMP_THREAD_LIMIT="1")).stdout
        rot, conf = re.search(r"Rotate:\s*(\d+)", out), re.search(r"Orientation confidence:\s*([\d.]+)", out)
        if rot and conf:
            return int(rot.group(1)), float(conf.group(1))
    except Exception:
        pass
    return None, None


def decide(p, osd):
    """One page's answer from the service + Tesseract's check -> "turn" | "flag"."""
    rot, conf = osd
    sure = conf is not None and conf >= OSD_SURE
    if p["votes"] == 4 and not (sure and rot != p["turn"]):
        return "turn"
    if p["votes"] == 3 and sure and rot == p["turn"]:
        return "turn"
    return "flag"


def _turn(src, dst, turns):
    import pikepdf
    with pikepdf.open(str(src)) as pdf:
        n = len(pdf.pages)
        for pno, deg in turns.items():
            pdf.pages[pno - 1].rotate(int(deg), relative=True)
        pdf.save(str(dst))
    with pikepdf.open(str(dst)) as chk:
        if len(chk.pages) != n:
            raise RuntimeError("page count changed")


def _ledger(rows):
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        new = not LEDGER.exists()
        with open(LEDGER, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["at", "queue", "file", "page", "action", "turn", "looks_agreeing", "labels", "tesseract_turn", "tesseract_conf", "note"])
            w.writerows(rows)
    except Exception:
        pass


def upright_copy(work, td, folder, name=None, log=None):
    """-> (path the readers should use, info for the sidecar or None). Never raises; never changes `work`."""
    work = Path(work)
    name = name or work.name
    try:
        if work.suffix.lower() != ".pdf":
            return work, None
        pages = _ask(work)
        cand = [p for p in pages if p.get("turn")]
        if not cand:
            return work, None
        turns, flagged, rows, at = {}, {}, [], time.strftime("%Y-%m-%dT%H:%M:%S")
        for p in cand:
            osd = _osd(work, p["page"])
            act = decide(p, osd)
            if act == "turn":
                turns[p["page"]] = p["turn"]
            else:
                flagged[p["page"]] = p["turn"]
            rows.append([at, folder, name, p["page"], act, p["turn"], p["votes"], " ".join(map(str, p.get("labels", []))), osd[0], osd[1], ""])
        out, info = work, {"by": BY}
        if turns:
            d = Path(td) / "upright"
            d.mkdir(parents=True, exist_ok=True)
            dst = d / work.name                      # same file name: the readers take hints from it
            _turn(work, dst, turns)
            # the copy lives in the worker's temp folder, which the container cannot see: it is checked through a second
            # copy in work/upright/probe, removed straight away
            PROBE.mkdir(parents=True, exist_ok=True)
            probe = PROBE / ("%d-%s" % (os.getpid(), work.name))
            try:
                shutil.copy2(dst, probe)
                still = [p["page"] for p in _ask(probe, sorted(turns)) if p.get("turn") and p.get("votes", 0) >= 3]
            finally:
                try:
                    probe.unlink()
                except OSError:
                    pass
            if still:
                for r in rows:
                    if r[4] == "turn":
                        r[4], r[10] = "flag", "turned copy did not check out (pages %s): left as scanned" % still
                flagged.update(turns)
                turns = {}
            else:
                out = dst
                info["turned"] = {str(k): v for k, v in sorted(turns.items())}
        if flagged:
            info["check_orientation"] = {str(k): v for k, v in sorted(flagged.items())}
        _ledger(rows)
        if log:
            log.info("%s %s: %d page(s) turned upright %s%s", folder, name, len(turns), dict(sorted(turns.items())) if turns else "",
                     ("; left as scanned, check orientation: pages %s" % sorted(flagged)) if flagged else "")
        return out, (info if (turns or flagged) else None)
    except Exception as e:
        if log:
            log.warning("upright step skipped for %s (%s: %s): read as scanned", name, type(e).__name__, str(e)[:160])
        return work, None


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outdir = Path(sys.argv[1]); outdir.mkdir(parents=True, exist_ok=True)
    LEDGER = outdir / "turned-test.csv"
    for f in sys.argv[2:]:
        with tempfile.TemporaryDirectory() as td:
            t0 = time.time()
            got, info = upright_copy(f, td, "test", log=logging.getLogger("test"))
            same = Path(got) == Path(f)
            if not same:
                shutil.copy2(got, outdir / Path(f).name)
            print(json.dumps({"file": os.path.basename(f), "copy": not same, "info": info, "seconds": round(time.time() - t0, 2)}), flush=True)

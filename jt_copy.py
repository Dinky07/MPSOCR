#!/usr/bin/env python3
"""jt_copy: which copy of a carbon job transfer a scan is (17 Nov 2026).

Dino, 17 Nov: "a field to show if a JT is white or yellow copy of the carbon paper Job transfers in SharePoint and
MPSDocs (the white and yellow copies can be told apart by the white or yellow background and a receiver's signature
or stamp on the yellow copy)."  The MPS JT book has four copies (footer: WHITE - MASTER, BLUE - JOB CARD,
YELLOW - PROGRESS, PINK - FIXED COPY); the white copy stays in the job file, the yellow one travels with the item and
is signed by the receiver.

classify(pdf) -> {"jt_copy": "White" | "Yellow" | "Blue" | "Pink" | "Unknown", "jt_copy_how": "..."}
  Paper colour of the page (median of the paper pixels, ink left out). A scan made in greyscale has no colour and
  gives Unknown.
  The receiver's signature was measured too (17 Nov, all 417 saved MPS JTs) and is NOT used: white copies are often
  signed or stamped as received as well, and some yellow copies are not, so the signature does not tell the copies
  apart. receiver_ink() is kept for that measurement only.
CPU only, well under a second a page. Never raises: anything going wrong gives Unknown.
Test:  python3 jt_copy.py <pdf> [<pdf> ...]       (one JSON line per file, with the measurements)
"""
import json, os, re, statistics as st, subprocess, sys, tempfile

from PIL import Image



def _render(pdf, dpi, td, name):
    subprocess.run(["pdftoppm", "-r", str(dpi), "-f", "1", "-l", "1", "-png", str(pdf), os.path.join(td, name)],
                   check=True, capture_output=True, timeout=90)
    return Image.open(os.path.join(td, [f for f in os.listdir(td) if f.startswith(name)][0]))


def paper(im):
    """-> (colour name or None, greyscale scan?, (r, g, b), saturation 0-255, hue 0-255)."""
    im = im.convert("RGB")
    px = list(im.getdata())
    light = [p for p in px if sum(p) > 450]
    gray = all(abs(p[0] - p[1]) < 3 and abs(p[1] - p[2]) < 3 for p in px[::97])
    if len(light) < len(px) * 0.2:
        return None, gray, None, None, None
    rgb = tuple(int(st.median(p[i] for p in light)) for i in range(3))
    h, s, _ = Image.new("RGB", (1, 1), rgb).convert("HSV").getpixel((0, 0))
    if gray:
        return None, True, rgb, s, h
    if s < 18:
        return "White", False, rgb, s, h
    if 25 <= h <= 60:
        return "Yellow", False, rgb, s, h
    if 120 <= h <= 190:
        return "Blue", False, rgb, s, h
    if h >= 220 or h <= 12:
        return "Pink", False, rgb, s, h
    return None, False, rgb, s, h


def receiver_ink(pdf, td):
    """Ink in the box after "RECEIVED BY" at the foot of the form -> (share of dark pixels, box) or (None, why)."""
    im = _render(pdf, 150, td, "r").convert("L")
    W, H = im.size
    tsv = subprocess.run(["tesseract", os.path.join(td, [f for f in os.listdir(td) if f.startswith("r")][0]), "-", "--psm", "11", "tsv"],
                         capture_output=True, text=True, timeout=120, env=dict(os.environ, OMP_THREAD_LIMIT="1")).stdout
    words = []
    for line in tsv.splitlines()[1:]:
        f = line.split("\t")
        if len(f) == 12 and f[11].strip():
            words.append((f[11].strip().upper(), int(f[6]), int(f[7]), int(f[8]), int(f[9])))
    rec = [w for w in words if re.match(r"^RECEI?VED", w[0]) and w[2] > H * 0.6]
    if not rec:
        return None, "no RECEIVED BY found"
    _, x, y, w, h = max(rec, key=lambda w: w[2])          # the lowest one: the sign-off line, not a header
    by = [b for b in words if b[0].startswith("BY") and abs(b[2] - y) < h and b[1] > x]
    x0 = (by[0][1] + by[0][3]) if by else (x + w)
    x0 += int(W * 0.01)
    x1 = min(W, x0 + int(W * 0.35))
    y0, y1 = max(0, y - int(h * 1.6)), min(H, y + int(h * 1.6))
    box = im.crop((x0, y0, x1, y1))
    dark = sum(1 for p in box.getdata() if p < 110)
    return dark / max(1, box.size[0] * box.size[1]), (x0, y0, x1, y1)


def classify(pdf):
    try:
        with tempfile.TemporaryDirectory() as td:
            colour, gray, rgb, s, h = paper(_render(pdf, 30, td, "c"))
            if colour:
                return {"jt_copy": colour, "jt_copy_how": f"paper colour {rgb}"}
            if gray:
                return {"jt_copy": "Unknown", "jt_copy_how": "greyscale scan: the paper colour is not in the scan"}
            return {"jt_copy": "Unknown", "jt_copy_how": f"paper colour unclear {rgb}"}
    except Exception as e:
        return {"jt_copy": "Unknown", "jt_copy_how": f"not checked ({type(e).__name__}: {str(e)[:80]})"}


if __name__ == "__main__":
    for f in sys.argv[1:]:
        r = {"file": os.path.basename(f)}
        r.update(classify(f))
        try:
            with tempfile.TemporaryDirectory() as td:
                r["paper"] = paper(_render(f, 30, td, "c"))[:4]
                r["receiver_ink"] = receiver_ink(f, td)[0]
        except Exception as e:
            r["error"] = str(e)[:120]
        print(json.dumps(r), flush=True)

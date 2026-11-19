#!/usr/bin/env python3
"""pdf_shrink: smaller PDFs in out/ (Dino, 19 Nov 2026: "get file sizes down before saving").

Office scanners (the Toshiba e-STUDIO, Chronoscan imports) save every page as a 300-400 dpi colour JPEG: 1.5-2 MB a
page, so a three-page invoice is 5 MB in SharePoint. The worker reads the scan as it always did; only the copy it
publishes to out/ goes through shrink():

  - each full-page scanned image above `dpi` (default 200) is resampled down to `dpi` and saved as a JPEG at
    `quality` (default 70). Colour stays colour: stamps, ticks, highlighter and sticky notes stay as they were.
  - nothing else changes: page count, page order, page rotation (upright_step turns), text layers (ocrmypdf /
    born-digital), vector drawings and small images (logos, signatures) are left exactly as they are.
  - black-and-white (1-bit CCITT/JBIG2) pages, images with masks or odd colour spaces are left alone: they are
    already small or not safe to re-encode.
  - the result is kept only if it opens, has the same page count and is at least `min_gain` (default 15%) smaller;
    otherwise the original is published unchanged. Any error also publishes the original.

200 dpi is what the readers and the dashboard already work at (config dpi 200 / px 1600), so nothing downstream sees
less than it did. Needs pikepdf and Pillow (both come with ocrmypdf; upright_step already uses pikepdf).

Config (config.json, whole pipeline or one doc type, like "upright"):
  "shrink": {"dpi": 200, "quality": 70, "min_gain": 0.15}     or     "shrink": false

Test from a shell:  python3 pdf_shrink.py <in.pdf> <out.pdf> [dpi] [quality]
"""
import os, shutil, sys
from io import BytesIO

DEFAULTS = {"dpi": 200, "quality": 70, "min_gain": 0.15}

try:
    import pikepdf
    from pikepdf import Name, PdfImage
    from PIL import Image
    AVAILABLE = True
except Exception:                       # a box without pikepdf/Pillow publishes files as they are
    AVAILABLE = False


def _settings(cfg):
    s = dict(DEFAULTS)
    if isinstance(cfg, dict):
        s.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return s


def _page_inches(page):
    """(width, height) of the page in inches, from its MediaBox (CropBox when set)."""
    box = page.obj.get("/CropBox") or page.obj.get("/MediaBox")
    x0, y0, x1, y1 = [float(v) for v in box]
    return abs(x1 - x0) / 72.0, abs(y1 - y0) / 72.0


def _shrink_image(xobj, page_in, s):
    """Re-encode one image XObject in place when it is a big scanned image. Returns bytes saved (estimate) or 0."""
    if xobj.get("/Subtype") != Name.Image or "/SMask" in xobj or "/Mask" in xobj or "/Decode" in xobj:
        return 0
    if int(xobj.get("/BitsPerComponent", 8)) != 8 or xobj.get("/ImageMask", False):
        return 0
    w, h = int(xobj.Width), int(xobj.Height)
    pw, ph = page_in
    if pw <= 0 or ph <= 0 or w * h < 1_000_000:          # logos, signatures, thumbnails: leave them
        return 0
    # The scan fills the page; take the dpi from the side that gives the lower figure (rotation-safe).
    dpi = min(max(w, h) / max(pw, ph), min(w, h) / min(pw, ph))
    try:
        pim = PdfImage(xobj)
        if pim.mode not in ("RGB", "L", "CMYK"):
            return 0
        im = pim.as_pil_image()
    except Exception:
        return 0
    old_len = len(xobj.read_raw_bytes())
    if dpi > s["dpi"] * 1.1:
        f = s["dpi"] / dpi
        im = im.resize((max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS)
    if im.mode == "CMYK":
        im = im.convert("RGB")
    buf = BytesIO()
    im.save(buf, "JPEG", quality=int(s["quality"]), optimize=True)
    data = buf.getvalue()
    if len(data) >= old_len:
        return 0
    xobj.write(data, filter=Name.DCTDecode)
    xobj.Width, xobj.Height = im.width, im.height
    xobj.ColorSpace = Name.DeviceGray if im.mode == "L" else Name.DeviceRGB
    xobj.BitsPerComponent = 8
    for k in ("/DecodeParms", "/Intent"):
        if k in xobj:
            del xobj[k]
    return old_len - len(data)


def shrink(src, dst, cfg=None):
    """Write a smaller copy of src to dst. Returns {"bytes_in", "bytes_out", "dpi", "quality"} when the smaller copy
    was written, None when dst should be a plain copy (not worth it, not possible, or switched off)."""
    if cfg is False or not AVAILABLE or str(src).lower()[-4:] != ".pdf":
        return None
    s = _settings(cfg)
    size_in = os.path.getsize(src)
    try:
        with pikepdf.open(str(src)) as pdf:
            n = len(pdf.pages)
            done, saved = set(), 0
            for page in pdf.pages:
                page_in = _page_inches(page)
                res = page.obj.get("/Resources")
                xobjs = res.get("/XObject") if res is not None else None
                if xobjs is None:
                    continue
                for key in list(xobjs.keys()):
                    x = xobjs[key]
                    og = x.objgen
                    if og != (0, 0) and og in done:      # the same image drawn on several pages: once is enough
                        continue
                    done.add(og)
                    saved += _shrink_image(x, page_in, s)
            if not saved:
                return None
            tmp = str(dst) + ".shrink"
            pdf.save(tmp, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        with pikepdf.open(tmp) as chk:
            ok = len(chk.pages) == n
        size_out = os.path.getsize(tmp)
        if not ok or size_out > size_in * (1 - float(s["min_gain"])):
            os.unlink(tmp)
            return None
        os.replace(tmp, dst)
        return {"bytes_in": size_in, "bytes_out": size_out, "dpi": s["dpi"], "quality": s["quality"]}
    except Exception:
        try:
            os.unlink(str(dst) + ".shrink")
        except OSError:
            pass
        return None


def publish(src, dst, cfg=None, log=None):
    """Copy src to dst the way out/ needs it: a smaller copy when shrink() can make one, else src as it is.
    Written to dst.tmp first and moved into place, so the PC never sees half a file. Returns shrink()'s answer."""
    tmp = dst.with_name(dst.name + ".tmp")
    info = shrink(src, tmp, cfg)
    if info is None:
        shutil.copy2(src, tmp)
    elif log:
        log.info("shrunk %s: %.1f MB -> %.1f MB", dst.name, info["bytes_in"] / 1e6, info["bytes_out"] / 1e6)
    os.replace(tmp, dst)
    return info


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    c = {}
    if len(sys.argv) > 3:
        c["dpi"] = int(sys.argv[3])
    if len(sys.argv) > 4:
        c["quality"] = int(sys.argv[4])
    r = shrink(sys.argv[1], sys.argv[2], c)
    if r is None:
        print("left as it is (already small, nothing to shrink, or not possible)")
    else:
        print(f"{r['bytes_in'] / 1e6:.2f} MB -> {r['bytes_out'] / 1e6:.2f} MB "
              f"({100 * (1 - r['bytes_out'] / r['bytes_in']):.0f}% smaller) at {r['dpi']} dpi, JPEG quality {r['quality']}")

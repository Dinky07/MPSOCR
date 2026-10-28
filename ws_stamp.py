#!/usr/bin/env python3
"""The red "ISSUED FOR CONSTRUCTION" stamp on workshop sheets, found on the CPU (Dino 27 Oct: only sheets with the
stamp are imported; only the date in the stamp matters, not who signed).

find(pdf, page=1) -> [stamp, ...] best first, each {"box": [x0, y0, x1, y1] (fraction of the page), "angle": degrees,
    "red_px": count, "words": [stamp words Tesseract read in the red ink], "text_ok": bool}
crop(pdf, stamp, out_png, page=1, dpi=300) -> writes the stamp area (straightened, red ink kept) for a close read of
    the handwritten date by the vision model.

How: the page at 100 dpi in colour; red/pink ink = R high and clearly above G and B (blue and black pen, pencil and
the grey drawing are left out); ink is gathered on a 10 px grid, joined into blobs; a blob the size of a stamp (also a
partial one at the page edge) is a candidate. Its red ink alone is turned upright (main direction of the ink) and read
with Tesseract: two or more of MINING / PRESSURE / ISSUED / CONSTRUCTION / APPROVED / DATE / SIGN = the stamp text.
A greyscale scan has no red: find() returns [] and the caller falls back to the vision model's own "stamped".
"""
import math, os, re, subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageOps

WORDS = ["MINING", "PRESSURE", "ISSUED", "CONSTRUCTION", "APPROVED", "DATE", "SIGN"]
CELL = 10


def _render(pdf, page, dpi, out):
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-f", str(page), "-l", str(page), "-singlefile", str(pdf), out],
                   capture_output=True, timeout=120, check=True)
    return Image.open(out + ".png").convert("RGB")


def _is_red(p):
    r, g, b = p
    return r > 140 and r - g >= 35 and r - b >= 12


def _blobs(im):
    w, h = im.size
    px = im.load()
    gw, gh = (w + CELL - 1) // CELL, (h + CELL - 1) // CELL
    cnt = [[0] * gw for _ in range(gh)]
    for y in range(0, h, 2):                      # every second pixel is plenty at 100 dpi
        for x in range(0, w, 2):
            if _is_red(px[x, y]):
                cnt[y // CELL][x // CELL] += 1
    on = [[cnt[j][i] >= 2 for i in range(gw)] for j in range(gh)]
    seen = [[False] * gw for _ in range(gh)]
    out = []
    for j in range(gh):
        for i in range(gw):
            if not on[j][i] or seen[j][i]:
                continue
            stack, cells = [(j, i)], []
            seen[j][i] = True
            while stack:
                a, b = stack.pop()
                cells.append((a, b))
                for da in (-2, -1, 0, 1, 2):          # gaps of one cell bridged (faint, broken ink)
                    for db in (-2, -1, 0, 1, 2):
                        c, d = a + da, b + db
                        if 0 <= c < gh and 0 <= d < gw and on[c][d] and not seen[c][d]:
                            seen[c][d] = True
                            stack.append((c, d))
            ys = [c[0] for c in cells]
            xs = [c[1] for c in cells]
            red = sum(cnt[a][b] for a, b in cells)
            bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            out.append({"cells": cells, "bbox": (min(xs) * CELL, min(ys) * CELL, (max(xs) + 1) * CELL, (max(ys) + 1) * CELL),
                        "red_px": red * 4, "span": max(bw, bh)})
    return out, w, h


def _angle(im, bbox):
    """The stamp's rotation (degrees, for PIL's rotate): the angle at which the red ink's rows line up best (the stamp's
    text lines and frame give sharp peaks in the row profile), tried from -90 to +90 in 3-degree steps."""
    x0, y0, x1, y1 = bbox
    m = _red_only(im.crop((max(0, x0 - 10), max(0, y0 - 10), min(im.size[0], x1 + 10), min(im.size[1], y1 + 10))))
    m = ImageOps.invert(m)                          # ink white on black: rotation fills with black
    best, best_a = -1.0, 0.0
    for a in range(-90, 91, 3):
        r = m.rotate(a, expand=True, fillcolor=0)
        w, h = r.size
        data = r.tobytes()
        rows = [sum(data[y * w:(y + 1) * w]) for y in range(h)]
        mean = sum(rows) / max(1, h)
        var = sum((v - mean) ** 2 for v in rows) / max(1, h)
        if var > best:
            best, best_a = var, float(a)
    return best_a


def _red_only(im):
    """Red ink black on white; everything else white."""
    rgb = im.convert("RGB")
    raw = rgb.tobytes()
    return Image.frombytes("L", rgb.size, bytes(0 if _is_red((raw[i], raw[i + 1], raw[i + 2])) else 255 for i in range(0, len(raw), 3)))


def _words(img):
    best = []
    for rot in (0, 180):
        t = subprocess.run(["tesseract", "-", "-", "--psm", "6"], input=_png(img.rotate(rot, expand=True, fillcolor=255)),
                           capture_output=True, timeout=120).stdout.decode("utf8", "ignore").upper()
        hit = [w for w in WORDS if re.search(w[:5], t)]
        if len(hit) > len(best):
            best = hit
    return best


def _png(img):
    import io
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def find(pdf, page=1):
    with tempfile.TemporaryDirectory() as td:
        try:
            im = _render(pdf, page, 100, os.path.join(td, "p"))
        except Exception:
            return []
        blobs, w, h = _blobs(im)
        cands = [b for b in blobs if b["span"] >= 9 and b["red_px"] >= 300]        # a stamp, or a good part of one
        cands.sort(key=lambda b: -b["red_px"])
        out = []
        hi = None
        for b in cands[:4]:
            x0, y0, x1, y1 = b["bbox"]
            ang = _angle(im, b["bbox"])
            if hi is None:
                hi = _render(pdf, page, 300, os.path.join(td, "h"))
            k = 3
            crop = hi.crop((max(0, x0 * k - 30), max(0, y0 * k - 30), min(hi.size[0], x1 * k + 30), min(hi.size[1], y1 * k + 30)))
            red = _red_only(crop).rotate(ang, expand=True, fillcolor=255)
            words = _words(ImageOps.autocontrast(red))
            out.append({"box": [round(x0 / w, 3), round(y0 / h, 3), round(x1 / w, 3), round(y1 / h, 3)], "angle": round(ang, 1),
                        "red_px": b["red_px"], "words": words, "text_ok": len(words) >= 2})
        out.sort(key=lambda s: (not s["text_ok"], -s["red_px"]))
        return out


def crop(pdf, stamp, out_png, page=1, dpi=300):
    """The stamp area at `dpi`, straightened, in colour (the handwritten date may be in any pen colour)."""
    with tempfile.TemporaryDirectory() as td:
        im = _render(pdf, page, dpi, os.path.join(td, "p"))
        W, H = im.size
        x0, y0, x1, y1 = stamp["box"]
        m = 0.04
        c = im.crop((int(max(0, x0 - m) * W), int(max(0, y0 - m) * H), int(min(1, x1 + m) * W), int(min(1, y1 + m) * H)))
        c.rotate(stamp.get("angle") or 0, expand=True, fillcolor=(255, 255, 255)).save(out_png)
    return out_png


VERSION = "ws-stamp-3 (28 Oct)"
WINDOW_DAYS = 31          # Dino 27 Oct: "anything that looks like this stamp and has a date within a month of the WSS date"

# No example dates (v1's "2017-01" + "10" came back as the date of blank stamps); says the DATE space is often blank;
# asks whether the picture is the MPS stamp at all (colour-printed MPS logos, a client's acceptance stamp and red-pen
# notes are red ink too). A/B on 40 crops labelled by eye (230): qwen3-vl 40/40 stamp or not, 11/11 blanks left blank,
# 8/15 dates exact and every wrong one outside the month; qwen2.5vl 6/15 with one wrong date inside the month.
PROMPT = (
    "This picture is cut from a scanned workshop drawing of Mining Pressure Systems (MPS). MPS's quality stamp is a red "
    "rubber stamp with four lines: MINING PRESSURE SYSTEMS / ISSUED FOR CONSTRUCTION / DATE: ...... SIGN: ...... / "
    "Q A APPROVED.\n"
    "1. is_ifc_stamp: true only if that red stamp (or most of it) is in the picture. A printed company logo, another "
    "company's stamp or red handwriting is not it.\n"
    "2. If it is, look only at the DATE space of the stamp. When a date is handwritten there it is usually in two parts: "
    "the year and month written above the DATE line, and the day written on the dotted line after \"DATE:\". Copy "
    "exactly what is handwritten. Very often nothing is written there (only the printed dots): then date_blank is true "
    "and the date fields are null. Never guess or make up a date; ignore the signature and all printed text.\n"
    "Answer only JSON: {\"is_ifc_stamp\": true or false, \"date_blank\": true or false, \"year_month_written\": text or "
    "null, \"day_written\": text or null, \"date_iso\": \"YYYY-MM-DD\" or null}")


# Dino 28 Oct: a black-and-white scan of the stamp is a photocopy - accepted like the red one, marked as a copy.
PROMPT_COPY = PROMPT.replace("MPS's quality stamp is a red rubber stamp", "MPS's quality stamp is a rubber stamp (red ink on the "
                             "original; this is a black-and-white copy, so it shows grey or black)").replace(
    "true only if that red stamp", "true only if that stamp").replace("red handwriting", "handwriting")
STAMP_WORDS = re.compile(r"ISSU|CONSTRUC|APPROV", re.I)


def find_bw(pdf, out_png, page=1):
    """No red ink (greyscale scan or a photocopy): the stamp's printed words found by Tesseract on the page turned
    0/90/180/270 degrees; two or more of ISSUED / CONSTRUCTION / APPROVED close together = the stamp. Writes the stamp
    area (upright, 300 dpi) to out_png and returns {"box", "angle", "words"} or None."""
    with tempfile.TemporaryDirectory() as td:
        try:
            im = _render(pdf, page, 300, os.path.join(td, "p")).convert("L")
        except Exception:
            return None
        for rot in (0, 90, 270, 180):
            r = im.rotate(rot, expand=True, fillcolor=255) if rot else im
            small = r.resize((r.size[0] * 2 // 3, r.size[1] * 2 // 3))        # 200 dpi for Tesseract
            tsv = subprocess.run(["tesseract", "-", "-", "--psm", "11", "tsv"], input=_png(small), capture_output=True,
                                 timeout=180).stdout.decode("utf8", "ignore").splitlines()
            words = []
            for line in tsv[1:]:
                c = line.split("\t")
                if len(c) >= 12 and STAMP_WORDS.search(c[11] or ""):
                    x, y, w, h = (int(v) * 3 // 2 for v in c[6:10])
                    words.append((c[11], x, y, w, h))
            for i, (t, x, y, w, h) in enumerate(words):
                near = [v for v in words if abs(v[1] - x) < r.size[0] * 0.18 and abs(v[2] - y) < r.size[1] * 0.08]
                kinds = {re.match(r"(ISSU|CONSTRUC|APPROV)", v[0].upper()).group(1) for v in near
                         if re.match(r"(ISSU|CONSTRUC|APPROV)", v[0].upper())}
                if len(kinds) >= 2:
                    x0 = min(v[1] for v in near); y0 = min(v[2] for v in near)
                    x1 = max(v[1] + v[3] for v in near); y1 = max(v[2] + v[4] for v in near)
                    pw, ph = (x1 - x0), (y1 - y0)
                    box = (max(0, x0 - pw * 0.35), max(0, y0 - ph * 1.6), min(r.size[0], x1 + pw * 0.35), min(r.size[1], y1 + ph * 1.6))
                    r.crop(tuple(int(v) for v in box)).save(out_png)
                    return {"box": [round(box[0] / r.size[0], 3), round(box[1] / r.size[1], 3), round(box[2] / r.size[0], 3),
                                    round(box[3] / r.size[1], 3)], "angle": rot, "words": sorted(kinds)}
    return None


def read_stamp(png, ollama_url, model, keep_alive="15m", timeout=600, num_ctx=49152, prompt=None):
    """The vision model on a cropped stamp: {is_ifc_stamp, date_blank, year_month_written, day_written, date_iso}."""
    import base64, json, urllib.request
    body = {"model": model, "stream": False, "format": "json", "keep_alive": keep_alive,
            "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": 200},
            "messages": [{"role": "user", "content": prompt or PROMPT, "images": [base64.b64encode(open(png, "rb").read()).decode()]}]}
    req = urllib.request.Request(ollama_url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read())["message"]["content"]
    try:
        return json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {}


def to_iso(a):
    """The date from the two handwritten parts (year-month above the line, day on it), or a full date written in one
    place (year first, or day-month-year). Year-month alone is not a date, and the model's own date_iso is not used (it
    fills in a missing day). O->0, l/I->1."""
    from datetime import date

    def digits(s):
        return re.sub(r"[^0-9]", " ", str(s or "").upper().replace("O", "0").replace("L", "1").replace("I", "1")).split()
    ym, dd = digits(a.get("year_month_written")), digits(a.get("day_written"))
    try:
        if len(ym) >= 3 and len(ym[0]) == 4:
            return date(int(ym[0]), int(ym[1]), int(ym[2])).isoformat()
        if len(ym) >= 3 and len(ym[2]) == 4:
            return date(int(ym[2]), int(ym[1]), int(ym[0])).isoformat()
        if len(ym) >= 2 and len(ym[0]) == 4 and dd:
            return date(int(ym[0]), int(ym[1]), int(dd[0])).isoformat()
    except ValueError:
        pass
    return None


def sheet_date_of(fields, name=""):
    """The WS sheet's own date: the reading (fields.date), else the date in the file name (...-DD-MM-YYYY)."""
    d = str(fields.get("date") or "")[:10]
    if re.match(r"^\d{4}-\d\d-\d\d$", d):
        return d
    m = re.search(r"(?<!\d)(\d{2})-(\d{2})-(\d{4})(?!\d)", str(name))
    return f"{m[3]}-{m[2]}-{m[1]}" if m else None


def check(pdf, fields, how, ollama_url, model, keep_alive="15m", timeout=600, name=""):
    """The import gate for a workshop sheet (Dino 27 Oct). Returns stamp_check = {status, ...}:
      no_stamp     no MPS stamp: not imported (CAD sheets read from their own text never have one)
      check_stamp  no red ink to look at (greyscale scan?) but the page reading saw a stamp; or the close read failed
      check_date   the stamp is there, but its date is blank / unreadable / more than WINDOW_DAYS from the WS date
      clean        the stamp is there and its date is within WINDOW_DAYS of the WS date
    The stamp and its date come only from close reads of the red-ink candidates (the full-page reading copied the WS
    date into blank stamps); up to 3 candidates, the first the model calls the MPS stamp counts."""
    from datetime import date
    out = {"version": VERSION, "model": model}
    if how == "text_layer":
        out.update(status="no_stamp", stamp="none", why="CAD sheet read from its own text: a rubber stamp is never in it")
        return out
    stamps = find(pdf)
    sd = sheet_date_of(fields, name or Path(str(pdf)).name)
    out.update(sheet_date=sd, red_ink=bool(stamps), model_stamped_page=fields.get("stamped") is True, candidates=[])
    hit = None
    for s in stamps[:3]:
        try:
            with tempfile.TemporaryDirectory() as td:
                png = crop(pdf, s, os.path.join(td, "stamp.png"))
                r = read_stamp(png, ollama_url, model, keep_alive, timeout)
        except Exception as e:
            out.update(status="check_stamp", stamp="unknown", why=f"close read failed: {str(e)[:120]}")
            return out
        iso = None if r.get("date_blank") else to_iso(r)
        out["candidates"].append({"box": s["box"], "words": s["words"], "is_ifc_stamp": bool(r.get("is_ifc_stamp")),
                                  "date_blank": r.get("date_blank"), "year_month_written": r.get("year_month_written"),
                                  "day_written": r.get("day_written"), "date": iso})
        if r.get("is_ifc_stamp"):
            hit = out["candidates"][-1]
            break
    if not hit and not stamps:
        # no red ink at all: a black-and-white scan / photocopy of the stamp is accepted and marked as a copy (Dino 28 Oct)
        try:
            with tempfile.TemporaryDirectory() as td:
                png = os.path.join(td, "bw.png")
                bw = find_bw(pdf, png)
                if bw:
                    r = read_stamp(png, ollama_url, model, keep_alive, timeout, prompt=PROMPT_COPY)
                    iso = None if r.get("date_blank") else to_iso(r)
                    out["candidates"].append({"box": bw["box"], "words": bw["words"], "is_ifc_stamp": bool(r.get("is_ifc_stamp")),
                                              "date_blank": r.get("date_blank"), "year_month_written": r.get("year_month_written"),
                                              "day_written": r.get("day_written"), "date": iso, "copy": True})
                    if r.get("is_ifc_stamp"):
                        hit = out["candidates"][-1]
                        out["copy"] = True
        except Exception as e:
            out.update(status="check_stamp", stamp="unknown", why=f"black-and-white close read failed: {str(e)[:120]}")
            return out
    if not hit:
        if not stamps and out["model_stamped_page"]:
            out.update(status="check_stamp", stamp="unknown", why="no red ink found (greyscale scan?), the page reading saw a stamp")
        else:
            out.update(status="no_stamp", stamp="none",
                       why="red ink found but none of it is the MPS stamp" if stamps else "no stamp")
        return out
    out.setdefault("copy", False)
    out.update(stamp="high", box=hit["box"], stamp_date=hit["date"],
               stamp_date_as_written=" / ".join(str(x) for x in (hit["year_month_written"], hit["day_written"]) if x) or None)
    if not hit["date"]:
        out.update(status="check_date", why="stamp date blank" if hit["date_blank"] else "stamp date not readable")
    elif not sd:
        out.update(status="check_date", why="no WS date to compare the stamp date with")
    else:
        gap = abs((date.fromisoformat(hit["date"]) - date.fromisoformat(sd)).days)
        out["gap_days"] = gap
        out.update(status="clean" if gap <= WINDOW_DAYS else "check_date",
                   why=None if gap <= WINDOW_DAYS else f"stamp date {hit['date']} is {gap} days from the WS date {sd}")
    return out


if __name__ == "__main__":
    import json, time
    for f in sys.argv[1:]:
        t = time.time()
        r = find(f)
        print(f, f"{time.time() - t:.1f}s", json.dumps(r, indent=1))

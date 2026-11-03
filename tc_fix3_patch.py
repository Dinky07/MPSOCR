#!/usr/bin/env python3
r"""489 (Dino 3 Nov: "look at all the documents in the hold queue ... see why they returned no heat numbers, and then
make some changes to the reader"). 35 of the 56 held test certs had no heats. Looked at page by page:
  1. a text rule filed real certs as letter-of-credit papers (no model read): the supplier's bank stamps "DOCUMENTARY
     CREDIT NUMBER" on the cert and the OCR'd title was garbled (DOC090720-003, DOC210217-003/-007, DOC010818,
     DOC210518) -> the stamp no longer counts, and cert wording (heat no, yield, tensile, certify, inspect, 10204)
     keeps a page for the model;
  2. certs titled "INSPECTION CERTIFICATE" / "CERTIFICATE OF INSPECTION" with chemistry were answered as
     inspection_only with the header only (MTC 0319, DOC050526-002, DOC270117-010, (A54364), 4101 ...) -> a
     certificate page answered with no heats is asked once more, pointing at the chemistry columns;
  3. sideways pages Tesseract was unsure of were read sideways (1028, 2208, 3953, 4581, DF2741, Sterling), and an
     upright landscape page it called upside down was turned over (DOC230821-004: 41 lines -> 0) -> OSD is taken only
     when sure of a quarter turn or of upright; otherwise each way is OCR'd and the way with most clearly read words wins;
  4. wide landscape tables read at 1600 px (Hengyang Valin x5, DOC250619-005) and a landscape cert printed small on a
     portrait page (3083) -> the page is cropped to its printed area when the margins are wide, and a landscape page
     gets 2400 px on its long side; faint print (DOC070917-002) is contrast-stretched.
The prompt is unchanged.
    tc_fix3_patch.py <tc_reader.py>"""
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
R = []

R.append(('''def rule_page_type(text):
    """Page type from a text layer when it is plain (shipping papers with no cert wording), else None (ask the model)."""
    t = text or ""
    if len(t.strip()) < 80 or _CERT.search(t):
        return None
    for kind, rx in _PAPERS:''', '''# 489: the bank's stamp on a cert ("DOCUMENTARY CREDIT NUMBER: IL...") is not a letter of credit, and cert wording
# survives a garbled title ("MlLL TEST", "lNSPECTlON")
_STAMP = re.compile(r"(?i)DOCUMENTARY\\s*(?:LETTER\\s*OF\\s*)?CREDIT\\s*(?:NUMBER|NO)\\b")
_CERTISH = re.compile(r"(?i)HEAT\\s*N[O0]|YIELD|TENSILE|ELONGATION|CERTIFY|INSPECT|M[I1l]LL\\s*TES|10204")


def rule_page_type(text):
    """Page type from a text layer when it is plain (shipping papers with no cert wording), else None (ask the model)."""
    t = text or ""
    if len(t.strip()) < 80 or _CERT.search(t) or _CHEM.search(t) or _CERTISH.search(t):
        return None
    t = _STAMP.sub(" ", t)
    for kind, rx in _PAPERS:'''))

R.append(('''UPRIGHT_MIN_CONF = 2.0


def upright(png):
    """453: rotate a sideways page image upright in place (Tesseract OSD). Returns the degrees turned, 0 if none."""
    try:
        r = run(["tesseract", png, "stdout", "--psm", "0"], 120)
        out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else str(r.stdout or "")
        rot = re.search(r"Rotate:\\s*(\\d+)", out)
        conf = re.search(r"Orientation confidence:\\s*([\\d.]+)", out)
        deg = int(rot.group(1)) if rot else 0
        if deg not in (90, 180, 270) or not conf or float(conf.group(1)) < UPRIGHT_MIN_CONF:
            return 0
        from PIL import Image
        with Image.open(png) as im:
            im.rotate(-deg, expand=True).save(png)
        return deg
    except Exception:
        return 0
''', '''UPRIGHT_SURE = 5.0          # 489: OSD this sure of a quarter turn (or of upright) is taken as it is
LANDSCAPE_PX = 2400         # 489: long side for a landscape page (wide tables)


def _good_words(png, deg):
    """489: words Tesseract reads with confidence >= 70 (3+ letters) on the page turned by deg."""
    from PIL import Image
    q = png[:-4] + f"_o{deg}.png"
    with Image.open(png) as im:
        g = im.convert("L")
        g.thumbnail((1400, 1400))
        (g.rotate(-deg, expand=True) if deg else g).save(q)
    out = run(["tesseract", q, "stdout", "--psm", "6", "tsv"], 120).stdout.decode("utf-8", "replace")
    n = 0
    for line in out.splitlines()[1:]:
        c = line.split("\\t")
        try:
            if len(c) >= 12 and float(c[10]) >= 70 and re.fullmatch(r"[A-Za-z]{3,}", c[11].strip()):
                n += 1
        except ValueError:
            pass
    return n


def upright(png):
    """453 + 489: turn a sideways or upside-down page image upright in place; returns the degrees turned, 0 if none.
    Tesseract's orientation check (OSD) is taken when it is sure of a quarter turn or of upright. Otherwise (unsure:
    1028, DF2741; or "upside down": DOC230821-004 was an upright landscape page) the page is OCR'd each way and the
    way with clearly more well-read words wins."""
    try:
        r = run(["tesseract", png, "stdout", "--psm", "0"], 120)
        out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else str(r.stdout or "")
        rot = re.search(r"Rotate:\\s*(\\d+)", out)
        conf = re.search(r"Orientation confidence:\\s*([\\d.]+)", out)
        deg = int(rot.group(1)) if rot else 0
        if not (conf and float(conf.group(1)) >= UPRIGHT_SURE and deg in (0, 90, 270)):
            ways = (0, 90, 270, 180) if deg == 180 else (0, 90, 270)
            score = {d: _good_words(png, d) for d in ways}
            best = max(ways, key=lambda d: score[d])
            deg = best if best and score[best] >= max(12, 1.5 * score[0]) else 0
        if not deg:
            return 0
        from PIL import Image
        with Image.open(png) as im:
            im.rotate(-deg, expand=True).save(png)
        return deg
    except Exception:
        return 0


def prepare(png, px):
    """489: the page image the model sees. Cropped to the printed area when the margins are wide (3083: a landscape
    cert printed small on a portrait page), contrast-stretched when the print is faint (DOC070917-002), then px on
    its long side - LANDSCAPE_PX for a landscape page (wide tables: Hengyang Valin, DOC250619-005)."""
    try:
        from PIL import Image, ImageOps
        with Image.open(png) as im0:
            im = im0.convert("RGB")
        g = im.convert("L")
        small = g.copy()
        small.thumbnail((800, 800))
        w, h = small.size
        px_ = small.load()
        rows = [sum(1 for x in range(w) if px_[x, y] < 160) for y in range(h)]
        cols = [sum(1 for y in range(h) if px_[x, y] < 160) for x in range(w)]
        # the printed area: rows / columns with some print in them (a pen stroke across the page, specks: not)
        ys = [y for y, n in enumerate(rows) if n > 0.02 * w]
        xs = [x for x, n in enumerate(cols) if n > 0.02 * h]
        box = (xs[0], ys[0], xs[-1] + 1, ys[-1] + 1) if xs and ys else None
        if box:
            sx, sy = g.width / small.width, g.height / small.height
            mx, my = 0.02 * small.width, 0.02 * small.height
            x0, y0 = max(0, int((box[0] - mx) * sx)), max(0, int((box[1] - my) * sy))
            x1, y1 = min(g.width, int((box[2] + mx) * sx)), min(g.height, int((box[3] + my) * sy))
            if (x1 - x0) * (y1 - y0) < 0.75 * g.width * g.height:
                im, g = im.crop((x0, y0, x1, y1)), g.crop((x0, y0, x1, y1))
        hist, k, dark = g.histogram(), 0, 0
        for v in range(256):
            k += hist[v]
            if k >= 0.01 * g.width * g.height:
                dark = v
                break
        if dark > 110:                                    # even the darkest 1 % is light grey: faint print
            im = ImageOps.autocontrast(im, cutoff=1)
        long_side = max(px, LANDSCAPE_PX) if im.width > 1.2 * im.height else px
        f = long_side / max(im.width, im.height)
        if f < 1:
            im = im.resize((round(im.width * f), round(im.height * f)), Image.LANCZOS)
        im.save(png)
    except Exception:
        pass
    return png
'''))

R.append(('''        if str(path).lower().endswith(".pdf"):
            png = render(path, page, px, os.path.join(tmp, "p.png"))
        else:
            png = shutil.copy(str(path), os.path.join(tmp, "p" + os.path.splitext(str(path))[1]))
        turned = upright(png)                                 # 453: sideways scans read upright
''', '''        if str(path).lower().endswith(".pdf"):              # 489: rendered larger, then cropped / sized by prepare()
            png = render(path, page, int(max(px, LANDSCAPE_PX) * 1.25), os.path.join(tmp, "p.png"))
        else:
            png = shutil.copy(str(path), os.path.join(tmp, "p" + os.path.splitext(str(path))[1]))
        turned = upright(png)                                 # 453: sideways scans read upright
        prepare(png, px)
'''))

R.append(('''REREAD_EMPTY = (''', '''REREAD_CHEM = ('Your answer for this page has no heats. If the page has a chemical composition table, or chemistry '
               'columns (C, Si, Mn, P, S ...) in its item table, it is an mtc - even when it is titled "INSPECTION '
               'CERTIFICATE" or "CERTIFICATE OF INSPECTION": read every item line with its heat number, and every heat '
               'with its chemistry and mechanical results. If the page really has no chemistry, answer as before.')
REREAD_EMPTY = ('''))

R.append(('''                    if _has_content(a2):
                        a, how = a2, how + "+reread (empty answer)"
                except Exception:
                    pass
            pages.append({"page": p, "how": how, "answer": a})''', '''                    if _has_content(a2):
                        a, how = a2, how + "+reread (empty answer)"
                except Exception:
                    pass
            if (a or {}).get("page_type") in CERT_TYPES and not (a or {}).get("heats"):
                # 489: a certificate page with no heats (titled INSPECTION CERTIFICATE, chemistry in the item table)
                try:
                    a3 = read_page(path, p, ollama_url, model, "", family, px, hint, timeout, keep_alive,
                                   earlier_heats=earlier, extra=REREAD_CHEM)
                    if isinstance(a3, dict) and a3.get("heats"):
                        if len(a3.get("items") or []) < len(a.get("items") or []):
                            a3["items"] = a["items"]
                        a, how = a3, how + "+reread (no heats)"
                except Exception:
                    pass
            pages.append({"page": p, "how": how, "answer": a})'''))

for old, new in R:
    n = s.count(old)
    if n != 1:
        sys.exit(f"expected 1 match, found {n}: {old[:80]!r}")
    s = s.replace(old, new)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched", p)

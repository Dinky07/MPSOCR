#!/usr/bin/env python3
"""Release notes (Dino 28 Oct 2026): MPS's own "RELEASE NOTE" / inspection report form, signed off by the customer's
or a third-party inspector, followed in the scan by its supporting papers (job transfers, workshop sheets, test certs,
inspection requests, item lists, photos). Each release note and the pages after it, up to the next release note, are
ONE document.

  1. ocrmypdf turns the pages upright and adds a text layer (dn_reader.make_upright); each page is classed from its
     text: release-note page, a known supporting paper, other, or blank (blank duplex backs are dropped).
  2. Every release-note page is read by the vision model (qwen3-vl:8b-instruct) into a fixed JSON schema: header,
     inspections ticked, items, despatch / NCR, and the sign-off block per party.
  3. Pages are grouped: a release-note page starts a document unless it is page 2+ of the one before ("P2/2", or the
     same release number straight after a "P1/2"). Pages before the first release note become one held document.
  4. Values are checked (release number vs MPS project number vs QCP number; items present; signed off) and the
     document's PDF is written with the blank pages left out.

    python3 rn_reader.py <scan.pdf> [--out DIR]      # test: writes <stem>-NN.pdf + .json per release note, prints a summary
"""
import argparse, base64, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
try:
    import dn_reader                       # make_upright (ocrmypdf), page_texts, words()
except Exception:                          # pragma: no cover
    dn_reader = None

DEFAULTS = {
    "model": "qwen3-vl:8b-instruct",
    "px": 1800,                            # typed form with a dense item table: a little more than the 1600 default
    "ocr": True, "ocr_jobs": 4, "ocr_lang": "eng",
    "blank_ink": 0.003,                    # share of dark pixels below which a page with no text is a blank back
    "timeout": 900, "keep_alive": "15m", "num_ctx": 49152,
    "ollama_url": "http://127.0.0.1:11434",
}


def run(cmd, timeout=300, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} rc={r.returncode}: {(r.stderr or '').strip()[-300:]}")
    return r


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def page_count(pdf):
    m = re.search(r"^Pages:\s*(\d+)", run(["pdfinfo", str(pdf)], 60).stdout, re.M)
    return int(m.group(1)) if m else 1


def _w(*ws):
    if dn_reader:
        return dn_reader.words(*ws)
    return r"\s+".join(r"\s?".join(re.escape(c) for c in w) for w in ws)


# ------------------------------------------------------------------------------------------------ page classes
RN_ANCHORS = [re.compile(x, re.I) for x in (
    _w("RELEASE", "NOTE"), _w("RELEASE", "NUMBER"), _w("PROJECT", "DESCRIPTION"), _w("MPS", "PROJECT"),
    _w("QCP", "NUMBER"), r"INSPECTION\s+TO\s+BE\s+PRE?F?OR?MED", _w("RELEASED", "FOR", "DESPATCH"), r"I?TM?E?MENS\s+RELEASE")]
SUPPORT = [("job_transfer", re.compile(_w("JOB", "TRANSFER"), re.I)),
           ("item_list", re.compile(r"ITEM\s+LIST\s+FOR\s+RELEASE", re.I)),
           ("inspection_request", re.compile(r"REQUEST\s+FOR\s+INSPECTION", re.I)),
           ("test_cert", re.compile(r"MILL\s*TEST|MATERIAL\s*TEST|INSPECTION\s*CERTIFICATE|TEST\s*CERTIFICATE|EN\s*10204|"
                                    r"CHEMICAL\s*COMPOSITION", re.I)),
           ("workshop_sheet", re.compile(r"CORROSION\s+(?:INT|EXT)|COPYRIGHT|MPS\s+DWG|SHEET\s+NO|\bMTO\b", re.I)),
           ("delivery_note", re.compile(_w("DELIVERY", "NOTE"), re.I)),
           ("packing_list", re.compile(_w("PACKING", "LIST"), re.I))]
KIND_LABEL = {"release_note": "Release note", "job_transfer": "Job transfer", "item_list": "Item list for release",
              "inspection_request": "Request for inspection", "test_cert": "Test certificate", "workshop_sheet": "Workshop sheet",
              "delivery_note": "Delivery note", "packing_list": "Packing list", "other": "Other page", "blank": "Blank page"}
_MARK = re.compile(r"\bP\s*([1-9Il|])\s*/\s*([1-9])\b")


def page_kind(text):
    t = text or ""
    if sum(1 for rx in RN_ANCHORS if rx.search(t)) >= 2:
        return "release_note"
    for k, rx in SUPPORT:
        if rx.search(t):
            return k
    return "other"


def ink(pdf, n, td):
    """Share of dark pixels per page (30 dpi greyscale), to tell blank duplex backs from pages with content."""
    run(["pdftoppm", "-r", "40", "-gray", str(pdf), str(Path(td) / "ink")], 600)
    imgs = sorted(Path(td).glob("ink*.pgm"), key=lambda p: int(re.search(r"(\d+)\.pgm$", p.name).group(1)))
    out = []
    for img in imgs[:n]:
        b = img.read_bytes()
        # P5 header: magic, width, height, maxval, then the pixels (comments are not written by pdftoppm)
        parts = b.split(maxsplit=4)
        px = parts[4] if len(parts) == 5 else b""
        w, h = int(parts[1]), int(parts[2])
        px = px[-w * h:] if w * h <= len(px) else px
        # ignore a 4% border: scanner edges and punch holes
        mx, my = max(1, w // 25), max(1, h // 25)
        dark = tot = 0
        for y in range(my, h - my):
            row = px[y * w + mx:(y + 1) * w - mx]
            tot += len(row)
            dark += sum(1 for v in row if v < 190)
        out.append(dark / tot if tot else 0.0)
        img.unlink(missing_ok=True)
    while len(out) < n:
        out.append(1.0)
    return out


def marker(text):
    m = _MARK.search((text or "")[:1500])
    if not m:
        return None
    a = m.group(1)
    a = 1 if a in "Il|" else int(a)
    return (a, int(m.group(2))) if a <= int(m.group(2)) else None


# ------------------------------------------------------------------------------------------------ vision model
PROMPT = """You read one page of a Mining Pressure Systems (MPS) "RELEASE NOTE" (inspection report) form and return JSON.

The form, top to bottom:
- Top right, sometimes: a page marker such as "P1/2" or "P2/2".
- "INSPECTION REPORT / RELEASE NUMBER NO:" followed by the release number, e.g. "32577E /GAL002" (job number, a slash,
  letters and digits; sometimes a letter at the end like FAB004B). Copy it exactly.
- A table: CUSTOMER, PROJECT DESCRIPTION, PROJECT NUMBER (the customer's number, often 10 digits), MPS PROJECT NUMBER
  (the MPS job, e.g. 32577E), QCP NUMBER (e.g. MPS/32577E/001).
- "INSPECTION TO BE PERFORMED": a grid of inspection names, each with a box beside it. A box with an X (or a tick) means
  that inspection is selected. The names are: PIPE VISUAL, FLANGE VISUAL, COATING VISUAL, LINING VISUAL,
  PIPE / SPECIAL DIMENSIONS, SURFACE PREPARATION INTERNAL, FLANGE DIMENSIONAL, SURFACE PREPARATION EXTERNAL,
  WELDING VISUAL, FILM THICKNESS COATING, NON-DESTRUCTIVE EXAMINATION, FILM THICKNESS LINING,
  ELECTRICAL INSULATION DEFECT, and "OTHER" which is usually followed by a date or a note (copy that text).
  List ONLY the names whose box is marked. The box is to the RIGHT of its name.
- Sometimes photos.
- "ITEMS RELEASE LISTED AS FOLLOW" (spelt ITMENS): a table QTY | ITEM REF | MARK NO | DESCRIPTION. One object per
  row. A description that wraps onto a second line belongs to the same row. Skip empty rows. Copy item refs exactly
  (e.g. 441-93-1-2) and descriptions exactly, including sizes, angles and codes (80NBx25NB SCH80, T4000/3FF, 90°).
- "RELEASED FOR DESPATCH TO:" with a value: the total quantity released (a number, it equals the sum of the QTY
  column), and sometimes a second cell with where it goes (e.g. POLYMER) - give that as despatch_note.
- "NON CONFORMING (N.C.R.) NO:" with a value or blank.
- The sign-off block: a header row "COMPANY" then columns, usually MPS | the customer (e.g. VALTERRA) | 3RD PARTY
  (with the inspection company in brackets, e.g. BIE) | OTHERS. Rows: NAME & SURNAME, DESIGNATION, SIGNATURE, DATE,
  STAMP. Mostly handwritten. For EACH column give: the column heading as printed, the name, the designation, whether
  there is a signature (any pen mark in the SIGNATURE row of that column), the date as written, and the stamp text
  (or "stamp" if a stamp is there but unreadable). Leave values null when that cell is empty.

Return exactly:
{"page_marker": "P1/2" or null,
 "release_number": "...", "customer": "...", "project_description": "...", "project_number": "...",
 "mps_project_number": "...", "qcp_number": "...",
 "inspections": ["COATING VISUAL", ...], "other_text": "..." or null, "has_photos": true/false,
 "items": [{"qty": "1", "item_ref": "441-93-1-2", "mark_no": "2", "description": "..."}],
 "released_for_despatch_to": "..." or null, "despatch_note": "..." or null, "ncr_no": "..." or null,
 "signoff": [{"party": "MPS", "name": "...", "designation": "...", "signed": true, "date": "27/10/26", "stamp": "..."}],
 "legibility": "good" | "fair" | "poor"}
Never invent values. Use null for anything empty or unreadable."""

SCHEMA = {
    "type": "object",
    "properties": {
        "page_marker": {"type": ["string", "null"]},
        "release_number": {"type": ["string", "null"]}, "customer": {"type": ["string", "null"]},
        "project_description": {"type": ["string", "null"]}, "project_number": {"type": ["string", "null"]},
        "mps_project_number": {"type": ["string", "null"]}, "qcp_number": {"type": ["string", "null"]},
        "inspections": {"type": "array", "items": {"type": "string"}}, "other_text": {"type": ["string", "null"]},
        "has_photos": {"type": "boolean"},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "qty": {"type": ["string", "number", "null"]}, "item_ref": {"type": ["string", "null"]},
            "mark_no": {"type": ["string", "number", "null"]}, "description": {"type": ["string", "null"]}},
            "required": ["qty", "item_ref", "mark_no", "description"]}},
        "released_for_despatch_to": {"type": ["string", "number", "null"]}, "despatch_note": {"type": ["string", "null"]},
        "ncr_no": {"type": ["string", "null"]},
        "signoff": {"type": "array", "items": {"type": "object", "properties": {
            "party": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]}, "designation": {"type": ["string", "null"]},
            "signed": {"type": ["boolean", "null"]}, "date": {"type": ["string", "null"]}, "stamp": {"type": ["string", "null"]}},
            "required": ["party", "name", "designation", "signed", "date", "stamp"]}},
        "legibility": {"type": "string", "enum": ["good", "fair", "poor"]}},
    "required": ["page_marker", "release_number", "customer", "project_description", "project_number", "mps_project_number",
                 "qcp_number", "inspections", "other_text", "has_photos", "items", "released_for_despatch_to", "despatch_note",
                 "ncr_no", "signoff", "legibility"]}


def render(pdf, page, px, out_png):
    run(["pdftoppm", "-png", "-scale-to", str(px), "-f", str(page), "-l", str(page), "-singlefile", str(pdf), out_png[:-4]], 300)
    return out_png


def render_crop(pdf, page, box_pt, out_png, dpi=300):
    """A strip of the page (x0, y0, x1, y1 in PDF points) at dpi."""
    k = dpi / 72.0
    x0, y0, x1, y1 = box_pt
    run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page), "-x", str(int(x0 * k)), "-y", str(int(y0 * k)),
         "-W", str(int((x1 - x0) * k)), "-H", str(int((y1 - y0) * k)), "-singlefile", str(pdf), out_png[:-4]], 300)
    return out_png


def chat(c, png, system, ask, fmt=None, num_predict=4096):
    body = {"model": c["model"], "stream": False, "keep_alive": c["keep_alive"],
            "options": {"temperature": 0, "num_ctx": int(c["num_ctx"]), "num_predict": num_predict},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": ask, "images": [base64.b64encode(open(png, "rb").read()).decode()]}]}
    if fmt is not None:
        body["format"] = fmt
    req = urllib.request.Request(c["ollama_url"].rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(c["timeout"])) as r:
        return json.loads(r.read())["message"]["content"]


def _json(raw):
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0) if m else raw)


def read_page(pdf, page, c, hint=None):
    with tempfile.TemporaryDirectory() as tmp:
        png = render(pdf, page, int(c["px"]), os.path.join(tmp, "p.png"))
        ask = "Read this release note page and return the JSON object described."
        if hint:
            ask += ("\n\nThe page's OCR text layer (a hint only: letters and digits are often wrong, trust the image):\n"
                    + hint[:3500])
        return _json(chat(c, png, PROMPT, ask, SCHEMA))


# ---- focused reads on a strip of the page: the tick boxes and the item table (28 Oct test: the whole-page read put
# ---- X marks against the wrong inspection on 4 of 8 pages and read one QTY "1" as "12")
def page_words(pdf, page):
    """(width_pt, height_pt, [(text, x0, y0, x1, y1)]) from the page's text layer (ocrmypdf)."""
    out = run(["pdftotext", "-bbox", "-f", str(page), "-l", str(page), str(pdf), "-"], 120, check=False).stdout
    m = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', out)
    if not m:
        return None, None, []
    import html as _h
    ws = [(_h.unescape(t), float(a), float(b), float(c2), float(d)) for a, b, c2, d, t in
          re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', out)]
    return float(m.group(1)), float(m.group(2)), ws


def _find(ws, first, nxt=None, after=0.0):
    """y0, y1 of the first word matching `first` (and the next word matching `nxt`) below y=after."""
    for i, (t, x0, y0, x1, y1) in enumerate(ws):
        if y0 < after or not re.match(first, t.strip("|[]_:.").upper()):
            continue
        if nxt is None:
            return y0, y1
        for t2, a0, b0, a1, b1 in ws[i + 1:i + 3]:
            if abs(b0 - y0) < max(4.0, (y1 - y0)) and re.match(nxt, t2.strip("|[]_:.").upper()):
                return y0, max(y1, b1)
    return None


GRID_PROMPT = """This is the "INSPECTION TO BE PERFORMED" table from a form. Each inspection name has a small box
directly to its RIGHT. The box either has an X (or a tick) in it, or is empty.
Transcribe the table row by row, left to right, one line per table row, writing each inspection name followed by
[X] when ITS OWN box (the one immediately to the right of the name) is marked, or [ ] when that box is empty.
For "OTHER", copy what is written after it instead of a box. Example of a line:
PIPE VISUAL [ ] | FLANGE VISUAL [X] | COATING VISUAL [ ] | LINING VISUAL [X]
Write only the transcription."""

ITEMS_PROMPT = """This is the items table of a release note: columns QTY | ITEM REF | MARK NO | DESCRIPTION, and below
it "RELEASED FOR DESPATCH TO:" with the total quantity released. Read every row exactly. A description that wraps onto
a second line belongs to the same row. Skip empty rows. QTY is a whole number; read it carefully (a tick or pen mark
next to a 1 is not a digit).
Return {"items": [{"qty": "1", "item_ref": "441-93-1-2", "mark_no": "2", "description": "..."}],
 "released_for_despatch_to": "23" or null, "despatch_note": "..." or null}"""
ITEMS_SCHEMA = {"type": "object", "properties": {
    "items": SCHEMA["properties"]["items"],
    "released_for_despatch_to": {"type": ["string", "number", "null"]}, "despatch_note": {"type": ["string", "null"]}},
    "required": ["items", "released_for_despatch_to", "despatch_note"]}

_GRID_NAMES = [(n, re.compile(r"\s*".join(re.escape(ch) for ch in re.sub(r"[^A-Z]", "", n)), re.I)) for n in [
    "PIPE / SPECIAL DIMENSIONS", "SURFACE PREPARATION INTERNAL", "SURFACE PREPARATION EXTERNAL", "FLANGE DIMENSIONAL",
    "FILM THICKNESS COATING", "FILM THICKNESS LINING", "NON-DESTRUCTIVE EXAMINATION", "ELECTRICAL INSULATION DEFECT",
    "PIPE VISUAL", "FLANGE VISUAL", "COATING VISUAL", "LINING VISUAL", "WELDING VISUAL"]]


def parse_grid(text):
    """The transcription -> ({name: True/False}, other_text). Longer names are matched first so 'FLANGE DIMENSIONAL'
    isn't taken for 'FLANGE VISUAL' and 'FILM THICKNESS COATING' isn't 'COATING VISUAL'."""
    res, other = {}, None
    for line in (text or "").splitlines():
        s = line.upper()
        spans = []
        for name, _ in _GRID_NAMES:
            rx = r"[^A-Z]*".join(re.escape(w) for w in name.replace("/", " ").split())
            for m in re.finditer(rx, s):
                if not any(a <= m.start() < b for a, b, _ in spans):
                    spans.append((m.start(), m.end(), name))
        spans.sort()
        for k, (a, b, name) in enumerate(spans):
            seg = s[b:spans[k + 1][0] if k + 1 < len(spans) else len(s)]
            box = re.search(r"\[\s*([^\]]*)\]", seg)
            if box is not None and name not in res:
                res[name] = bool(re.search(r"[X✓✔V]", box.group(1)))
        m = re.search(r"OTHER\s*[-–—:]*\s*(.+)$", line, re.I)
        if m and not other:
            other = clean(re.sub(r"\[\s*\]|\|", " ", m.group(1)))
    return res, other


def read_grid(pdf, page, c, ws, H):
    """Inspections ticked, from a strip of the page around the tick-box table; None when the table isn't found."""
    top = _find(ws, r"^INSPECTION$", r"^TO$")
    if not top:
        return None
    bot = _find(ws, r"^ELECTRICAL$", None, after=top[0]) or _find(ws, r"^I?TM?E?MENS$", None, after=top[0])
    y0, y1 = top[0] - 4, (bot[1] + 6) if bot else top[1] + H * 0.16
    if y1 - y0 > H * 0.3:
        y1 = y0 + H * 0.18
    W = max(w[3] for w in ws) + 20 if ws else 600
    best = None
    with tempfile.TemporaryDirectory() as tmp:
        for dpi in (300, 200):               # a second try at another size when the first transcription is incomplete
            png = render_crop(pdf, page, (0, max(0, y0), min(W + 30, 9999), y1), os.path.join(tmp, f"g{dpi}.png"), dpi=dpi)
            raw = chat(c, png, GRID_PROMPT, "Transcribe the table.", None, 600)
            res, other = parse_grid(raw)
            if best is None or len(res) > len(best["marks"]):
                best = {"marks": res, "other": other, "raw": raw[:800], "dpi": dpi}
            if len(res) >= 12:
                break
    return best


def read_items_strip(pdf, page, c, ws, H):
    top = _find(ws, r"^I?T[MN]?E?MENS$|^ITEMS$", r"^RELEASE") or _find(ws, r"^QTY$")
    if not top:
        return None
    bot = _find(ws, r"^NON$", r"^CONFORMING", after=top[0]) or _find(ws, r"^RELEASED$", None, after=top[0])
    y1 = (bot[1] + 8) if bot else H * 0.80
    W = max(w[3] for w in ws) + 20 if ws else 600
    with tempfile.TemporaryDirectory() as tmp:
        png = render_crop(pdf, page, (0, max(0, top[0] - 4), W + 30, y1), os.path.join(tmp, "i.png"), dpi=250)
        return _json(chat(c, png, ITEMS_PROMPT, "Read the table.", ITEMS_SCHEMA))


# ------------------------------------------------------------------------------------------------ values
def clean(v):
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip(" :;,-")
    return None if s.lower() in ("", "null", "none", "n/a", "-") else s


def norm_release(s):
    """'32577E /GAL002' -> '32577E/GAL002' (upper case, no spaces around the slash; O read for 0 inside the digits)."""
    s = clean(s)
    if not s:
        return None
    s = re.sub(r"\s*/\s*", "/", s.upper()).replace(" ", "")
    m = re.match(r"^(\d{4,5}[A-Z]?)/([A-Z]+?)([0-9O]*[0-9][0-9O]*|[0-9O]{2,})([A-Z]?)$", s)
    if m:
        s = f"{m.group(1)}/{m.group(2)}{m.group(3).replace('O', '0')}{m.group(4)}"
    return s


def norm_job(s):
    s = clean(s)
    if not s:
        return None
    m = re.search(r"\b(\d{4,5})\s*([A-Z])\b", s.upper())
    return f"{m.group(1)}{m.group(2)}" if m else s.upper().replace(" ", "")


def parse_date(s):
    s = clean(s)
    if not s:
        return None
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\s*[-./|\\]\s*(\d{1,2})\s*[-./|\\]\s*(\d{2,4})", s)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
    try:
        day = date(y, mo, d)
    except ValueError:
        return None
    return day.isoformat() if 2000 <= day.year <= date.today().year + 1 else None


def qty(v):
    s = clean(v)
    if s is None:
        return None
    s2 = s.replace(",", ".").replace("O", "0").replace("o", "0")
    try:
        n = float(re.sub(r"[^\d.]", "", s2))
        return int(n) if n.is_integer() else n
    except ValueError:
        return s


INSPECTIONS = ["PIPE VISUAL", "FLANGE VISUAL", "COATING VISUAL", "LINING VISUAL", "PIPE / SPECIAL DIMENSIONS",
               "SURFACE PREPARATION INTERNAL", "FLANGE DIMENSIONAL", "SURFACE PREPARATION EXTERNAL", "WELDING VISUAL",
               "FILM THICKNESS COATING", "NON-DESTRUCTIVE EXAMINATION", "FILM THICKNESS LINING", "ELECTRICAL INSULATION DEFECT"]


def norm_inspection(s):
    k = re.sub(r"[^A-Z]", "", str(s or "").upper())
    for name in INSPECTIONS:
        if re.sub(r"[^A-Z]", "", name) == k:
            return name.title().replace("Non-Destructive", "Non-destructive").replace(" / ", " / ")
    return None


def is_inspector(party):
    p = str(party or "").upper()
    return bool(p) and "MPS" not in p.split() and p not in ("MPS", "COMPANY", "OTHERS", "OTHER")


def lines_text(items, limit=4000):
    out = []
    for i in items:
        q = "" if i.get("qty") is None else f"{i['qty']:g} x " if isinstance(i["qty"], (int, float)) else f"{i['qty']} x "
        ref = " ".join(x for x in (i.get("item_ref") or "", f"(mark {i['mark_no']})" if i.get("mark_no") else "") if x)
        out.append(f"{q}{ref}{' ' if ref else ''}{i.get('description') or ''}".strip())
    return "\n".join(out)[:limit]


def to_fields(reads, pages_info, source_name=""):
    """reads: the model's answers for the release-note pages of one document, in page order.
    pages_info: [{"no", "kind"}] of every page kept in the document. -> (fields, flags)"""
    flags = []
    first = reads[0] if reads else {}
    f = {"doc_label": "Release Note", "library_doc_type": "Release Note"}
    f["release_number"] = norm_release(first.get("release_number"))
    f["release_number_read"] = clean(first.get("release_number"))
    for k in ("customer", "project_description", "project_number", "qcp_number"):
        f[k] = clean(first.get(k))
    if f["project_number"]:
        f["project_number"] = f["project_number"].replace(" ", "")
    f["job_number"] = norm_job(first.get("mps_project_number"))
    if f.get("qcp_number"):
        f["qcp_number"] = re.sub(r"\s*/\s*", "/", f["qcp_number"].upper())
    # items from every release-note page of the document (P1/2, P2/2 ...)
    items = []
    for r in reads:
        for it in r.get("items") or []:
            if not isinstance(it, dict):
                continue
            d = {"qty": qty(it.get("qty")), "item_ref": clean(it.get("item_ref")), "mark_no": clean(it.get("mark_no")),
                 "description": clean(it.get("description"))}
            if d["item_ref"]:
                d["item_ref"] = d["item_ref"].replace(" ", "")
            if any(v not in (None, "") for v in d.values()):
                items.append(d)
    for n, it in enumerate(items, 1):
        it["#"] = n
    f["items"] = items
    f["line_count"] = len(items)
    tq = [i["qty"] for i in items if isinstance(i.get("qty"), (int, float))]
    f["total_qty"] = sum(tq) if tq else None
    f["lines_text"] = lines_text(items) or None
    insp = []
    grids = [r["_grid"] for r in reads if isinstance(r.get("_grid"), dict) and len(r["_grid"].get("marks") or {}) >= 10]
    if grids:                                    # the focused read of the tick-box strip (more reliable)
        for g in grids:
            for name, on in g["marks"].items():
                n = norm_inspection(name)
                if on and n and n not in insp:
                    insp.append(n)
        insp.sort(key=lambda n: [norm_inspection(x) for x in INSPECTIONS].index(n))
        f["inspections_from"] = "tick-box strip"
    elif any(isinstance(r.get("_grid"), dict) and len(r["_grid"].get("marks") or {}) >= 6 for r in reads):
        # part of the strip read: its marks where it has them, the whole-page read for the rest
        g = max((r["_grid"] for r in reads if isinstance(r.get("_grid"), dict)), key=lambda g: len(g.get("marks") or {}))
        whole = {norm_inspection(x) for r in reads for x in (r.get("inspections") or []) if norm_inspection(x)}
        for name in INSPECTIONS:
            n = norm_inspection(name)
            on = g["marks"].get(name) if name in g["marks"] else (n in whole)
            if on:
                insp.append(n)
        f["inspections_from"] = f"tick-box strip ({len(g['marks'])} of 13) + whole page"
    else:
        for r in reads:
            for s in r.get("inspections") or []:
                n = norm_inspection(s)
                if n and n not in insp:
                    insp.append(n)
        f["inspections_from"] = "whole page"
    f["inspections"] = insp
    f["inspections_text"] = "; ".join(insp) or None
    raws = [r["_grid"].get("raw") for r in reads if isinstance(r.get("_grid"), dict) and r["_grid"].get("raw")]
    f["inspections_strip_text"] = raws[0][:600] if raws else None
    f["other_text"] = clean(first.get("other_text")) or next((g.get("other") for g in grids if g.get("other")), None)
    f["has_photos"] = any(bool(r.get("has_photos")) for r in reads)
    last = next((r for r in reversed(reads) if r.get("signoff")), first)
    lr = next((r for r in reversed(reads) if clean(r.get("released_for_despatch_to"))), last)
    raw_rel = clean(lr.get("released_for_despatch_to"))
    f["released_for_despatch_to"] = raw_rel
    rq = qty(raw_rel) if raw_rel and re.fullmatch(r"[\dOo]{1,5}", raw_rel.replace(" ", "")) else None
    f["released_qty"] = rq if isinstance(rq, (int, float)) else None
    f["despatch_note"] = clean(lr.get("despatch_note"))
    f["despatch_to"] = f["despatch_note"] or (raw_rel if f["released_qty"] is None else None)
    f["ncr_no"] = clean(last.get("ncr_no"))
    f["items_from"] = "item table strip (re-read)" if any(r.get("_items_reread") for r in reads) else "whole page"
    so = []
    for s in last.get("signoff") or []:
        if not isinstance(s, dict):
            continue
        d = {"party": clean(s.get("party")), "name": clean(s.get("name")), "designation": clean(s.get("designation")),
             "signed": bool(s.get("signed")), "date_as_written": clean(s.get("date")), "date": parse_date(s.get("date")),
             "stamp": clean(s.get("stamp"))}
        if d["party"] and str(d["party"]).upper() in ("COMPANY",):
            continue
        so.append(d)
    f["signoff"] = so
    mps = next((s for s in so if "MPS" in str(s.get("party") or "").upper().split()), None)
    insps = [s for s in so if is_inspector(s.get("party")) and (s["signed"] or s["name"] or s["date"])]
    f["mps_signed"] = bool(mps and (mps["signed"] or mps["name"]))
    f["mps_name"] = mps["name"] if mps else None
    f["signed_off"] = any(s["signed"] or (s["name"] and s["date"]) for s in insps)
    f["signed_off_label"] = "Yes" if f["signed_off"] else "No"
    who = insps[0] if insps else None
    f["inspector"] = who["name"] if who else None
    f["inspector_company"] = who["party"] if who else None
    f["inspector_designation"] = who["designation"] if who else None
    f["signed_date"] = (who or {}).get("date") or (mps or {}).get("date")
    f["inspection_date"] = parse_date(f["other_text"]) or f["signed_date"]
    f["signoff_text"] = "; ".join(
        f"{s['party'] or '?'}: {s['name'] or '-'}{' (' + s['designation'] + ')' if s['designation'] else ''}"
        f"{' ' + s['date_as_written'] if s['date_as_written'] else ''}{' signed' if s['signed'] else ''}"
        for s in so if s["name"] or s["signed"] or s["date_as_written"]) or None
    leg = [r.get("legibility") for r in reads if r.get("legibility")]
    f["legibility"] = "poor" if "poor" in leg else "fair" if "fair" in leg else (leg[0] if leg else None)
    # supporting pages
    kinds = [p["kind"] for p in pages_info]
    sup = [k for k in kinds if k != "release_note"]
    f["pages"] = len(pages_info)
    f["release_note_pages"] = kinds.count("release_note")
    f["supporting_pages"] = len(sup)
    f["supporting_text"] = "; ".join(f"{KIND_LABEL.get(k, k)} ({sup.count(k)})" for k in dict.fromkeys(sup)) or None

    # checks
    if not f["release_number"]:
        flags.append("check: release number not read")
    elif not re.fullmatch(r"\d{4,5}[A-Z]/(?:[A-Z]{1,4}/)?[A-Z]{2,5}\d{2,4}[A-Z]?", f["release_number"]):
        flags.append(f"check: release number read as {f['release_number']}: unusual form")
    if not f["job_number"]:
        flags.append("check: MPS project number not read")
    elif f["release_number"] and not f["release_number"].startswith(f["job_number"] + "/"):
        flags.append(f"check: release number {f['release_number']} does not start with the MPS project number {f['job_number']}")
    if f.get("qcp_number") and f["job_number"] and f["job_number"] not in f["qcp_number"]:
        flags.append(f"check: QCP number {f['qcp_number']} does not carry the MPS project number {f['job_number']}")
    if not f["customer"]:
        flags.append("check: customer not read")
    if not items:
        flags.append("check: no items listed on the release note")
    elif f["released_qty"] is not None and f["total_qty"] is not None and abs(f["total_qty"] - f["released_qty"]) > 1e-6:
        flags.append(f"check: the items add up to {f['total_qty']:g} but the release note says {f['released_qty']:g} released")
    for it in items:
        if it.get("qty") is None or not isinstance(it.get("qty"), (int, float)):
            flags.append(f"check: line {it['#']}: quantity '{it.get('qty')}' not a number")
        if not it.get("item_ref") and not it.get("description"):
            flags.append(f"check: line {it['#']}: no item ref or description")
    if f["legibility"] == "poor":
        flags.append("poor_legibility")
    marks = [clean(r.get("page_marker")) for r in reads]
    if any(m and re.match(r"P\s*1\s*/\s*([2-9])", m.upper()) for m in marks[:1]) and len(reads) < 2:
        flags.append("check: marked as page 1 of several but the next release-note page was not in the scan")
    f["review"] = "; ".join(x for x in flags) or None
    return f, flags


def recompute(f):
    """After a dashboard edit of the items or header."""
    items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    for n, it in enumerate(items, 1):
        it["#"] = n
        if it.get("qty") is not None and not isinstance(it["qty"], (int, float)):
            it["qty"] = qty(it["qty"])
    f["items"] = items
    f["line_count"] = len(items)
    tq = [i["qty"] for i in items if isinstance(i.get("qty"), (int, float))]
    f["total_qty"] = sum(tq) if tq else None
    f["lines_text"] = lines_text(items) or None
    if isinstance(f.get("signed_off"), bool):
        f["signed_off_label"] = "Yes" if f["signed_off"] else "No"
    if f.get("release_number"):
        f["release_number"] = norm_release(f["release_number"])
    if f.get("job_number"):
        f["job_number"] = norm_job(f["job_number"])


def library_name(f, fallback):
    rn = f.get("release_number")
    if rn:
        return re.sub(r"[^A-Za-z0-9 _.-]+", "-", rn).strip("-") + " Release Note"
    return re.sub(r"[^A-Za-z0-9 _.-]+", "_", fallback)[:120]


# ------------------------------------------------------------------------------------------------ the scan
def same_release(a, b):
    a, b = norm_release(a), norm_release(b)
    return bool(a and b and a == b)


def read(pdf, cfg=None, log=None, name=None):
    """One scanned batch -> (prep, docs, tmp). Each doc: {"pages": [scan page numbers kept], "dropped": [blank pages],
    "pdf": Path in tmp (the caller removes tmp), "fields": {}, "flags": [], "hold": reason or None, "stem": str,
    "seconds_model": float}"""
    c = dict(DEFAULTS, **(cfg or {}))
    tmp = Path(tempfile.mkdtemp(prefix="rn-"))
    upright = tmp / "upright.pdf"
    if dn_reader:
        prep = dn_reader.make_upright(pdf, upright, {"ocr": c["ocr"], "ocr_jobs": c["ocr_jobs"], "ocr_lang": c["ocr_lang"]}, log)
    else:
        shutil.copy2(pdf, upright)
        prep = "as_scanned"
    n = page_count(upright)
    texts = dn_reader.page_texts(upright, n) if dn_reader else [""] * n
    with tempfile.TemporaryDirectory() as td:
        inks = ink(upright, n, td)
    pages = []
    for i in range(n):
        t = texts[i] or ""
        kind = page_kind(t)
        if kind == "other" and len(re.sub(r"\s", "", t)) < 25 and inks[i] < float(c["blank_ink"]):
            kind = "blank"
        pages.append({"no": i + 1, "kind": kind, "ink": round(inks[i], 4), "marker": marker(t) if kind == "release_note" else None})

    # read every release-note page
    t_model = 0.0
    for p in pages:
        if p["kind"] != "release_note":
            continue
        t0 = time.time()
        try:
            p["read"] = read_page(upright, p["no"], c, hint=texts[p["no"] - 1])
        except Exception as e:                       # one retry: Ollama 500s happen now and then
            if log:
                log.warning("release note page %s: %s; retrying once", p["no"], e)
            time.sleep(5)
            p["read"] = read_page(upright, p["no"], c, hint=texts[p["no"] - 1])
        W, H, ws = page_words(upright, p["no"])
        p["ws"], p["H"] = ws, H
        if ws and c.get("grid_strip", True):
            try:
                p["read"]["_grid"] = read_grid(upright, p["no"], c, ws, H)
            except Exception as e:
                if log:
                    log.warning("release note page %s: tick-box strip not read: %s", p["no"], e)
        t_model += time.time() - t0
        pm = clean(p["read"].get("page_marker"))
        if pm and not p["marker"]:
            m = re.match(r"P\s*(\d)\s*/\s*(\d)", pm.upper())
            if m and int(m.group(1)) <= int(m.group(2)):
                p["marker"] = (int(m.group(1)), int(m.group(2)))

    # group
    groups = []
    for p in pages:
        if p["kind"] == "release_note":
            g = groups[-1] if groups else None
            cont = False
            if g and g["rn"]:
                prev = g["rn"][-1]
                rn_now = p["read"].get("release_number")
                rn_prev = prev["read"].get("release_number")
                if p["marker"] and p["marker"][0] > 1 and (same_release(rn_now, rn_prev) or not norm_release(rn_now)):
                    cont = True
                elif prev["marker"] and prev["marker"][0] < prev["marker"][1] and same_release(rn_now, rn_prev):
                    cont = True
                elif same_release(rn_now, rn_prev) and all(q["kind"] in ("release_note", "blank") for q in g["pages"]):
                    cont = True      # e.g. 32577E/FAB004: page 1 header + photos, page 2 photos + items + sign-off
            if cont:
                g["rn"].append(p)
                g["pages"].append(p)
            else:
                groups.append({"rn": [p], "pages": [p]})
        else:
            if not groups:
                groups.append({"rn": [], "pages": []})
            groups[-1]["pages"].append(p)

    for g in groups:
        if not g["rn"] or not c.get("items_strip", True):
            continue
        f0, _ = to_fields([p["read"] for p in g["rn"]], g["pages"])
        if f0.get("released_qty") is None or (f0.get("total_qty") is not None and abs(f0["total_qty"] - f0["released_qty"]) < 1e-6):
            continue
        t0 = time.time()
        rereads = {}
        for p in g["rn"]:
            if not p.get("ws"):
                continue
            try:
                rr = read_items_strip(upright, p["no"], c, p["ws"], p["H"])
            except Exception as e:
                if log:
                    log.warning("release note page %s: item strip not read: %s", p["no"], e)
                rr = None
            if rr and rr.get("items"):
                rereads[p["no"]] = rr
        t_model += time.time() - t0
        if rereads:
            trial = []
            for p in g["rn"]:
                r = dict(p["read"])
                if p["no"] in rereads:
                    r["items"] = rereads[p["no"]]["items"]
                    r["_items_reread"] = True
                trial.append(r)
            f1, _ = to_fields(trial, g["pages"])
            if f1.get("total_qty") is not None and abs(f1["total_qty"] - f0["released_qty"]) < 1e-6:
                for p, r in zip(g["rn"], trial):
                    p["read"] = r
            elif log:
                log.info("items re-read for %s still add up to %s (released %s): kept the first read",
                         f0.get("release_number"), f1.get("total_qty"), f0.get("released_qty"))

    docs, seen = [], {}
    for gi, g in enumerate(groups, 1):
        keep = [p for p in g["pages"] if p["kind"] != "blank"]
        dropped = [p["no"] for p in g["pages"] if p["kind"] == "blank"]
        if not keep:
            continue
        nos = [p["no"] for p in keep]
        part = tmp / f"doc{gi:03d}.pdf"
        singles = []
        for k in nos:
            s = tmp / f"g{gi:03d}-{k:04d}.pdf"
            run(["pdfseparate", "-f", str(k), "-l", str(k), str(upright), str(s)], 120)
            singles.append(str(s))
        if len(singles) == 1:
            shutil.move(singles[0], part)
        else:
            run(["pdfunite", *singles, str(part)], 300)
            for s in singles:
                Path(s).unlink(missing_ok=True)
        if g["rn"]:
            f, flags = to_fields([p["read"] for p in g["rn"]], keep, name or Path(pdf).name)
            hold = None
            rn = f.get("release_number")
            if rn:
                if rn in seen:
                    flags.append(f"check: release number {rn} appears twice in this scan (pages {seen[rn]} and {nos[0]})")
                    f["review"] = "; ".join(flags)
                seen.setdefault(rn, nos[0])
            stem = library_name(f, f"{Path(name or pdf).stem} p{nos[0]}")
        else:
            kinds = [p["kind"] for p in keep]
            f = {"doc_label": "No release note", "library_doc_type": None, "pages": len(keep),
                 "supporting_text": "; ".join(f"{KIND_LABEL.get(k, k)} ({kinds.count(k)})" for k in dict.fromkeys(kinds))}
            flags = ["no_release_note"]
            f["review"] = "Pages before the first release note in the scan"
            hold = "no_release_note"
            stem = re.sub(r"[^A-Za-z0-9 _.-]+", "_", f"No release note - {Path(name or pdf).stem} p{nos[0]}")
        f["source_pages"] = f"{nos[0]}-{nos[-1]}" if len(nos) > 1 else str(nos[0])
        f["blank_pages_dropped"] = len(dropped)
        f["page_kinds"] = ", ".join(f"{p['no']}:{p['kind']}" for p in keep)
        f["content_hash"] = sha256(part).upper()
        docs.append({"pages": nos, "dropped": dropped, "pdf": part, "fields": f, "flags": flags, "hold": hold, "stem": stem,
                     "seconds_model": round(t_model, 1)})
    return prep, docs, tmp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", nargs="+")
    ap.add_argument("--out", default=".")
    ap.add_argument("--model")
    a = ap.parse_args()
    cfg = {"model": a.model} if a.model else {}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for src in a.pdf:
        t0 = time.time()
        prep, docs, tmp = read(src, cfg, name=Path(src).name)
        try:
            for k, d in enumerate(docs, 1):
                stem = f"{Path(src).stem}-{k:02d}"
                shutil.copy2(d["pdf"], out / f"{stem}.pdf")
                json.dump({"pages": d["pages"], "dropped": d["dropped"], "hold": d["hold"], "flags": d["flags"], "stem": d["stem"],
                           "fields": d["fields"]}, open(out / f"{stem}.json", "w"), indent=1, ensure_ascii=False)
                f = d["fields"]
                print(f"{stem} p{f.get('source_pages')}: {f.get('release_number') or '-'} | {f.get('customer') or ''} | job {f.get('job_number')}"
                      f" | {f.get('line_count', 0)} items | signed off {f.get('signed_off')} ({f.get('inspector') or '-'}, {f.get('signed_date') or '-'})"
                      f" | {f.get('supporting_text') or ''} | dropped {len(d['dropped'])} | {'; '.join(d['flags']) or 'clean'}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"{src}: {len(docs)} documents in {time.time() - t0:.0f}s (prep {prep})")


if __name__ == "__main__":
    main()

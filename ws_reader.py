#!/usr/bin/env python3
"""Workshop sheet (WS sheet / shop drawing) reader for the MPS OCR pipeline.

One page = one workshop sheet. Header: Date, Sheet x of y, Project, Corrosion Int / Ext (older forms: Destination),
Job number, Spec, customer drawing (DWG / DRG No), Rev, MPS DWG No (our internal sheet reference), Batch No; MTO and
Drawn by on CAD sheets. Line items: Item No, Qty, Size (Description on CAD sheets), customer DWG No per item, and the
dimension columns (L, A, B, R, theta, Thk, SEG ...).

How a page is read:
  * CAD-printed sheets (2023 on) have a text layer: read on the CPU with rules (text_layer), no GPU.
  * Scans (printed or hand-drawn forms): the vision model (Qwen2.5-VL in Ollama) with the prompt below and the worked
    examples in ws_fewshot/ (or the set a training job installed: training/ws/active.json). Any text layer is passed
    to the model as a hint and its header values win over the model's.
  * looks_like_ws(): cheap check (file name + Tesseract keywords) used by the worker to send workshop sheets that were
    scanned into the Purchase Orders queue to the Workshop Sheets type.

    python3 ws_reader.py SHEET.pdf [--ollama http://localhost:11434] [--model qwen2.5vl:7b] [--cpu-only] [--classify]
Standard library + poppler (pdftoppm, pdftotext); tesseract for looks_like_ws on scans.
"""
import argparse, base64, glob, json, os, re, subprocess, sys, tempfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import po_reader                       # job list (jobs_*.csv): completes "5368" to 5368D, finds the customer
except Exception:                          # pragma: no cover
    po_reader = None

FEWSHOT_DIR = os.path.join(HERE, "ws_fewshot")
NUM_PREDICT = 4096
NUM_CTX = 49152         # same as po_reader.NUM_CTX: Ollama keeps one model loaded across document types

PROMPT = '''You are reading a WORKSHOP SHEET ("WS sheet", shop drawing) from Mining Pressure Systems (MPS), Boksburg,
South Africa: the fabrication instruction for one pipe spool, bend or set of cut plates. One page = one sheet.
Three layouts:
1. Printed MPS form (logo top left). Left header rows: DATE, PROJECT, CORROSION INT., CORROSION EXT., JOB.
   Right: SHEET x OF y, DWG No (+ Rev), SPEC. Sketch in the middle. Table: ITEM | QTY | SIZE | dimension columns
   (L, A, B, R, the angle symbol, Thk, SEG ...). Above an item row there can be a mark row: "MK'd-UA1.1" or
   "MK-150PP025-S" (the item's mark) with "DWG No:13.D002" at its right (that item's customer drawing).
   Footer boxes: DWG No (or DRG No, or TEMPLATE No), MPS DWG No, BATCH No or MTO.
2. Older hand-drawn form: DATE, PROJECT, DESTINATION (the coating / lining, e.g. GALVANISING, UNCOATED/UNLINED), JOB;
   right: SHT x OF y, DRG, REV, SPEC. Table: Item | Qty | Size | Thk | A | B | angle | SEG. Usually handwritten.
3. CAD-printed sheet (2023 on): DATE, PROJECT, CORROSION INT, CORROSION EXT, JOB; right: "SHT. 1-5" (= sheet 1 of 5),
   ISSUE FOR APPROVAL, DRAWN BY, SPEC. Table: ITEM No | QTY | DESCRIPTION | L, with a "MK'd-" row and
   "DWG No.<customer drawing>" above the item row. Footer: NOTE, BILL, [Plt. Dwg.-1A], MPS DWG No, MTO.
   Plate cutting sheets have no table: each "MK'd-3A / 40 off BASE Plt. / 12 Thk." is one line
   (item_no "3A", qty 40, size "BASE Plt. 12 Thk."); "S355JR GRADE" is the spec.

Fields:
- job_number: as written ("J 5927 D", "5368 D", "9176 E"); MPS jobs are 4-5 digits + a series letter (C, D or E; older ones A or B).
- sheet_no / sheet_of: "Sheet 1 Of 1" -> 1 / 1; "SHT. 2-5" -> 2 / 5; "2/2" -> 2 / 2.
- customer_dwg_no: the CUSTOMER's drawing number from the header "DWG No"/"DRG" box or the footer "DWG No"/"DRG No"
  box. "AS BELOW" means it is in the footer or on each line. null when blank.
- mps_dwg_no: the footer box "MPS DWG No" (our own sheet reference, e.g. "WK1", "3A", "1"). null when blank.
- batch_no: the footer box "BATCH No" (e.g. "001").
- Each line: item_no exactly as written without a leading dash ("-01" -> "01"); mark = the MK'd / MK- marking on
  the row above it (null if none); qty a number; size = the SIZE (or DESCRIPTION) text, e.g. "350NB x 6mm",
  "600NB x 6mm", "300NB" - a size that carries on over the next rows (no item or qty there) is ONE line, joined with
  spaces; dwg_no = a customer drawing number written for that item ("DWG No:" on the mark row, CAD: the "DWG No."
  text above the item row), else null; dims = every other column of that row
  keyed by its heading as printed: {"L": "3000"}, {"A": "227", "R": "533.4", "θ": "45°"}, {"Thk": "SCH 40", "SEG": "3"}.
  Empty rows are not lines.
- end_detail: end preparation / flange notes written near the sketch, e.g. "F.B.E 1000/3 F/F", "P.E.", "L/R/E 45° C/F=221".
- The QA stamp: a red rubber stamp "MINING PRESSURE SYSTEMS / ISSUED FOR CONSTRUCTION / DATE: ..... SIGN: ..... /
  Q A APPROVED", usually over the empty table rows.
  stamped = true when that stamp is on the sheet (signed or not), else false.
  ifc_signed = true ONLY when there is a handwritten signature (a pen scribble) on or across the SIGN line of the stamp;
  an empty dotted SIGN line is false. A stamp is often NOT signed - look carefully.
  stamp_date: the date written in the stamp. The year-month is often written above the DATE line ("2017-01") and the
  day on it ("10"): together 2017-01-10. stamp_date_as_written = exactly what is written there; null when blank.
- status: a PRINTED status such as "ISSUE FOR APPROVAL" (CAD sheets); null otherwise (the stamp has its own fields).
- notes: other written notes (NOTE, BILL, Plt. Dwg., colour bands), joined with "; ".
Dates are day-month-year unless written year first (2017-01-11).

Return ONLY this JSON object:
{
 "doc_type": "workshop_sheet|other",
 "layout": "printed_form|handwritten_form|cad",
 "date_as_written": string|null, "date_iso": "YYYY-MM-DD"|null,
 "sheet_no": number|null, "sheet_of": number|null,
 "project": string|null, "corrosion_int": string|null, "corrosion_ext": string|null, "destination": string|null,
 "job_number": string|null, "spec": string|null, "customer_dwg_no": string|null, "rev": string|null,
 "mps_dwg_no": string|null, "batch_no": string|null, "mto": string|null, "drawn_by": string|null,
 "stamped": true|false, "ifc_signed": true|false, "stamp_date_as_written": string|null, "stamp_date_iso": "YYYY-MM-DD"|null,
 "status": string|null, "end_detail": string|null,
 "lines": [{"item_no": string|null, "mark": string|null, "qty": number|null, "size": string|null, "dwg_no": string|null, "dims": {}}],
 "notes": string|null, "legibility": "good|fair|poor"
}
Copy numbers and codes exactly as written. Use null when you cannot read a value - do not guess.'''

HEADER_KEYS = ("date_iso", "sheet_no", "sheet_of", "project", "corrosion_int", "corrosion_ext", "destination", "job_number",
               "spec", "customer_dwg_no", "rev", "mps_dwg_no", "batch_no", "mto", "drawn_by", "status")


# ------------------------------------------------------------------------------------------------ small helpers
def _clean(v):
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip(" .:;-_")
    return s or None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else v
    m = re.search(r"\d+(?:[.,]\d+)?", str(v))
    if not m:
        return None
    x = float(m.group(0).replace(",", "."))
    return int(x) if x.is_integer() else x


def parse_date(s):
    """'09-01-2017', '17-01-2017', '2017-01-11', '26-05-2023', '2017.01.10' -> ISO, day first unless year first."""
    import datetime as dt
    s = str(s or "").strip()
    m = re.search(r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{2,4})", s)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
    try:
        day = dt.date(y, mo, d)
    except ValueError:
        return None
    return day.isoformat() if 1990 <= day.year <= dt.date.today().year + 1 else None


def _bool(v):
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("true", "yes", "y", "1", "signed")


def stamp_date(iso, written, sheet_date, chk):
    """The date written in the QA stamp. Usually the year-month above the DATE line and the day on it ("2017-01" + "10");
    a day on its own takes the sheet's year-month (noted in the checks)."""
    import datetime as dt
    d = parse_date(iso) or parse_date(written)
    w = str(written or "")
    if not d:
        ym = re.search(r"((?:19|20)\d\d)\s*[-./]\s*(\d{1,2})(?!\s*[-./]\s*\d)", w)
        rest = w[:ym.start()] + " " + w[ym.end():] if ym else w
        day = re.findall(r"(?<!\d)(\d{1,2})(?!\d)", rest)
        if ym and day:
            d = parse_date(f"{ym.group(1)}-{ym.group(2)}-{day[-1]}")
        elif day and len(day) == 1 and sheet_date:
            y, m, _ = str(sheet_date).split("-")
            d = parse_date(f"{y}-{m}-{day[0]}")
            if d:
                chk.append(f"stamp date: only the day ({day[0]}) written; month and year taken from the sheet date")
    if d and sheet_date:
        gap = (dt.date.fromisoformat(d) - dt.date.fromisoformat(str(sheet_date))).days
        if gap < -3:
            chk.append(f"stamp date {d} is before the sheet date {sheet_date}")
        elif gap > 365:
            chk.append(f"stamp date {d} is more than a year after the sheet date {sheet_date}")
    return d


def stamp_text(f):
    if f.get("ifc_signed"):
        return "Signed " + (f.get("stamp_date") or "(no date)")
    if f.get("stamped"):
        return "Stamped, not signed" + (f" ({f['stamp_date']})" if f.get("stamp_date") else "")
    if f.get("stamped") is False:
        return "No stamp"
    return None


def parse_sheet(s):
    """'1 Of 1', 'SHT. 1-5', '2/2', '1 OF 3' -> (1, 1) ..."""
    m = re.search(r"(\d{1,3})\s*(?:of|OF|Of|oF|/|-|\\)\s*(\d{1,3})", str(s or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _nb(v):
    s = _clean(v)
    if s and re.fullmatch(r"(?i)as\s*below|n/?a|none|-+|/+|\.+", s):
        return None
    return s


# ------------------------------------------------------------------------------------------------ PDF text
def pdf_text(path, layout=True):
    try:
        r = subprocess.run(["pdftotext"] + (["-layout"] if layout else []) + ["-f", "1", "-l", "1", str(path), "-"],
                           capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def is_scan(path):
    """True when page 1 carries a page-sized image (a scan, maybe with an OCR text layer), False for a CAD-printed PDF."""
    if not str(path).lower().endswith(".pdf"):
        return True
    try:
        r = subprocess.run(["pdfimages", "-list", "-f", "1", "-l", "1", str(path)], capture_output=True, timeout=60)
        for ln in r.stdout.decode("utf-8", "replace").splitlines()[2:]:
            c = ln.split()
            if len(c) > 4 and c[3].isdigit() and c[4].isdigit() and int(c[3]) >= 1000 and int(c[4]) >= 1000:
                return True
    except Exception:
        pass
    return False


def is_cad_text(text):
    t = (text or "").upper()
    return len(t.strip()) > 120 and bool(re.search(r"MPS\s*DWG", t)) and bool(re.search(r"\bJOB\s*:", t))


def from_text(text):
    """Rules for the CAD-printed sheets' text layer -> answer in the model's format (layout cad)."""
    t = text.replace("∅", "Ø").replace("Ø", "Ø")
    U = t.upper()
    d = {"doc_type": "workshop_sheet", "layout": "cad", "lines": [], "legibility": "good"}

    def grab(rx, flags=re.I):
        m = re.search(rx, t, flags)
        return _clean(m.group(1)) if m else None

    d["date_as_written"] = grab(r"DATE\s*:\s*([0-9][0-9 ./-]{6,12}[0-9])")
    d["date_iso"] = parse_date(d["date_as_written"])
    m = re.search(r"SHT\.?\s*(\d{1,3})\s*[-/]\s*(\d{1,3})|SHEET\s*:?\s*(\d{1,3})\s*OF\s*(\d{1,3})", U)
    if m:
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        d["sheet_no"], d["sheet_of"] = int(a), int(b)
    d["project"] = grab(r"PROJECT\s*:\s*(.+?)(?:\s{3,}|$)", re.I | re.M)
    for k, lab in (("corrosion_int", "INT"), ("corrosion_ext", "EXT")):
        m = re.search(rf"CORROSION\s+{lab}\s*[.:]?[ \t]*([^\n]*?)(?:\s{{3,}}|$)", t, re.I | re.M)
        d[k] = _clean(m.group(1)) if m else None
    d["job_number"] = grab(r"\bJOB\s*:\s*([A-Z]?\s*\d{3,5}\s*[A-E]?)\b")
    d["spec"] = grab(r"SPEC\s*:\s*(.+?)(?:\s{3,}|$)", re.I | re.M)
    if not d["spec"]:
        m = re.search(r"^\s*([A-Z0-9]+\s+GRADE)\s*$", U, re.M)            # plate sheets: "S355JR GRADE"
        d["spec"] = _clean(m.group(1)) if m else None
    d["drawn_by"] = grab(r"DRAWN\s+BY\s*:\s*([A-Z][A-Z. ]{0,10}?)(?:\s{2,}|$)", re.I | re.M)
    m = re.search(r"MPS\s*DWG\s*No\s*:?\s*([A-Z0-9][A-Z0-9 -]{0,8}?)(?:\s{2,}|\)|$)", t, re.I | re.M)
    d["mps_dwg_no"] = re.sub(r"\s+", "", m.group(1)).upper() if m else None
    d["mto"] = grab(r"MTO\s*:?\s*(\d+)")
    d["status"] = "ISSUE FOR APPROVAL" if "ISSUE FOR APPROVAL" in U else ("ISSUED FOR CONSTRUCTION" if "ISSUED FOR CONSTRUCTION" in U else None)
    d["customer_dwg_no"] = None
    d["rev"] = d["batch_no"] = d["destination"] = None
    d["stamped"] = d["ifc_signed"] = False          # the QA stamp is a rubber stamp: never in a CAD PDF's own text
    d["stamp_date_as_written"] = d["stamp_date_iso"] = None

    # table rows: "01   40   350NB x (SCH60)   5981" (item, qty, description, L) with "DWG No.xxx" on the MK'd row
    dwg = mark = None
    head = re.search(r"ITEM\s*No\s*:?\s+QTY\s+DESCRIPTION(?P<rest>[^\n]*)", t, re.I)
    dim_name = (head.group("rest").split() or ["L"])[-1] if head else "L"
    if head:
        for ln in t[head.end():].splitlines():
            mm = re.search(r"DWG\s*No\.?\s*:?\s*([A-Z0-9][^\s]*)", ln, re.I)
            mk = re.search(r"MK'?\"?d?\s*-\s*([A-Z0-9][A-Z0-9.\-/]*)", ln, re.I)
            if mm or re.search(r"MK'?\"?d?\s*-", ln, re.I):
                dwg = mm.group(1).strip() if mm else dwg
                mark = mk.group(1) if mk and not re.match(r"(?i)DWG", mk.group(1)) else None
                continue
            mr = re.match(r"^\s*(\d{1,3}[A-Z]?)\s{2,}(\d+(?:\.\d+)?)\s{2,}(.+?)\s{2,}(\d+(?:\.\d+)?)\s*$", ln)
            if mr:
                d["lines"].append({"item_no": mr.group(1), "mark": mark, "qty": _num(mr.group(2)), "size": _clean(mr.group(3)),
                                   "dwg_no": dwg, "dims": {dim_name: mr.group(4)}})
                mark = None
                continue
            mr = re.match(r"^\s*(\d{1,3}[A-Z]?)\s{2,}(\d+(?:\.\d+)?)\s{2,}(\S.*?)\s*$", ln)
            if mr and not re.match(r"(?i)NOTE", ln.strip()):
                d["lines"].append({"item_no": mr.group(1), "mark": mark, "qty": _num(mr.group(2)), "size": _clean(mr.group(3)),
                                   "dwg_no": dwg, "dims": {}})
                mark = None
    # plate cutting sheets: "MK'd-3A" then "40 off BASE Plt." then "12 Thk."
    if not d["lines"]:
        lines = t.splitlines()
        for i, ln in enumerate(lines):
            for mk in re.finditer(r"MK'?d\s*-\s*([0-9]+\s?[A-Z]?)", ln, re.I):
                col = mk.start()
                near = []
                for nxt in lines[i + 1:i + 5]:
                    seg = nxt[max(0, col - 12): col + 40].strip()
                    if seg:
                        near.append(seg)
                    if len(near) == 2:
                        break
                near = [re.split(r"\s{2,}", x)[0] for x in near]           # drop drawing text further along the line
                off = next((re.match(r"(\d+)\s*off\s+(.+)", x, re.I) for x in near if re.match(r"\d+\s*off\s", x, re.I)), None)
                if not off:
                    continue
                thk = next((x for x in near if re.search(r"Thk", x, re.I) and not re.match(r"\d+\s*off", x, re.I)), "")
                thk = re.search(r"\d+(?:\.\d+)?\s*Thk\.?", thk, re.I)
                size = _clean(off.group(2) + (" " + thk.group(0) if thk else ""))
                d["lines"].append({"item_no": re.sub(r"\s+", "", mk.group(1)).upper(), "qty": _num(off.group(1)),
                                   "size": size, "dwg_no": None, "dims": {}})
    # pack plates: "PACK PLATES CONSIST OFF" / "8 x 10Thk. PLATE" ... / "4 SET"s"
    pack = re.search(r"PACK PLATES CONSIST[^\n]*\n((?:[^\n]*\n){1,10})", t, re.I)
    sets = None
    if pack:
        for ln in pack.group(1).splitlines():
            seg = re.split(r"\s{3,}", ln.strip())[0] if ln.strip() else ""
            mp = re.match(r"(\d+)\s*x\s*(.+?(?:PLATE|Plt\.?))\s*$", seg, re.I)
            ms = re.match(r"(\d+)\s*SET", seg, re.I)
            if mp and not d["lines"] or (mp and all(x.get("pack") for x in d["lines"])):
                d["lines"].append({"item_no": None, "qty": _num(mp.group(1)), "size": "PACK " + _clean(mp.group(2)),
                                   "dwg_no": None, "dims": {}, "pack": True})
            elif ms:
                sets = ms.group(1)
            elif seg and not mp:
                break
        for x in d["lines"]:
            if x.pop("pack", None) and sets:
                x["dims"] = {"Sets": sets}
    # notes
    notes = [f"PACK PLATES: {sets} SETS" if sets else "PACK PLATES"] if pack else []
    for rx in (r"NOTE\s*:\s*(.+?)$", r"NOTES\s*:\s*(.+?)$", r"(\(BILL[^\[]+?)(?:\s{3,}|\[|$)", r"(\[Plt\.\s*Dwg\.?-?\s*[^\]]+\])"):
        for m in re.finditer(rx, t, re.I | re.M):
            v = _clean(m.group(1))
            if v and v not in notes and not re.match(r"(?i)PACK PLATES", v):
                notes.append(v)
    d["notes"] = "; ".join(notes) or None
    d["end_detail"] = "P.E." if re.search(r"\bP\.E\.", t) else None
    return d


# ------------------------------------------------------------------------------------------------ is it a WS sheet?
_NAME_HINT = re.compile(r"\bW\.?\s?S\.?\s*SHEETS?\b|WORK\s*SHOP\s*SHEET|WORKSHOP|\bDRGS?\b|DRAWING", re.I)
_KEYS = [r"PROJECT", r"CORROSION", r"\bSPEC\b", r"\bSHT\b|SHEET", r"MPS\s*DWG|\bDWG\b|\bDRG\b", r"BATCH", r"DESTINATION",
         r"ISSUED\s+FOR\s+CONSTRUCTION|ISSUE\s+FOR\s+APPROVAL", r"\bITEM\b", r"\bSIZE\b", r"\bJOB\b"]
_ORDER = re.compile(r"\bORDER\s*(?:A|PL|NO)\b|PURCHASE\s+ORDER|TAX\s+INVOICE|DELIVERY\s+NOTE|QUOTATION|REMITTANCE|PROOF OF PAYMENT", re.I)


def ocr_text(path, px=1600):
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", "1", "-singlefile", str(path), os.path.join(tmp, "p")],
                       check=True, capture_output=True, timeout=300)
        r = subprocess.run(["tesseract", os.path.join(tmp, "p.png"), "-", "--psm", "11"], capture_output=True, timeout=300)
        return r.stdout.decode("utf-8", "replace")


def looks_like_ws(path, name="", text=None, po_answer=None):
    """(True/False, score, why). Text layer or Tesseract on page 1; the file name ("WS SHEET", "DRGS") and the PO
    reader's own reading (it read a 'SIZE' table, no order number) add weight. Orders, invoices, quotes never pass."""
    if text is None:
        text = pdf_text(path) if str(path).lower().endswith(".pdf") else ""
        if len(text.strip()) < 80:
            try:
                text = ocr_text(path)
            except Exception:
                text = ""
    hits = [k for k in _KEYS if re.search(k, text, re.I)]
    score = len(hits)
    why = [f"{score} sheet words"]
    if _NAME_HINT.search(name or ""):
        score += 3
        why.append("file name")
    if is_cad_text(text):
        score += 3
        why.append("sheet layout")
    if po_answer and not po_answer.get("order_number") and str(po_answer.get("form")) == "not_an_order":
        score += 1
    if _ORDER.search(text) and "CORROSION" not in text.upper():
        return False, score, "order / invoice wording"
    return score >= 6, score, ", ".join(why)


# ------------------------------------------------------------------------------------------------ vision model
_FEW = {}


def fewshot_msgs(fewshot_dir):
    try:
        key = (fewshot_dir, os.path.getmtime(fewshot_dir))     # a training install re-creates the folder: read it again
    except (OSError, TypeError):
        key = (fewshot_dir, None)
    if key in _FEW:
        return _FEW[key]
    msgs = []
    if fewshot_dir and os.path.isdir(fewshot_dir):
        for j in sorted(glob.glob(os.path.join(fewshot_dir, "*.json"))):
            png = j[:-5] + ".png"
            if os.path.basename(j) != "sources.json" and os.path.exists(png):
                msgs.append({"role": "user", "content": PROMPT, "images": [base64.b64encode(open(png, "rb").read()).decode()]})
                msgs.append({"role": "assistant", "content": json.dumps(json.load(open(j, encoding="utf-8")), ensure_ascii=False)})
    _FEW[key] = msgs
    return msgs


def parse_answer(raw):
    if po_reader:
        return po_reader.parse_answer(raw)
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0) if m else raw)


def read_model(path, ollama_url, model, fewshot_dir, px=1600, timeout=1200, keep_alive="15m", page=1, hint=None):
    with tempfile.TemporaryDirectory() as tmp:
        p = str(path)
        if p.lower().endswith(".pdf"):
            subprocess.run(["pdftoppm", "-png", "-scale-to", str(px), "-f", str(page), "-l", str(page), "-singlefile", p,
                            os.path.join(tmp, "p")], check=True, capture_output=True, timeout=300)
            png = os.path.join(tmp, "p.png")
        else:
            png = p
        prompt = PROMPT + (f"\n\nThe PDF's own text layer for this page (exact, use it for the printed values):\n{hint[:4000]}" if hint else "")
        body = {"model": model, "stream": False, "format": "json", "keep_alive": keep_alive,
                "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
                "messages": fewshot_msgs(fewshot_dir) + [{"role": "user", "content": prompt,
                                                          "images": [base64.b64encode(open(png, "rb").read()).decode()]}]}
        req = urllib.request.Request(ollama_url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read())["message"]["content"]
    return parse_answer(raw)


def read(path, ollama_url=None, model="qwen2.5vl:7b", fewshot_dir=None, px=1600, timeout=1200, keep_alive="15m",
         cpu_only=False, source_name=None):
    """Read page 1 of path. Returns (answer, how): how = 'text_layer' (CPU) or 'vision' (+ 'text_hint')."""
    text = pdf_text(path) if str(path).lower().endswith(".pdf") else ""
    cad = is_cad_text(text) and not is_scan(path)        # an OCR text layer on a scan is not trusted for the rules
    if cad:
        d = from_text(text)
        if d.get("job_number") and d.get("date_iso") and (d["lines"] or d.get("mps_dwg_no")):
            return d, "text_layer"
    if cpu_only or not ollama_url:
        if cad:
            return from_text(text), "text_layer"
        raise RuntimeError("scanned sheet: needs the vision model (Ollama)")
    hint = text if len(text.strip()) > 120 else None
    d = read_model(path, ollama_url, model, fewshot_dir or FEWSHOT_DIR, px, timeout, keep_alive, hint=hint)
    if hint and cad:                            # printed values from the text layer win over the model's reading
        t = from_text(hint)
        for k in HEADER_KEYS:
            if t.get(k) not in (None, ""):
                d[k] = t[k]
        if t["lines"] and not d.get("lines"):
            d["lines"] = t["lines"]
        return d, "vision+text_hint"
    return d, "vision"


# ------------------------------------------------------------------------------------------------ answer -> fields
def fmt_dims(dims):
    if not isinstance(dims, dict):
        return ""
    return " ".join(f"{k}={v}" for k, v in dims.items() if v not in (None, ""))


def line_text(i):
    q = i.get("qty")
    q = "" if q is None else (format(q, "g") if isinstance(q, (int, float)) else str(q)) + " x "
    bits = [f"{i.get('item_no')}:" if i.get("item_no") else "", f"MK {i['mark']}" if i.get("mark") else "", q + str(i.get("size") or "")]
    if fmt_dims(i.get("dims")):
        bits.append(fmt_dims(i.get("dims")))
    if i.get("dwg_no"):
        bits.append(f"[DWG {i['dwg_no']}]")
    return " ".join(b for b in bits if b).strip()


def library_name(f, source_name="", ext=".pdf"):
    """JOB-WS-MPSDWG-SHTxofy-dd-MM-yyyy, parts left out when unread."""
    parts = [f.get("job_number") or "NO JOB", "WS"]
    if f.get("mps_dwg_no"):
        parts.append(str(f["mps_dwg_no"]))
    if f.get("sheet_no"):
        parts.append(f"SHT{f['sheet_no']}" + (f"of{f['sheet_of']}" if f.get("sheet_of") else ""))
    if f.get("date") and re.fullmatch(r"\d{4}-\d\d-\d\d", str(f["date"])):
        y, m, dd = str(f["date"]).split("-")
        parts.append(f"{dd}-{m}-{y}")
    if len(parts) <= 2 and source_name:
        parts.append(os.path.splitext(os.path.basename(source_name))[0][:60])
    name = re.sub(r'[\\/:*?"<>|#%\t\r\n,]+', " ", "-".join(str(p) for p in parts))
    return re.sub(r"\s+", " ", name).strip(" .")[:150] + ext


def job_from_name(name):
    m = re.search(r"(?<![A-Z0-9])J?\s?0*(\d{4,5})\s?([A-E])(?![A-Z0-9])", os.path.basename(str(name or "")).upper())
    return (m.group(1), m.group(2)) if m else None


def recompute(f):
    """Derived values after reading or a dashboard edit: line DWG numbers from the sheet, lines_text, counts, review."""
    items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    for i in items:
        if not i.get("dwg_no") and f.get("customer_dwg_no"):
            i["dwg_no"] = f["customer_dwg_no"]
            i["dwg_from_sheet"] = True
        elif i.get("dwg_from_sheet") and i.get("dwg_no") != f.get("customer_dwg_no"):
            i.pop("dwg_from_sheet", None)
        i["dims_text"] = fmt_dims(i.get("dims")) or None
    f["items"] = items
    f["line_count"] = len(items)
    f["lines_text"] = "\n".join(line_text(i) for i in items) or None
    f["total_qty"] = sum(i["qty"] for i in items if isinstance(i.get("qty"), (int, float))) or None
    f["sheet"] = (f"{f['sheet_no']} of {f['sheet_of']}" if f.get("sheet_of") else str(f["sheet_no"])) if f.get("sheet_no") else None
    if f.get("ifc_signed") or f.get("stamp_date"):
        f["stamped"] = True
    f["stamp_text"] = stamp_text(f)
    f["review"] = "; ".join(f.get("checks") or []) or None
    return f


def to_fields(d, source_name="", jobs_dir=None):
    """Pipeline fields + flags from an answer (model or text rules)."""
    flags, chk = [], []
    if d.get("_cut_off"):
        flags.append("answer_cut_off")
    f = {"form": "workshop_sheet", "layout": d.get("layout"), "doc_label": "Workshop sheet"}
    f["date_as_written"] = _clean(d.get("date_as_written"))
    f["date"] = parse_date(d.get("date_iso")) or parse_date(d.get("date_as_written"))
    sn, so = _num(d.get("sheet_no")), _num(d.get("sheet_of"))
    if sn is None and d.get("sheet"):
        sn, so = parse_sheet(d.get("sheet"))
    f["sheet_no"] = int(sn) if isinstance(sn, (int, float)) else None
    f["sheet_of"] = int(so) if isinstance(so, (int, float)) else None
    if f["sheet_no"] and f["sheet_of"] and f["sheet_no"] > f["sheet_of"]:
        chk.append(f"sheet {f['sheet_no']} of {f['sheet_of']}: check")
    for k in ("project", "corrosion_int", "corrosion_ext", "destination", "spec", "rev", "batch_no", "mto", "drawn_by",
              "status", "end_detail", "notes"):
        f[k] = _clean(d.get(k))
    for k in ("customer_dwg_no", "rev"):
        f[k] = _nb(d.get(k))
    f["spec"] = _nb(f["spec"])
    mps = _nb(d.get("mps_dwg_no"))
    f["mps_dwg_no"] = re.sub(r"\s+", "", mps).upper() if mps else None
    f["legibility"] = d.get("legibility") or None

    # QA stamp ("ISSUED FOR CONSTRUCTION / DATE / SIGN / Q A APPROVED"): stamped, signed, the date written in it
    st = f.get("status") or ""
    if f.get("layout") != "cad" and re.search(r"ISSUED\s+FOR\s+CONSTRUCTION", st, re.I):
        f["status"] = None                          # an older answer format put the stamp in status
        d.setdefault("stamped", True)
    f["stamped"] = _bool(d.get("stamped"))
    f["ifc_signed"] = _bool(d.get("ifc_signed"))
    f["stamp_date_as_written"] = _clean(d.get("stamp_date_as_written"))
    f["stamp_date"] = stamp_date(d.get("stamp_date_iso"), f["stamp_date_as_written"], f.get("date"), chk)
    if f["ifc_signed"] or f["stamp_date"]:
        f["stamped"] = True

    # job number: as written, completed with its letter from the job list and the sheet date (same rules as orders)
    written = _clean(d.get("job_number"))
    f["job_number_read"] = written
    job, rec = None, None
    m = re.search(r"0*(\d{3,5})\s*\.?\s*([A-E])?\b", (written or "").upper().replace("J", " ", 1) if written else "")
    named = job_from_name(source_name)
    if po_reader and jobs_dir:
        po_reader.JOBS_DIR = str(jobs_dir)
    if m or named:
        dg, lt = (m.group(1), m.group(2)) if m else named
        if named and m and int(named[0]) != int(m.group(1)):
            chk.append(f"job: file name says {int(named[0])}{named[1]}, sheet reads {written}")
        if not lt and named and int(named[0]) == int(dg):
            lt = named[1]
        if po_reader and getattr(po_reader, "JOBS_DIR", None):
            try:
                job, rec = po_reader.complete_job(dg, lt or None, f["date"], chk, written=written or dg)
            except Exception as e:       # job list unreadable: keep what was written
                chk.append(f"job list check failed: {e}")
        if not job:
            n = int(dg)
            job = ((f"{n:04d}" if n < 10000 else str(n)) + lt) if lt else None
            if not lt:
                chk.append(f"job {written} written without its letter: add it")
    f["job_number"] = job
    f["job_customer"] = (rec or {}).get("customer")
    f["job_name"] = (rec or {}).get("name")

    items = []
    for ln in d.get("lines") or []:
        if not isinstance(ln, dict):
            continue
        it = {"item_no": _clean(str(ln.get("item_no")).lstrip("-") if ln.get("item_no") is not None else None),
              "mark": _nb(ln.get("mark")), "qty": _num(ln.get("qty")), "size": _clean(ln.get("size") or ln.get("description")),
              "dwg_no": _nb(ln.get("dwg_no")),
              "dims": {str(k).strip(): _clean(v) for k, v in (ln.get("dims") or {}).items() if _clean(v)} if isinstance(ln.get("dims"), dict) else {}}
        if it["size"] or it["qty"] is not None or it["dims"]:
            items.append(it)
    f["items"] = items
    f["checks"] = chk
    recompute(f)
    if d.get("doc_type") not in (None, "workshop_sheet"):
        flags.append("not_a_workshop_sheet")
    for k in ("date", "job_number"):
        if not f.get(k):
            flags.append(f"missing_{k}")
    if not items:
        flags.append("no_lines")
    if not f.get("sheet_no"):
        flags.append("missing_sheet")
    flags += ["check: " + c for c in chk]
    return f, flags


ANSWER_KEYS = ("doc_type", "layout", "date_as_written", "date_iso", "sheet_no", "sheet_of", "project", "corrosion_int",
               "corrosion_ext", "destination", "job_number", "spec", "customer_dwg_no", "rev", "mps_dwg_no", "batch_no", "mto",
               "drawn_by", "stamped", "ifc_signed", "stamp_date_as_written", "stamp_date_iso", "status", "end_detail", "lines",
               "notes", "legibility")


def to_answer(f):
    """A checked sidecar's fields -> the JSON answer the reader is taught to give (training worked examples)."""
    d = f.get("date")
    dw = f.get("date_as_written")
    if d and (not dw or parse_date(dw) != d):
        y, m, dd = str(d).split("-")
        dw = f"{dd}-{m}-{y}"
    job = f.get("job_number")
    jr = str(f.get("job_number_read") or "")
    if job and jr and re.sub(r"\D", "", jr).lstrip("0") == re.sub(r"\D", "", job).lstrip("0"):
        job = jr                                # what is on the page; the tidy-up completes the letter
    a = {k: f.get(k) for k in ANSWER_KEYS}
    sd, sw = f.get("stamp_date"), f.get("stamp_date_as_written")
    if sd and (not sw or not re.search(r"\d", str(sw))):
        sw = sd
    a.update(stamp_date_iso=sd, stamp_date_as_written=sw if sd or sw else None,
             stamped=f.get("stamped") if "stamped" in f else None, ifc_signed=f.get("ifc_signed") if "ifc_signed" in f else None)
    a.update(doc_type="workshop_sheet", layout=f.get("layout") or "printed_form", date_as_written=dw, date_iso=d, job_number=job,
             legibility=f.get("legibility") or "good",
             lines=[{"item_no": i.get("item_no"), "mark": i.get("mark"), "qty": i.get("qty"), "size": i.get("size"),
                     "dwg_no": None if i.get("dwg_from_sheet") else i.get("dwg_no"), "dims": i.get("dims") or {}}
                    for i in (f.get("items") or []) if isinstance(i, dict)])
    return a


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="+")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--fewshot", default=FEWSHOT_DIR)
    ap.add_argument("--jobs-dir", default=None, help="pipeline root with jobs_*.csv")
    ap.add_argument("--cpu-only", action="store_true")
    ap.add_argument("--classify", action="store_true", help="only say whether each file looks like a workshop sheet")
    a = ap.parse_args()
    for p in a.pdf:
        if a.classify:
            print(json.dumps({"file": os.path.basename(p), "ws": looks_like_ws(p, os.path.basename(p))}))
            continue
        d, how = read(p, None if a.cpu_only else a.ollama, a.model, a.fewshot, cpu_only=a.cpu_only)
        f, flags = to_fields(d, os.path.basename(p), a.jobs_dir)
        print(json.dumps({"file": os.path.basename(p), "how": how, "flags": flags, "name": library_name(f, p), "fields": f},
                         indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()

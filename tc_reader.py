#!/usr/bin/env python3
"""Test certificate (mill test certificate, MTC) reader for the MPS OCR pipeline.

One file = one library item. A file holds one or more certificate pages (a multi-heat pipe cert has one page per
heat) and often other pages scanned with it (inspection certificates, hydro reports, packing lists, bills of lading,
invoices, LC covers): those are kept in the file and recorded as extra pages, not read for items.

How a file is read (page by page):
  * pdftotext per page: the text layer is mostly OCR of a scan (I00NB for 100NB), so it is only a hint - it is used
    to skip pages that are plainly shipping papers and passed to the model with each page.
  * Every other page goes to the vision model (Qwen-VL in Ollama) with the prompt below and the worked examples in
    tc_fewshot/ for the file's family (falls back to any). One call per page; chemistry pages at 2 000 px.
  * Pages are merged: header fields first-read wins (cert numbers collected), items concatenated, heats merged by
    heat number. Then normalised (NB, schedule, class, grade, order numbers) and validated - failures become
    review checks, never silent fixes.

    python3 tc_reader.py CERT.pdf [--family Pipe] [--ollama URL] [--model qwen2.5vl:7b] [--max-pages 30] [--json]
Standard library + poppler (pdfinfo, pdftotext, pdftoppm). po_reader.parse_answer is used when it is beside this file.
"""
import argparse, base64, glob, json, os, re, shutil, subprocess, sys, tempfile
import model_call                      # every Ollama request goes through here

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import po_reader                       # parse_answer: tolerant JSON (cut-off answers, trailing text)
except Exception:                          # pragma: no cover
    po_reader = None

FEWSHOT_DIR = os.path.join(HERE, "tc_fewshot")
NUM_PREDICT = 12288                                   # 694: long flange certs were cut off at 6144
FAMILIES = ("Pipe", "Flanges", "Fittings", "Plate", "Mixed", "Other")          # 532: Plate

PROMPT = '''You are reading ONE PAGE of a file of steel test certificates bought by Mining Pressure Systems (MPS) or
Ronin, South Africa: pipe, flanges and pipe fittings (elbows/bends, tees, reducers, caps, stub ends).

First decide what the page is (page_type):
- "mtc": a mill test certificate / material test certificate / inspection certificate to EN 10204 (3.1, 3.2, 2.2)
  or ISO 10474 with a chemical composition table (and usually mechanical properties) for one or more heats.
- "mtc_continuation": the next page of a certificate (more items, more heats or the mechanicals) without its own header.
- "inspection_only": an inspection / conformity / quality certificate with NO chemistry table.
- "hydro_report", "packing_list", "bill_of_lading", "invoice", "letter_of_credit", "other".
For any page_type other than mtc / mtc_continuation / inspection_only, return only page_type and legibility and
leave every other field null or empty.

Header fields (as printed; null when not on this page). Where a name is printed in Chinese and English, give the English.
- cert_no: the certificate number ("No.", "Cert No", "Certificate No", "Certificate Reference No", "CERTIFI.NO").
  Never return the label itself or the inspection standard (EN10204-3.1): if the "No." box is empty, cert_no is null.
- material_no: a separate material / product number when printed (ArcelorMittal: "Material No").
- cert_type: "3.1", "3.2", "2.2" or "inspection" (EN10204 3.1B counts as 3.1); it may only be in a remark or footer.
- date_as_written / date_iso: the certificate (issue) date - may be in the footer ("Date of Release", "Filling date
  YEAR 2011 MONTH 11 DAY 25", "日期").
- mill: the manufacturer whose letterhead it is (e.g. "JIANGYIN CITY SEAMLESS STEEL TUBE FACTORY",
  "ZHANGQIU RUIYE MECHANICAL CO., LTD", "SHANGHAI GATEWAY FITTINGS CO., LTD", "ArcelorMittal South Africa Limited").
- supplier: a trader / stockist named as seller or customer when it is not MPS or Ronin (NSTK / Nippon Steel and
  Sumikin Bussan, Kendo, Macsteel, Incledons); null if none.
- customer: the customer / purchaser / consignee as printed.
- order_numbers: EVERY order, contract, PO, invoice, indent and reference number printed anywhere on the page - the
  header, a sales-contract box ("SC No.DF0940/DF0941" -> DF0940 and DF0941), the SHIPPING MARKS
  ("MPS/A26650/K1832/DURBAN" -> A26650) and the footer, including the documentary credit / L/C number
  ("Documentary Credit Number: IL16341ZA0704992"), exactly as printed, each with its label:
  [{"label": "ORDER NO", "value": "DF0906"}, {"label": "Contract No", "value": "A41249"}].
  MPS orders look like A41249, Ronin orders like DF0906, contracts like CNZAN20188GS-1.
- handwritten: numbers written BY HAND in pen at the top of the page, usually top right outside the printed frame:
  {"orders": [each order number as written, such as "DF2843" or "A49797"; a page can have two; leave out any that is
  crossed out], "job": an MPS job number as written, such as "32245E" or "C3124", or null}; "order 68525 job 5435" gives
  orders ["68525"] and job "5435". Printed text never goes here.
- size_unit: "inch" when the item table's size column heading says inches ("Product & Size (Inch)", "NPS", "SIZE (IN)"),
  "mm" when it says mm, DN or NB, else null.
- product: e.g. "SEAMLESS CARBON STEEL PIPE", "FLANGES", "BUTT WELDING FITTINGS".
- standard: the specification(s) as printed ("ASTM A106 B / API 5L X42 PSL-1", "SANS 1123", "ASTM A234 WPB").
- grade: the steel grade (X42, Gr.B, S355JR, WPB, A105, 316L).

items: one entry per PRODUCT line (the item / size / product list - not a test-summary row). A size that continues
on the next row with no new qty is ONE line. A product named in a section row above its sizes ("SGP ELBOW 90D LR",
"SGP TEE") belongs to every size row under it:
  {"line": number|null, "product": "Pipe|Flange|Elbow 90|Elbow 45|Bend|Tee|Reducer|Cap|Stub end|...",
   "description": text as printed, "size_text": size as printed ("219.1*12.7*12.192", "2 1/2\\"", "250NB", "DN50"),
   "size2_text": second size for reducers/tees or null, "od_mm": number|null, "wt_text": wall thickness or schedule as
   printed ("12.7", "SCH80", "STD", "4.2mm"), "class": pressure class as printed ("1000/3", "1600/3", "150#", "PN16",
   "T1000"), "ends": ends / facing ("PE", "BE", "FF", "RF", "PLFF"), "length_m": length of one piece in metres,
   "qty": number of PIECES, "unit": "pcs" (or "m" / "kg" when only that is given), "total_m": total metres|null,
   "weight_kg": weight in kg (a column in tonnes "WT (t) 38.041" = 38041 kg),
   "heat_no": the HEAT / CAST / CHARGE / MELT number (炉号) for this line - never the batch, lot, work or MFG number,
   "batch": a batch / lot / MFG / work number for this line, or null}
  A size string like "8\\"*XXS*9.144" or "14\\"(355.6)*S40(11.13)*12192" is SIZE * WALL * LENGTH: 9.144 / 12192 is
  the length (m / mm), never the wall thickness.
heats: one entry per heat (cast / charge / melt) number with its chemistry and mechanical results. When the chemistry or
mechanical rows are keyed by a line number, lot, batch or specimen number, find that row's heat through the item table
(the same number on the item row) and give the heat as heat_no: a lot, batch, specimen or coil number is never a heat.
  Chemistry: copy the chemistry table's column headings ONCE per page into "chem_columns", left to right exactly as
  printed, one entry per element column: {"el": the element as printed (C, Si, Mn, P, S, Cr, Ni, Mo, Cu, V, Nb, Ti, Al,
  N, B, CE ...), "factor": the multiplier printed with that heading ("x100", "*1000", "X10-3" -> 100, 100, 1000) or
  null}. Mills order the columns differently (C Si Mn P S / C Mn S P Si / C Si Mn S P ...): copy THIS page's order.
  Headings stacked in two rows: the upper row left to right, then the lower row. A row of small numbers under the
  headings with a legend ("2: x10-2, 3: x10-3") gives each column's factor: copy that small number (2, 3) as its factor. Elements may be spelled out (CARBON,
  COLUMBIUM = Nb). Factors given in a legend by marks under the headings ("*2: X1000  *3: X10000  OTHER: X100",
  Sumitomo): each column takes the factor of its mark, and the unmarked columns the OTHER factor.
  Each heat's "chem_row" is its result row read left to right, one value per chem_columns entry, exactly as printed
  (0.004 stays 0.004; 19 under "x100" stays 19), null for a blank or "-" cell. When the table has a heat / ladle /
  melting row (L, R, "Heat") and a product row (P, C, "Product"), give the heat row.
  One mech entry per result row or pair ("(1) Actual", "(2) Actual", "363/364" = two entries). When mechanical rows
  carry no heat number they follow the heats in the order the certificate lists them, including heats listed on
  earlier pages (named below the prompt when there are any).
  When the chemistry table is keyed by an ITEM NO. column and gives a material charge number instead of the item
  table's heat number (Shanghai Gateway: item 101 -> charge 09109615), give heat_no as printed on the chemistry row and
  its item number as "item_line"; the code joins the row to that item.
  {"heat_no": text, "grade": text|null, "item_line": number|null,
   "chem_row": [number|null, ...] - the RESULT row, one value per chem_columns entry, in the same order; skip the
            spec min / max rows,
   "mech": [{"yield": number|null, "tensile": number|null, "unit": "MPa|N/mm2|ksi|psi", "elongation_pct": number|null,
             "hardness": text|null, "impact_j": number|null, "item_line": number|null}]}
If the item table gives the heat number per line, every item heat_no must also appear in heats.

legibility: "good|fair|poor".
Return ONLY this JSON object:
{"page_type": "...", "cert_no": null, "material_no": null, "cert_type": null, "date_as_written": null,
 "date_iso": null, "mill": null, "supplier": null, "customer": null, "order_numbers": [], "product": null,
 "standard": null, "grade": null, "handwritten": {"orders": [], "job": null}, "size_unit": null, "items": [], "chem_columns": [], "heats": [],
 "legibility": "good"}
Copy numbers and codes exactly as printed. Use null when you cannot read a value - do not guess.'''

CERT_TYPES = ("mtc", "mtc_continuation", "inspection_only")

# ------------------------------------------------------------------------------------------------ sizes
NB_LIST = (6, 8, 10, 15, 20, 25, 32, 40, 50, 65, 80, 90, 100, 125, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600,
           650, 700, 750, 800, 850, 900, 950, 1000, 1050, 1100, 1200, 1400, 1500, 1600, 1800, 2000)
INCH_NB = {"1/4": 8, "3/8": 10, "1/2": 15, "3/4": 20, "1": 25, "1 1/4": 32, "1 1/2": 40, "2": 50, "2 1/2": 65, "3": 80,
           "3 1/2": 90, "4": 100, "5": 125, "6": 150, "8": 200, "10": 250, "12": 300, "14": 350, "16": 400, "18": 450,
           "20": 500, "22": 550, "24": 600, "26": 650, "28": 700, "30": 750, "32": 800, "34": 850, "36": 900, "40": 1000,
           "42": 1050, "48": 1200}
# ASME B36.10 outside diameters (mm) -> NB
OD_NB = {21.3: 15, 26.7: 20, 33.4: 25, 42.2: 32, 48.3: 40, 60.3: 50, 73.0: 65, 76.1: 65, 88.9: 80, 101.6: 90, 114.3: 100,
         139.7: 125, 141.3: 125, 165.1: 150, 168.3: 150, 219.1: 200, 273.0: 250, 273.1: 250, 323.9: 300, 355.6: 350,
         406.4: 400, 457.0: 450, 508.0: 500, 559.0: 550, 610.0: 600, 660.0: 650, 711.0: 700, 762.0: 750, 813.0: 800,
         864.0: 850, 914.0: 900, 1016.0: 1000, 1067.0: 1050, 1219.0: 1200}
# ASME B36.10 wall thickness (mm) by NB and schedule name, for naming a printed WT ("9.52" on 125NB -> SCH80/XS)
WT_SCH = {
    15: {"SCH10": 2.11, "SCH40": 2.77, "STD": 2.77, "SCH80": 3.73, "XS": 3.73, "SCH160": 4.78, "XXS": 7.47},
    20: {"SCH10": 2.11, "SCH40": 2.87, "STD": 2.87, "SCH80": 3.91, "XS": 3.91, "SCH160": 5.56, "XXS": 7.82},
    25: {"SCH10": 2.77, "SCH40": 3.38, "STD": 3.38, "SCH80": 4.55, "XS": 4.55, "SCH160": 6.35, "XXS": 9.09},
    32: {"SCH10": 2.77, "SCH40": 3.56, "STD": 3.56, "SCH80": 4.85, "XS": 4.85, "SCH160": 6.35, "XXS": 9.70},
    40: {"SCH10": 2.77, "SCH40": 3.68, "STD": 3.68, "SCH80": 5.08, "XS": 5.08, "SCH160": 7.14, "XXS": 10.15},
    50: {"SCH10": 2.77, "SCH40": 3.91, "STD": 3.91, "SCH80": 5.54, "XS": 5.54, "SCH160": 8.74, "XXS": 11.07},
    65: {"SCH10": 3.05, "SCH40": 5.16, "STD": 5.16, "SCH80": 7.01, "XS": 7.01, "SCH160": 9.53, "XXS": 14.02},
    80: {"SCH10": 3.05, "SCH40": 5.49, "STD": 5.49, "SCH80": 7.62, "XS": 7.62, "SCH160": 11.13, "XXS": 15.24},
    100: {"SCH10": 3.05, "SCH40": 6.02, "STD": 6.02, "SCH80": 8.56, "XS": 8.56, "SCH120": 11.13, "SCH160": 13.49, "XXS": 17.12},
    125: {"SCH10": 3.40, "SCH40": 6.55, "STD": 6.55, "SCH80": 9.53, "XS": 9.53, "SCH120": 12.70, "SCH160": 15.88, "XXS": 19.05},
    150: {"SCH10": 3.40, "SCH40": 7.11, "STD": 7.11, "SCH80": 10.97, "XS": 10.97, "SCH120": 14.27, "SCH160": 18.26, "XXS": 21.95},
    200: {"SCH10": 3.76, "SCH20": 6.35, "SCH30": 7.04, "SCH40": 8.18, "STD": 8.18, "SCH60": 10.31, "SCH80": 12.70,
          "XS": 12.70, "SCH100": 15.09, "SCH120": 18.26, "SCH140": 20.62, "SCH160": 23.01, "XXS": 22.23},
    250: {"SCH10": 4.19, "SCH20": 6.35, "SCH30": 7.80, "SCH40": 9.27, "STD": 9.27, "SCH60": 12.70, "XS": 12.70,
          "SCH80": 15.09, "SCH100": 18.26, "SCH120": 21.44, "SCH140": 25.40, "SCH160": 28.58},
    300: {"SCH10": 4.57, "SCH20": 6.35, "SCH30": 8.38, "STD": 9.53, "SCH40": 10.31, "XS": 12.70, "SCH60": 14.27,
          "SCH80": 17.48, "SCH100": 21.44, "SCH120": 25.40, "SCH140": 28.58, "SCH160": 33.32},
    350: {"SCH10": 6.35, "SCH20": 7.92, "SCH30": 9.53, "STD": 9.53, "SCH40": 11.13, "XS": 12.70, "SCH60": 15.09,
          "SCH80": 19.05, "SCH100": 23.83, "SCH120": 27.79, "SCH140": 31.75, "SCH160": 35.71},
    400: {"SCH10": 6.35, "SCH20": 7.92, "SCH30": 9.53, "STD": 9.53, "SCH40": 12.70, "XS": 12.70, "SCH60": 16.66,
          "SCH80": 21.44, "SCH100": 26.19, "SCH120": 30.96, "SCH140": 36.53, "SCH160": 40.49},
    450: {"SCH10": 6.35, "SCH20": 7.92, "STD": 9.53, "SCH30": 11.13, "XS": 12.70, "SCH40": 14.27, "SCH60": 19.05,
          "SCH80": 23.83, "SCH100": 29.36, "SCH120": 34.93, "SCH140": 39.67, "SCH160": 45.24},
    500: {"SCH10": 6.35, "SCH20": 9.53, "STD": 9.53, "SCH30": 12.70, "XS": 12.70, "SCH40": 15.09, "SCH60": 20.62,
          "SCH80": 26.19, "SCH100": 32.54, "SCH120": 38.10, "SCH140": 44.45, "SCH160": 50.01},
    600: {"SCH10": 6.35, "SCH20": 9.53, "STD": 9.53, "XS": 12.70, "SCH30": 14.27, "SCH40": 17.48, "SCH60": 24.61,
          "SCH80": 30.96, "SCH100": 38.89, "SCH120": 46.02, "SCH140": 52.37, "SCH160": 59.54},
}
_OCR_DIGIT = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "|": "1", "S": "5", "s": "5",
                            "B": "8", "Z": "2", "z": "2", "G": "6"})


def _clean(v):
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip(" .:;,-_")
    return s or None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else float(v)
    s = str(v).replace(" ", "")
    m = re.search(r"-?\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    t = m.group(0)
    if "," in t and "." not in t and re.fullmatch(r"-?\d{1,3},\d{3}", t):
        t = t.replace(",", "")                       # 1,000 (thousands) vs 0,25 (decimal comma)
    x = float(t.replace(",", "."))
    return int(x) if x.is_integer() else x


def _digits_ocr(s):
    """'I00' -> '100', 'SO' -> '50' - only for tokens that are meant to be numbers."""
    return str(s).translate(_OCR_DIGIT)


def nb_from_od(od):
    od = _num(od)
    if not isinstance(od, (int, float)) or od <= 0:
        return None
    best = min(OD_NB, key=lambda k: abs(k - od))
    return OD_NB[best] if abs(best - od) <= max(1.5, best * 0.012) else None


def norm_nb(v):
    """Nominal bore as a standard NB number: '250NB', 'DN250', '10"', '2 1/2"', '2-1/2', 'I00NB', 219.1 (OD) -> NB."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
        if int(x) in NB_LIST and x.is_integer():
            return int(x)
        return nb_from_od(x)
    s = str(v).upper().replace("”", '"').replace("''", '"').strip()
    if re.match(r'\s*[\d /.-]+"\s+[X×*]\s+\d', s):          # 711: 2" x 1 1/2" - the size before the x
        s = re.split(r"[X×*]", s)[0]
    m = re.search(r'(\d+)\s*[- ]\s*(\d)\s*/\s*(\d)\s*(?:"|IN|INCH)', s) or re.search(r'(?<![\d.])(\d)\s*/\s*(\d)\s*(?:"|IN|INCH)', s)
    if m:
        key = f"{m.group(1)} {m.group(2)}/{m.group(3)}" if len(m.groups()) == 3 else f"{m.group(1)}/{m.group(2)}"
        return INCH_NB.get(key)
    m = re.search(r'(?<![\d.])(\d{1,2}(?:\.\d)?)\s*(?:"|IN\b|INCH)', s)
    if m:
        x = float(m.group(1))
        key = str(int(x)) if x.is_integer() else {1.25: "1 1/4", 1.5: "1 1/2", 2.5: "2 1/2", 3.5: "3 1/2"}.get(x)
        return INCH_NB.get(key)
    m = re.search(r"(?:DN\s*)?([0-9OIlSZ]{1,4})\s*(?:MM\s*)?N\.?\s*B\b|DN\s*([0-9OIlSZ]{1,4})\b", s)
    if m:
        n = _num(_digits_ocr(m.group(1) or m.group(2)))
        return int(n) if n in NB_LIST else None
    m = re.search(r"\bN\.?\s*B\.?\s*(\d{1,4})\b", s)    # 694: "NB8" = 8 inch = 200NB (Sumitomo); NB20 = 20NB (Iraeta)
    if m:
        n = int(m.group(1))
        return INCH_NB.get(str(n)) if n <= 18 and str(n) in INCH_NB else (n if n in NB_LIST else None)
    m = re.match(r"\s*(\d{2,4}(?:\.\d+)?)\s*(?:MM)?\s*(?:[X*×]|$)", s)        # '219.1*12.7*12.192' or '141.3'
    if m:
        x = float(m.group(1))
        if x.is_integer() and int(x) in NB_LIST and not re.search(r"[X*×]", s):
            return int(x)
        return nb_from_od(x)
    return None


def norm_sch(v, nb=None):
    """'SCH 80' -> 'SCH80', 'S40'/'40S' -> 'SCH40', '-XS'/'X-STRONG' -> 'XS', '-STD' -> 'STD', '12.7'/'4.2mm' -> '12.7mm'."""
    s = _clean(v)
    if not s:
        return None
    u = s.upper().replace(" ", "")
    u = re.sub(r"^-+", "", u)
    u = re.sub(r"^SCHED(?:ULE)?\.?", "SCH", u)                          # 'SCHED 120', 'SCHED STD'
    u = re.sub(r"^SCH(?=STD|XXS|XS)", "", u)
    if re.fullmatch(r"XXS|XX-?STRONG|XXH", u):
        return "XXS"
    if re.fullmatch(r"XS|X-?STRONG|XH|EXTRASTRONG|XSTG", u):
        return "XS"
    if re.fullmatch(r"STD|STANDARD|STD\.?WT|ST", u):
        return "STD"
    m = re.fullmatch(r"(?:SCH(?:EDULE)?\.?|S)(\d{2,3})S?|(\d{2,3})S", u) or re.match(r"(?:SCH\.?|S)0*(\d{2,3})(?![\d.])()", u)
    if m:                                                               # 'S40(11.13)' -> SCH40
        return "SCH" + (m.group(1) or m.group(2)).lstrip("0")
    m = re.search(r"\(?SCH(?:EDULE)?\.?0*(\d{2,3})\)?", u)
    if m:
        return "SCH" + m.group(1)
    u = u.replace("(", "").replace(")", "")                 # ArcelorMittal: '10.970(mm)', '0.237(")29.200(ft)'
    m = (re.match(r"(?:WT|THK|T=)?(\d{1,2}(?:[.,]\d{1,3})?)(MM|\"|IN(?:CH)?)", u)
         or re.fullmatch(r"(?:WT|THK|T=)?(\d{1,2}(?:[.,]\d{1,3})?)()", u))
    if m:
        x = float(m.group(1).replace(",", "."))
        if m.group(2) and m.group(2) != "MM":
            x = round(x * 25.4, 2)
        return f"{x:g}mm"
    return None


def sch_names_for(nb, wt):
    """Schedule names a printed WT (mm) matches for this NB, e.g. (125, 9.52) -> {'SCH80', 'XS'}."""
    if not nb or nb not in WT_SCH:
        return set()
    m = re.match(r"(\d+(?:\.\d+)?)mm$", str(wt or ""))
    if not m:
        return set()
    x = float(m.group(1))
    return {k for k, v in WT_SCH[nb].items() if abs(v - x) <= 0.16}


def norm_class(v):
    s = _clean(v)
    if not s:
        return None
    u = s.upper().replace(" ", "")
    m = re.fullmatch(r"T(600|1000|1600|2500|4000)", u)                 # SANS 1123 table codes: T1000 = 1000/3
    if m:
        return f"{m.group(1)}/3"
    m = re.fullmatch(r"(?:BS10)?T(?:ABLE)?([D-H])", u)                  # BS 10 Table D / E
    if m:
        return f"Table {m.group(1)}"
    m = re.search(r"(\d{3,4})/(\d)", u)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"(?:CLASS|CL\.?|ASA|ANSI)?(150|300|400|600|900|1500|2500)(?:#|LB|LBS)?\b", u)
    if m and re.search(r"CLASS|CL|ASA|ANSI|#|LB", u):
        return f"ASA{m.group(1)}"
    m = re.search(r"PN\.?(\d{1,3})", u)
    if m:
        return f"PN{m.group(1)}"
    return s


def norm_grade(v):
    s = _clean(v)
    if not s:
        return None
    u = s.upper()
    for rx, out in ((r"S\s*355\s*J\s*R", "S355JR"), (r"S\s*355\s*J\s*2", "S355J2"), (r"S\s*275\s*JR", "S275JR"),
                    (r"\bWPB\b", "WPB"), (r"\bA\s*105\s*N?\b", "A105"), (r"\b316\s*L\b", "316L"), (r"\b304\s*L\b", "304L"),
                    (r"\bX\s*(42|46|52|56|60|65|70)\b", r"X\1"), (r"\bGR\.?\s*B\b|\bGRADE\s*B\b|\bA106\s*B\b|\bA53\s*B\b", "Gr B")):
        m = re.search(rx, u)
        if m:
            return m.expand(out)
    return s


def norm_heat(v):
    """'98B 5582' -> '98B5582'; the steel-making process printed under the heat goes: '1184901 EAF' -> '1184901'."""
    s = _clean(v)
    if not s:
        return None
    s = re.sub(r"(?i)[\s/-]*\(?(?:EAF|BOF|LD|OHF|VD|EF)\)?$", "", s)
    return re.sub(r"[\s.]", "", s).upper().strip("-/") or None


def heat_key(v):
    """Loose key for comparing heat numbers read by OCR / typed by hand: O->0, I/L->1, Z->2, S->5, B->8, no hyphens."""
    h = (norm_heat(v) or "").replace("-", "")
    return h.translate(str.maketrans({"O": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "Q": "0", "D": "0"}))


def norm_orders(values, text=""):
    """-> (mps_pos, ronin_pos, contracts) from read order numbers + any text (file name, text layer)."""
    mps, ronin, contracts = [], [], []
    blob = " ".join(str(x) for x in values if x) + " " + (text or "")
    for m in re.finditer(r"(?<![A-Z0-9])A\s?-?\s?(\d{5})(?!\d)", blob.upper()):
        v = "A" + m.group(1)
        if v not in mps:
            mps.append(v)
    for m in re.finditer(r"(?<![A-Z0-9])DF\s?-?\s?(\d{3,4})(?!\d)", blob.upper()):
        v = "DF" + m.group(1).zfill(4)
        if v not in ronin:
            ronin.append(v)
    for m in re.finditer(r"(?<![A-Z0-9])(CN[A-Z]{2,4}\d{4,6}[A-Z]{0,3}(?:-\d+)?)", blob.upper()):
        if m.group(1) not in contracts:
            contracts.append(m.group(1))
    return mps, ronin, contracts


def parse_date(s):
    import datetime as dt
    s = str(s or "").strip()
    if not s:
        return None
    months = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
    m = re.search(r"(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})", s, re.I)          # Jul.12, 2016
        m2 = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s*[-. ]?\s*([A-Za-z]{3})[a-z]*\.?\s*[-., ]?\s*(\d{2,4})", s, re.I)  # 12-JUL-2016
        if m and m.group(1).upper()[:3] in months:
            mo, d, y = months[m.group(1).upper()[:3]], int(m.group(2)), int(m.group(3))
        elif m2 and m2.group(2).upper()[:3] in months:
            d, mo, y = int(m2.group(1)), months[m2.group(2).upper()[:3]], int(m2.group(3))
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
    return day.isoformat() if 1995 <= day.year <= dt.date.today().year + 1 else None


# ------------------------------------------------------------------------------------------------ PDF helpers
def run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def page_count(path):
    try:
        out = run(["pdfinfo", str(path)], 60).stdout.decode("utf-8", "replace")
        m = re.search(r"^Pages:\s*(\d+)", out, re.M)
        return int(m.group(1)) if m else 1
    except Exception:
        return 1


def page_text(path, page):
    try:
        return run(["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(path), "-"], 120).stdout.decode("utf-8", "replace")
    except Exception:
        return ""


_CERT = re.compile(r"(?i)MILL\s*TEST|MATERIAL\s*TEST|INSPECTION\s*CERTIFICATE|TEST\s*CERTIFICATE|EN\s*10204|ISO\s*10474|"
                   r"QUALITY\s*CERTIFICATE|CERTIFICATE\s*OF\s*(?:QUALITY|PRODUCT|COMPLIANCE)|CHEMICAL\s*COMPOSITION")
_CHEM = re.compile(r"(?i)\bC\b.{0,40}\bSi\b.{0,40}\bMn\b|CHEMICAL\s*COMPOSITION|CHARGE\s*NO")
_PAPERS = [("bill_of_lading", r"BILL\s*OF\s*LADING|\bB/L\s*NO"), ("packing_list", r"PACKING\s*LIST"),
           ("invoice", r"COMMERCIAL\s*INVOICE|TAX\s*INVOICE"), ("letter_of_credit", r"LETTER\s*OF\s*CREDIT|DOCUMENTARY\s*CREDIT\s*NUMBER")]


_TABLE = re.compile(r"(?i)HEAT\s*NO|YIELD|TENSILE|ELONGATION")


def rule_page_type(text):
    """Page type from a text layer when it is plain (shipping papers with no cert wording), else None (ask the model)."""
    t = text or ""
    if len(t.strip()) < 80 or _CERT.search(t) or _TABLE.search(t):
        return None                                     # 553: a chemistry / mechanicals page whose header says
                                                        # "DOCUMENTARY CREDIT NUMBER" (DOC010818-001 p2) is not a L/C
    for kind, rx in _PAPERS:
        if re.search(rx, t, re.I):
            return kind
    return None


# ------------------------------------------------------------------------------------------------ vision model
_FEW = {}


PAGE_ASK = "Read this page and return the JSON object described in the instructions."


def fewshot_msgs(fewshot_dir, family=None, limit=1):
    """Worked examples: <n>_<family>_<what>.json + .png. Up to `limit` of the file's family, else any.
    A "_context" key in the JSON is added to that example's prompt (a continuation page's earlier heats) and is not
    part of the answer. The instructions go once in the system message, so examples carry only a short ask.
    One example by default: with three (23 Oct test) qwen2.5vl:7b overflowed the context (HTTP 500) and copied
    the examples' values into its answers."""
    key = (fewshot_dir, family, limit)
    if key in _FEW:
        return _FEW[key]
    pairs = []
    if fewshot_dir and os.path.isdir(fewshot_dir):
        for j in sorted(glob.glob(os.path.join(fewshot_dir, "*.json"))):
            png = j[:-5] + ".png"
            if os.path.basename(j) != "sources.json" and os.path.exists(png):
                pairs.append((j, png))
    if family:
        same = [p for p in pairs if f"_{family.lower()}_" in os.path.basename(p[0]).lower()]
        pairs = (same or pairs)
    msgs = []
    for j, png in pairs[:limit]:
        ans = json.load(open(j, encoding="utf-8"))
        ctx = ans.pop("_context", None)
        msgs.append({"role": "user", "content": PAGE_ASK + (f"\n\n{ctx}" if ctx else ""),
                     "images": [base64.b64encode(open(png, "rb").read()).decode()]})
        msgs.append({"role": "assistant", "content": json.dumps(ans, ensure_ascii=False)})
    _FEW[key] = msgs
    return msgs


_EXAMPLE_KEYS = {}


def copied_from_example(answer, fewshot_dir, family=None, source_name=""):
    """The example's stem when the answer repeats a worked example's cert number or two of its heat numbers."""
    if not fewshot_dir or not os.path.isdir(fewshot_dir):
        return None
    if fewshot_dir not in _EXAMPLE_KEYS:
        keys = {}
        for j in glob.glob(os.path.join(fewshot_dir, "*_*.json")):
            ex = json.load(open(j, encoding="utf-8"))
            keys[os.path.basename(j)[:-5]] = ({_clean(ex.get("cert_no"))} - {None},
                                              {norm_heat(h.get("heat_no")) for h in ex.get("heats") or []} - {None})
        _EXAMPLE_KEYS[fewshot_dir] = keys
    certs = {_clean(answer.get("cert_no"))} - {None}
    heats = {norm_heat(h.get("heat_no")) for h in answer.get("heats") or [] if isinstance(h, dict)} - {None}
    for stem, (ec, eh) in _EXAMPLE_KEYS[fewshot_dir].items():
        if (certs & ec) or len(heats & eh) >= 2 or (eh and heats and heats <= eh):
            return stem
    return None


def parse_answer(raw):
    import ci_keys                         # keys differing only in case ("Ce"/"CE") break the PC sync (29 Oct)
    return ci_keys.dedupe(_parse_answer(raw))


def _parse_answer(raw):
    if po_reader:
        return po_reader.parse_answer(raw)
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0) if m else raw)


def render(path, page, px, out_png):
    r = run(["pdftoppm", "-png", "-scale-to", str(px), "-f", str(page), "-l", str(page), "-singlefile", str(path),
             out_png[:-4]], 300)
    if r.returncode != 0 or not os.path.exists(out_png):
        # keep pdftoppm's own message: on 23 Oct every page after file 21 failed here with no reason recorded
        raise RuntimeError(f"pdftoppm rc={r.returncode} page {page}: {r.stderr.decode('utf-8', 'replace').strip()[:240]}")
    return out_png


UPRIGHT_MIN_CONF = 2.0


def upright(png):
    """453: rotate a sideways page image upright in place (Tesseract OSD). Returns the degrees turned, 0 if none."""
    try:
        r = run(["tesseract", png, "stdout", "--psm", "0"], 120)
        out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else str(r.stdout or "")
        rot = re.search(r"Rotate:\s*(\d+)", out)
        conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
        deg = int(rot.group(1)) if rot else 0
        if deg not in (90, 180, 270) or not conf or float(conf.group(1)) < UPRIGHT_MIN_CONF:
            return 0
        from PIL import Image
        with Image.open(png) as im:
            im.rotate(-deg, expand=True).save(png)
        return deg
    except Exception:
        return 0


def _good_words(png, deg):
    """518: words Tesseract reads with confidence >= 70 (3+ letters) on the page image turned by deg."""
    from PIL import Image
    q = png[:-4] + f"_o{deg}.png"
    with Image.open(png) as im:
        g = im.convert("L")
        g.thumbnail((1400, 1400))
        (g.rotate(-deg, expand=True) if deg else g).save(q)
    out = run(["tesseract", q, "stdout", "--psm", "6", "tsv"], 120).stdout
    out = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")
    n = 0
    for line in out.splitlines()[1:]:
        c = line.split("\t")
        try:
            if len(c) >= 12 and float(c[10]) >= 70 and re.fullmatch(r"[A-Za-z]{3,}", c[11].strip()):
                n += 1
        except ValueError:
            pass
    return n


def best_turn(path, page):
    """518: the way up with clearly the most well-read words: (degrees, scores). degrees is None when no way is
    clearly best (the reader then keeps its own orientation check)."""
    with tempfile.TemporaryDirectory() as tmp:
        png = render(path, page, 1400, os.path.join(tmp, "w.png"))
        score = {d: _good_words(png, d) for d in (0, 90, 270, 180)}
    best = max(score, key=score.get)
    rest = max(v for d, v in score.items() if d != best)
    return (best if score[best] >= max(12, 1.5 * rest) else None), score


def _read_page_once(path, page, ollama_url, model, fewshot_dir, family=None, px=1600, hint=None, timeout=1200,
                   keep_alive="15m", num_ctx=49152, earlier_heats=None, extra="", turn=None, system=None):   # 49152 = the pipeline readers' value: no model reload
    with tempfile.TemporaryDirectory() as tmp:
        if str(path).lower().endswith(".pdf"):
            png = render(path, page, px, os.path.join(tmp, "p.png"))
        else:
            png = shutil.copy(str(path), os.path.join(tmp, "p" + os.path.splitext(str(path))[1]))
        if turn is None:
            turned = upright(png)                             # 453: sideways scans read upright
        else:                                                 # 518: the way best_turn found
            turned = turn
            if turn:
                from PIL import Image
                with Image.open(png) as im:
                    im.rotate(-turn, expand=True).save(png)
        prompt = PAGE_ASK + (f"\n\nHeats listed on earlier pages of this file, in order: {', '.join(earlier_heats)}"
                             if earlier_heats else "")
        prompt += (f"\n\nThe page's text layer (OCR of the scan - a hint only, digits and letters are often "
                   f"wrong; trust the image):\n{hint[:3000]}" if hint else "")
        prompt += ("\n\n" + extra) if extra else ""
        body = {"model": model, "stream": False, "format": "json", "keep_alive": keep_alive,
                "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": NUM_PREDICT},
                "messages": [{"role": "system", "content": system or PROMPT}] + fewshot_msgs(fewshot_dir, family)
                            + [{"role": "user", "content": prompt, "images": [base64.b64encode(open(png, "rb").read()).decode()]}]}
        raw = model_call.chat_content(ollama_url, body, timeout)
    a = parse_answer(raw)
    if turned and isinstance(a, dict):
        a["turned_upright"] = turned
    return a


def _v2_bad(a):
    """694: a cert page the headings + rows answer failed on: nothing read, heats left out, no heat with usable
    chemistry, or carbon-steel values out of range."""
    if not isinstance(a, dict) or a.get("page_type") not in CERT_TYPES:
        return False
    heats = [h for h in a.get("heats") or [] if isinstance(h, dict)]
    if not heats:
        return not a.get("items") or bool(a.get("chem_columns"))
    chems = [chem_of(h, a) or {} for h in heats]
    if not any(chems):
        return True
    mn = [c["Mn"] for c in chems if isinstance(c.get("Mn"), (int, float)) and not ((c.get("Cr") or 0) > 5 or (c.get("Ni") or 0) > 5)]
    if mn and max(mn) < 0.1:                              # 727: F2000-2503 - C 0.0043 Mn 0.0044 on every row
        return True
    return any(isinstance(c.get(el), (int, float)) and c[el] > CHEM_MAX[el] for c in chems for el in _TIGHT
               if not ((c.get("Cr") or 0) > 5 or (c.get("Ni") or 0) > 5))


def _item_heat_hits(heats, items):
    ik = {heat_key(i.get("heat_no")) for i in items if isinstance(i, dict) and i.get("heat_no")}
    return sum(1 for h in heats if isinstance(h, dict) and heat_key(h.get("heat_no")) in ik)


def _good_chem(heats, ans):
    """710: distinct heats with chemistry and no carbon-steel value out of range (stainless not checked)."""
    n = set()
    for h in heats:
        c = chem_of(h, ans) or {}
        if c and not any(isinstance(c.get(el), (int, float)) and c[el] > CHEM_MAX[el] for el in _TIGHT
                         if not ((c.get("Cr") or 0) > 5 or (c.get("Ni") or 0) > 5)):
            n.add(heat_key(h.get("heat_no")))
    return len(n)


def _hybrid(b, a):
    """710: the 10 Nov answer b with the headings + rows answer a's heats, chem_columns and handwritten numbers."""
    items = [i for i in b.get("items") or [] if isinstance(i, dict)]
    ha = [h for h in a.get("heats") or [] if isinstance(h, dict)]
    hb = [h for h in b.get("heats") or [] if isinstance(h, dict)]
    if _good_chem(hb, b) > _good_chem(ha, a):             # 00014296: the 10 Nov answer had chemistry on more heats
        return None
    if items and _item_heat_hits(ha, items) < _item_heat_hits(hb, items):
        if len(ha) != len(hb):
            return None
        ha = [dict(h, heat_no=g.get("heat_no")) if g.get("heat_no") else h for h, g in zip(ha, hb)]
    ia = [i for i in a.get("items") or [] if isinstance(i, dict)]
    if len(ia) == len(items):                             # 'SGP ELBOW 90D LR' above its size rows: the product per row
        items = [dict(i, product=j.get("product")) if not i.get("product") and j.get("product") and
                 heat_key(i.get("heat_no")) == heat_key(j.get("heat_no")) else i for i, j in zip(items, ia)]
    ha = _rows_from_v1(ha, a, hb)
    return dict(b, items=items, heats=ha, chem_columns=a.get("chem_columns") or [], handwritten=a.get("handwritten") or b.get("handwritten"),
                read_with="10 Nov prompt for header and items; headings + rows for heats and handwriting")


def read_page(*args, **kw):
    """710: a cert page read with both prompts (see _hybrid); 694: the 10 Nov answer whole where the headings + rows
    answer fails (_v2_bad) or is cut off."""
    try:
        b = _read_page_once(*args, **dict(kw, system=PROMPT_V1))
    except ValueError:                                    # json: the answer cut off or garbled
        b = None
    if isinstance(b, dict) and b.get("page_type") not in CERT_TYPES:
        return b
    try:
        a = _read_page_once(*args, **kw)
    except ValueError:
        if b is None:
            raise
        a = None
    if not isinstance(b, dict):
        return a
    if not isinstance(a, dict) or a.get("page_type") not in CERT_TYPES or _v2_bad(a):
        if isinstance(a, dict) and a.get("handwritten") and not b.get("handwritten"):
            b["handwritten"] = a["handwritten"]
        b["read_with"] = "10 Nov prompt (the headings + rows answer " + ("was cut off)" if a is None else "had no usable chemistry)")
        return b
    m = _hybrid(b, a)
    if m is None:
        b["handwritten"] = a.get("handwritten") or b.get("handwritten")
        b["read_with"] = "10 Nov prompt (the headings + rows heats did not match the items or had chemistry on fewer heats)"
        return b
    return m


BNV_NOTES = """FORM NOTES - this is a BNV Piping Supplies / BNV Piping Products certificate. Its layout:
ITEM TABLE, first column headed "Product & Size (Inch)". Each row reads left to right:
  [underlined line number] [size in INCHES: 2, 1 1/2, 3/4, or a reducer 4X 3 / 2X 1 1/2 / 14 x 8]
  [product: TEE, CON RED, ECC RED, CAP, 90 ELBOW LR, 45 ELBOW LR, 90 ELBOW SR - or, for a flange, its standard and
  table such as SABS 1123 1000/3 or BS4504 16/3 followed by the face FF / RF and W/O] [schedule XS / STD / SCH 60, or SGP]
  then the columns Heat ID, Quantity (blank on most certs, absent on older ones), Heat No. (on newer certs the same
  3-digit number as Heat ID, such as 311 or 106; on older certs a longer number such as 54108 - always give the
  Heat No. column), Raw Material Mfg. (C.H.MILL, H.X. MILL).
Give each row as an item: "line" = the underlined line number, "size_text" = the inch size exactly as printed and
nothing else ("2X 1 1/2", or 2.1/2" when it is printed with the inch mark), "product" = the product words ("CON RED", "90 ELBOW LR") or "Flange", "description" = the
flange standard, table and face ("SABS 1123 1000/3 FF W/O") or null, "class" = the flange table ("1000/3") or null,
"wt_text" = the schedule ("XS") or null, "qty" = the Quantity cell (null when blank - never the size or the line number),
"heat_no" = the Heat No. The size_unit is "inch".
CHEMISTRY TABLE below it: the columns C Si Mn P S Cu Ni Cr Mo Others CE, then MIN. and MAX. rows (the specification -
never a result), then ONE result row per line number. Give one heats entry per result row: "item_line" = its line
number, "heat_no" = the Heat No. of that line in the item table, "chem_row" = the row with null for every blank cell
and for "####". Most rows have only C, Si, Mn, P, S. The tension test (yield, tensile, elongation, hardness HB) is
on the same row.
The certificate number is printed after "Certificate No." (BV-AG-...-n); "Job No" and "Indent Nos." are order numbers."""

MINGPIN_NOTES = """FORM NOTES - this is a Hefei Mingpin Metals certificate. The item table (ITEM NO., SIZE in DN, PRODUCTS,
QUANTITY, HEAT NO. ...) runs over several pages. The CHEMICAL COMPOSITION table is keyed by ITEM NO. and its "MATERIAL
CHARGE NO." cells are blank: give one heats entry per result row with "item_line" = its ITEM NO. and "heat_no" null
unless that item's row is on this page (then its HEAT NO.). The factors are printed under the headings (X100, X1000):
copy them into chem_columns. MIN. / MAX. are the specification. YS and TS are headed KSI but are printed in MPa:
give "unit": "MPa"."""

FORMS = (
    {"name": "BNV", "rows_per_line": True, "re": re.compile(r"\bB\s?N\s?V\b|\bBV-[A-Z]{2}-"), "inch": True, "no_qty": True, "items": "v2", "notes": BNV_NOTES},
    {"name": "HEFEI MINGPIN", "rows_per_line": True, "re": re.compile(r"MINGPIN"), "inch": False, "no_qty": False, "items": None, "notes": MINGPIN_NOTES},
)


def _form_of(a):
    """711: the known form a page answer names (mill, supplier or cert number), or None."""
    if not isinstance(a, dict):
        return None
    hit = next((f for f in FORMS if f["name"] == a.get("form")), None)     # a sidecar's fields carry form "test_cert"
    if hit:
        return hit
    t = " | ".join(str(a.get(k) or "") for k in ("mill", "supplier", "cert_no")).upper()
    return next((f for f in FORMS if f["re"].search(t)), None)


def read_page(*args, **kw):
    """711 (replaces the 710 read_page above): a cert page read with both prompts; a known form's notes go with the
    second reading, and for a form marked items "v2" its items are used; 694: the 10 Nov answer whole where the
    headings + rows answer fails (_v2_bad) or is cut off."""
    try:
        b = _read_page_once(*args, **dict(kw, system=PROMPT_V1))
    except ValueError:                                    # json: the answer cut off or garbled
        b = None
    if isinstance(b, dict) and b.get("page_type") not in CERT_TYPES:
        return b
    form = _form_of(b)
    kw2 = dict(kw, extra=((kw.get("extra") or "") + "\n\n" + form["notes"]).strip()) if form else kw
    try:
        a = _read_page_once(*args, **kw2)
    except ValueError:
        if b is None:
            raise
        a = None
    if not isinstance(b, dict):
        return a
    if form:
        b["form"] = form["name"]
    v2_items = bool(form and form["items"] == "v2" and isinstance(a, dict) and [i for i in a.get("items") or [] if isinstance(i, dict)]
                    and _v2_items_ok(a, b))
    if not isinstance(a, dict) or a.get("page_type") not in CERT_TYPES or _v2_bad(a):
        if isinstance(a, dict) and a.get("handwritten") and not b.get("handwritten"):
            b["handwritten"] = a["handwritten"]
        b["read_with"] = "10 Nov prompt (the headings + rows answer " + ("was cut off)" if a is None else "had no usable chemistry)")
        return b                                          # BNV p13: a reading that failed the chemistry dropped a row too
    if v2_items:
        items, heats = _full_heats(_fill_sizes(a["items"], b.get("items")), b.get("items"), a.get("heats") or [])
        return _v1_flags(b, dict(b, items=items, heats=heats, chem_columns=a.get("chem_columns") or [],
                                 handwritten=a.get("handwritten") or b.get("handwritten"), size_unit=a.get("size_unit"),
                                 cert_no=a.get("cert_no") or b.get("cert_no"),
                                 read_with=f"10 Nov prompt for the header; headings + rows with the {form['name']} form notes for items, heats and handwriting"))
    m = _hybrid(b, a)
    if m is None:
        b["handwritten"] = a.get("handwritten") or b.get("handwritten")
        b["read_with"] = "10 Nov prompt (the headings + rows heats did not match the items or had chemistry on fewer heats)"
        return b
    m["size_unit"] = a.get("size_unit")
    return _v1_flags(b, m)


def _rows_from_v1(ha, a, hb):
    """727 (Panjin ZBS2024078066: 10 values under 12 headings, S 0.008 and one 0.002 left out): a headings + rows row
    shorter than its headings pairs from the left, so every value after the first gap is one heading off. Where the
    10 Nov reading of that heat has a value for every heading and the short row is those values with some left out,
    the heat keeps the 10 Nov chemistry."""
    byk = {heat_key(g.get("heat_no")): g for g in hb if g.get("heat_no")}
    out = []
    for n, h in enumerate(ha):
        g = byk.get(heat_key(h.get("heat_no"))) or (hb[n] if len(hb) == len(ha) else None)
        row = h.get("chem_row")
        els = [el for el, _ in _chem_cols(h.get("chem_columns") or a.get("chem_columns")) if el]
        if g and isinstance(row, list) and 5 <= len(row) < len(els) and isinstance(g.get("chem"), dict):
            c1 = {_el(k): _num(v) for k, v in g["chem"].items()}
            full = [c1.get(el) for el in els]
            vals = [_num(v) for v in row]
            it = iter(full)
            if None not in full and None not in vals and all(any(abs(v - x) < 1e-9 for v in it) for x in vals):
                h = dict({k: v for k, v in h.items() if k not in ("chem_row", "chem_columns")}, chem=g["chem"],
                         chem_multiplier=g.get("chem_multiplier") or {})
        out.append(h)
    return out


def _v1_flags(b, m):
    """727: what the 10 Nov reading b of this page lacked, kept on the combined answer m: read() asks a page that
    answered with its type only once more (445) and rescue() takes the upright reading whole (518) as they did before
    the headings + rows reading added heats to such a page (a sideways page: its header is misread)."""
    if not _has_content(b):
        m["v1_empty"] = True
    if not b.get("items") and not b.get("heats"):
        m["v1_no_lines"] = True
    return m


def _heats_even(items):
    """727: the lines whose heat number has the commonest length on the page."""
    ls = [len(k) for k in (heat_key(i.get("heat_no")) for i in items if isinstance(i, dict)) if k]
    return max(ls.count(n) for n in set(ls)) if ls else 0


def _v2_items_ok(a, b):
    """727 (BNV control BV-AG-J2WNG160701A: heats 119 / 183 / 160 / 173 among 54xxx ones, the rows one off): with the
    same number of lines in both readings the form reading's lines are used only if their heat numbers are as even in
    length as the 10 Nov reading's."""
    ia = [i for i in a.get("items") or [] if isinstance(i, dict)]
    ib = [i for i in b.get("items") or [] if isinstance(i, dict)]
    return len(ia) != len(ib) or _heats_even(ia) >= _heats_even(ib)


def _full_heats(items, other, heats):
    """711: older BNV certs print a short Heat ID (108) beside the full Heat No. (54108): where the other reading's line
    has a longer heat number ending in the form reading's, the longer one is the heat (items and chemistry rows)."""
    other = [i for i in other or [] if isinstance(i, dict)]
    byline = {int(_num(o.get("line"))): o for o in other if _num(o.get("line")) is not None}
    ren = {}
    for k, i in enumerate(items):
        ln = _num(i.get("line"))
        o = byline.get(int(ln)) if ln is not None else (other[k] if len(other) == len(items) else None)
        h, ho = heat_key(i.get("heat_no")), heat_key((o or {}).get("heat_no"))
        if h and ho and len(ho) > len(h) and ho.endswith(h):
            ren[h] = o["heat_no"]
            i["heat_no"] = o["heat_no"]
    heats = [dict(h, heat_no=ren[heat_key(h.get("heat_no"))]) if isinstance(h, dict) and heat_key(h.get("heat_no")) in ren else h
             for h in heats]
    return items, heats


def _fill_sizes(items, other):
    """711: form-reading item lines with no size (BNV p8 / p12: the model left the size out on flange pages) take the
    other reading's size of the same line - its size text, or the quantity when the size went there."""
    items = [dict(i) for i in items if isinstance(i, dict)]
    other = [i for i in other or [] if isinstance(i, dict)]
    byline = {int(_num(o.get("line"))): o for o in other if _num(o.get("line")) is not None}
    qs = [_num(o.get("qty")) for o in other]
    qty_is_size = len(other) > 2 and len(set(qs)) > len(other) // 2       # BNV p8: qty 1 on every line is no size
    for k, i in enumerate(items):
        if str(i.get("size_text") or "").strip() and not _FLANGE_STD.fullmatch(str(i.get("size_text")).strip()) \
                and not re.fullmatch(r"\s*\d{3,4}\s*/\s*\d\s*", str(i.get("size_text"))):
            continue
        ln = _num(i.get("line"))
        o = byline.get(int(ln)) if ln is not None else None
        if o is None and len(other) == len(items):
            o = other[k]
        if not o:
            continue
        st = str(o.get("size_text") or "").strip()
        q = _num(o.get("qty"))
        if _BARE_SIZE.fullmatch(st) and _inch_key(_BARE_SIZE.fullmatch(st).group(1)) in INCH_NB:
            i["size_text"] = st
        elif qty_is_size and q is not None and float(q).is_integer() and str(int(q)) in INCH_NB:
            i["size_text"] = str(int(q))
    return items


_BARE_IN = r"(\d{1,2}\s+\d\s*/\s*\d|\d\s*/\s*\d|\d{1,2})"
_BARE_SIZE = re.compile(rf"\s*{_BARE_IN}\s*(?:[Xx×*]\s*{_BARE_IN})?\s*")
_FLANGE_STD = re.compile(r"(SA[BN]S\s*1123|BS\s*4504|BS\s*10\b|B\s*16\.5)\s*(?:T(?:ABLE)?\s*)?([0-9]{1,4}\s*/\s*\d|[D-H]\b)?", re.I)


def _inch_key(s):
    return re.sub(r"\s*/\s*", "/", re.sub(r"\s+", " ", str(s).strip()))


def _form_rules(a):
    """711: a page answer with its form's rules applied: bare sizes in inches, BNV's blank quantity, flange lines."""
    if not isinstance(a, dict):
        return a
    form = _form_of(a)
    items = [dict(i) for i in a.get("items") or [] if isinstance(i, dict)]
    if not items:
        return a
    bnv = bool(form and form["name"] == "BNV")
    if form and form["no_qty"]:
        qs = [_num(i.get("qty")) for i in items]
        same = len(items) > 1 and len({q for q in qs}) == 1
        for i, q in zip(items, qs):
            if q is None:
                continue
            asize = str(int(q)) if float(q).is_integer() else None
            bare = _BARE_SIZE.fullmatch(str(i.get("size_text") or ""))
            if not bare and asize in INCH_NB and not norm_nb(i.get("size_text")):
                i["description"] = i.get("description") or i.get("size_text")     # the size was read into the quantity
                i["size_text"], i["qty"] = asize, None
            elif same or (bare and _inch_key(bare.group(1)) == asize):
                i["qty"] = None
    inch = bool(form and form["inch"])
    if not inch and str(a.get("size_unit") or "").lower().startswith("in"):
        bare = [_BARE_SIZE.fullmatch(str(i.get("size_text") or "")) for i in items]
        keys = [_inch_key(g) for m in bare if m for g in m.groups() if g]
        inch = bool(keys) and all(k in INCH_NB for k in keys) and any(not k.isdigit() or int(k) not in NB_LIST for k in keys)
    for i in items:
        t = " ".join(str(i.get(k) or "") for k in ("product", "description", "size_text"))
        m = _FLANGE_STD.search(t)
        if bnv and m:                                     # SABS 1123 1000/3 FF W/O: a flange, its table the class
            if not family_of_product(i.get("product"), None) == "Flanges":
                i["description"] = i.get("description") or i.get("product")
                i["product"] = "Flange"
            if m.group(2) and not i.get("class"):
                i["class"] = re.sub(r"\s", "", m.group(2)) if "/" in m.group(2) else f"Table {m.group(2).upper()}"
        if not inch:
            continue
        m = _BARE_SIZE.fullmatch(str(i.get("size_text") or ""))
        if not m or _inch_key(m.group(1)) not in INCH_NB:
            continue
        a1, a2 = _inch_key(m.group(1)), (_inch_key(m.group(2)) if m.group(2) else None)
        if a2 and a2 not in INCH_NB:
            continue
        i["size_text"] = f'{a1}"' + (f' x {a2}"' if a2 else "")
        if a2 and not i.get("size2_text"):
            i["size2_text"] = f'{a2}"'
    return dict(a, items=items)


def _vote_rows(a):
    """711: a form with one chemistry row per item line (BNV, Hefei Mingpin): the rows of one heat are one chemistry -
    each element takes the value most of that heat's rows give (the first on a tie)."""
    form = _form_of(a)
    heats = [h for h in a.get("heats") or [] if isinstance(h, dict)]
    if not form or not form.get("rows_per_line") or len(heats) < 2:
        return a
    groups, order = {}, []
    for h in heats:
        k = heat_key(h.get("heat_no"))
        if k not in groups:
            order.append(k)
        groups.setdefault(k, []).append(h)
    if all(len(g) == 1 for g in groups.values()):
        return a
    out = []
    for k in order:
        g = groups[k]
        chems = [chem_of(h, a) or {} for h in g]
        voted = {}
        for el in [e for c in chems for e in c]:
            if el in voted:
                continue
            vals = [c[el] for c in chems if c.get(el) is not None]
            if len(vals) * 2 >= len(g) or len(g) == 1:    # an element on under half the rows is a stray cell
                voted[el] = max(vals, key=lambda v: (vals.count(v), -vals.index(v)))
        first = {x: v for x, v in g[0].items() if x not in ("chem_row", "chem_columns", "chem_multiplier")}
        out.append(dict(first, chem=voted, mech=[m for h in g for m in (h.get("mech") or []) if isinstance(m, dict)] or g[0].get("mech") or []))
    return dict(a, heats=out)


def _link_cert(cert_pages):
    """711: chemistry rows keyed by item number whose item rows are on another page of the cert (Hefei Mingpin: items
    1 - 45 on page 1, their chemistry on page 4) take that item's heat number."""
    byline = {}
    for p in cert_pages:
        for i in (p["answer"] or {}).get("items") or []:
            ln = _num(i.get("line")) if isinstance(i, dict) else None
            if ln is not None and i.get("heat_no"):
                byline.setdefault(int(ln), i["heat_no"])
    if not byline:
        return cert_pages
    known = {heat_key(v) for v in byline.values()}
    out = []
    for p in cert_pages:
        a = p["answer"] or {}
        heats = []
        for h in a.get("heats") or []:
            ln = _num(h.get("item_line")) if isinstance(h, dict) else None
            if ln is not None and int(ln) in byline and (not h.get("heat_no") or heat_key(h.get("heat_no")) not in known):
                h = dict(h, heat_no=byline[int(ln)], charge_no=h.get("heat_no") or None)
            heats.append(h)
        out.append(dict(p, answer=dict(a, heats=heats)))
    return out


REREAD_EMPTY = ('Your answer for this page was empty, but it is a certificate (a page with a chemical composition table '
                'is an mtc, even when it is titled "INSPECTION CERTIFICATE"). Read the whole page: the header fields, every '
                'order number, every item line with its heat number ("Heat No.", "Fitting Heat No."), and the chemistry / '
                'mechanical results. MPS often writes its job number and order number by hand at the top ("JOB 4122 A49797"): '
                'add "handwritten": {"job": the job number as written or null, "order": the order number as written or null}.')


RESCUE_ASK = ('This page is a test certificate, and your first answer for it had no item lines or no heat numbers. Read '
              'it again carefully. The items may be in a product or dimensions block rather than a table (outside '
              'diameter x wall thickness, length, quantity / pieces, weight), or in a table that has the chemistry '
              'columns (C, Si, Mn, P, S ...) on the same row. A page titled "INSPECTION CERTIFICATE" or "CERTIFICATE OF '
              'INSPECTION" that has chemistry is an mtc. Return every item line with its size, quantity and heat number '
              '("Heat No.", "Cast No.", "Charge No."), and every heat with its chemistry and mechanical results.')
RESCUE_PX = 2400
_HEADER = ("cert_no", "material_no", "cert_type", "date_as_written", "date_iso", "mill", "supplier", "customer",
           "order_numbers", "standard")


def _found(a):
    a = a if isinstance(a, dict) else {}
    return len(a.get("items") or []) + len(a.get("heats") or [])


def rescue(path, pages, ollama_url, model, family=None, timeout=1200, keep_alive="15m", log=None):
    """518: a second look at a file that read with no item lines or no heats (see tc_rescue_patch.py). Returns the
    number of pages read again."""
    if not str(path).lower().endswith(".pdf"):
        return 0
    got = [q for q in pages if q.get("page") and isinstance(q.get("answer"), dict)]
    cert = [q["answer"] for q in got if q["answer"].get("page_type") in CERT_TYPES]
    if cert and sum(len(a.get("items") or []) for a in cert) and sum(len(a.get("heats") or []) for a in cert):
        return 0
    n = 0
    for q in got:
        a = q["answer"]
        if a.get("page_type") in CERT_TYPES:
            if a.get("items") and a.get("heats"):
                continue
        elif not (q.get("how") == "text_rules" and a.get("page_type") == "letter_of_credit" and not cert):
            continue
        n += 1
        try:
            deg, score = best_turn(path, q["page"])
            text = page_text(path, q["page"])
            hint = text if len(text.strip()) > 80 else None
            a2 = read_page(path, q["page"], ollama_url, model, "", family, RESCUE_PX, hint, timeout, keep_alive,
                           extra=RESCUE_ASK, turn=deg)
            way = "osd" if deg is None else deg
            if isinstance(a2, dict) and a2.get("page_type") in CERT_TYPES:
                if a.get("page_type") in CERT_TYPES and (a.get("items") or a.get("heats")) and not a.get("v1_no_lines"):
                    # 528: the first reading had items or heats: keep it, take only what it lacked (518's test: a
                    # whole second reading found the heats but changed a right pipe size)
                    a2 = dict(a, items=a.get("items") or a2.get("items") or [], heats=a.get("heats") or a2.get("heats") or [])
                else:
                    for k in _HEADER:
                        if not a2.get(k) and a.get(k):
                            a2[k] = a[k]
            if isinstance(a2, dict) and a2.get("page_type") in CERT_TYPES and _found(a2) > _found(a):
                q["answer"], q["how"] = a2, q["how"] + f"+rescue (turn {way}, {RESCUE_PX} px)"
            else:
                q["how"] += f"+rescue tried (turn {way}: no more found)"
        except Exception as e:
            q["how"] += f"+rescue failed ({str(e)[:120]})"
        if log:
            log(f"  p{q['page']}: {q['how']} {q['answer'].get('page_type')}")
    return n


def _has_content(a):
    a = a or {}
    return any(a.get(k) for k in ("cert_no", "mill", "items", "heats", "order_numbers", "customer", "standard"))


def read(path, ollama_url, model="qwen2.5vl:7b", fewshot_dir=None, family=None, max_pages=30, px=1600, chem_px=2000,
         timeout=1200, keep_alive="15m", log=None):
    """Read every page of path. Returns (pages, how): pages = [{"page", "how", "answer"}]."""
    n = page_count(path) if str(path).lower().endswith(".pdf") else 1
    pages = []
    for p in range(1, min(n, max_pages) + 1):
        text = page_text(path, p) if str(path).lower().endswith(".pdf") else ""
        kind = rule_page_type(text)
        if kind:
            pages.append({"page": p, "how": "text_rules", "answer": {"page_type": kind}})
            continue
        hint = text if len(text.strip()) > 80 else None
        earlier = list(dict.fromkeys(h for q in pages for h in
                                     [norm_heat(i.get("heat_no")) for i in (q["answer"].get("items") or []) if isinstance(i, dict)]
                                     + [norm_heat(x.get("heat_no")) for x in (q["answer"].get("heats") or []) if isinstance(x, dict)]
                                     if h))
        few = FEWSHOT_DIR if fewshot_dir is None else fewshot_dir
        how = "vision" + ("+text_hint" if hint else "")
        try:
            try:
                a = read_page(path, p, ollama_url, model, few, family, chem_px if (hint and _CHEM.search(hint)) else px,
                              hint, timeout, keep_alive, earlier_heats=earlier)
                copied = copied_from_example(a, few, family, source_name=os.path.basename(str(path)))
                if copied:
                    raise ValueError(f"answer copies worked example {copied}")
            except Exception as e:                  # Ollama 500 (context full), cut-off JSON or a copied example:
                first = str(e)[:200]                # once more without worked examples, at the normal size
                a = read_page(path, p, ollama_url, model, "", family, px, hint, timeout, keep_alive, earlier_heats=earlier)
                how += f"+retry ({first})"
            if (a or {}).get("page_type") in CERT_TYPES and (not _has_content(a) or a.get("v1_empty")):
                # 445: a certificate page answered with its type only (DOC280117-007): asked once more
                try:
                    a2 = read_page(path, p, ollama_url, model, "", family, px, hint, timeout, keep_alive,
                                   earlier_heats=earlier, extra=REREAD_EMPTY)
                    if _has_content(a2) and not a2.get("v1_empty"):
                        a, how = a2, how + "+reread (empty answer)"
                except Exception:
                    pass
            pages.append({"page": p, "how": how, "answer": a})
        except Exception as e:                      # one bad page does not lose the file
            pages.append({"page": p, "how": "error", "answer": {"page_type": "error"}, "error": str(e)[:300]})
        if log:
            log(f"  p{p}: {pages[-1]['how']} {pages[-1]['answer'].get('page_type')}")
    rescue(path, pages, ollama_url, model, family, timeout, keep_alive, log)    # 518: a file with no items / heats
    if n > max_pages:
        pages.append({"page": None, "how": "skipped", "answer": {"page_type": "not_read"}, "error": f"pages {max_pages + 1}-{n} not read"})
    return pages, n


# ------------------------------------------------------------------------------------------------ answer -> fields
_FAM_FLG = re.compile(r"FLANGE|\bPLFF\b|\bSORF\b|\bWNRF\b|\bBLRF\b|BLIND|\bW/N\b|\bWN\b|\bS/O\b|WELD\s*NECK|SLIP\s*ON|"
                      r"\bFLP\b|\b(?:WO|BLO|SO|WN|BL)FF\b|SANS\s*1123|BS\s*4504|EN\s*1092|STEEL\s*RINGS?\b|\bCOLLARS?\b|\bFFWD\b|\bFFBLD\b|\bBS\s*10\b")
_FAM_FIT = re.compile(r"ELBOW|BEND|\bTEES?\b|REDUCER|\bCAP\b|STUB|FITTING|\bLR\b|\bSR\b|90\s*DEG|45\s*DEG|\bELB(?:LR|SR)?\b|"
                      r"\b(?:CON|ECC)\.?\s*RED\b|\bRED\.?\s*(?:CON|ECC)\b|\bREDUC(?:ING|TION)\b|COUPLING|OLET\b|NIPPLE")
_FAM_PIPE = re.compile(r"PIPE|TUBE|SMLS|SEAMLESS|ERW|LSAW")


def family_of_product(p, description=""):
    t = f"{p or ''} {description or ''}".upper()          # 531: + the shorthand on MPS certs (see tc_family_patch.py)
    if _FAM_FLG.search(t):
        return "Flanges"
    if _FAM_FIT.search(t):
        return "Fittings"
    if _FAM_PIPE.search(t):
        return "Pipe"
    if _FAM_PLATE.search(t):
        return "Plate"                                     # 532
    return None


_FAM_PLATE = re.compile(r"PLATE|中厚板|钢板")


def plate_of_size(size_text):
    """532: "16*2400*12000", "04000 X 2000 X 080.000": thickness x width x length with a round plate width -> Plate.
    A pipe's OD x WT x length is not (1016 x 10 x 11800: 1016 is no round width)."""
    m = _PLATE_SIZE.match(str(size_text or "").upper())
    xs = sorted(float(x) for x in m.groups()) if m else []
    if len(xs) == 3 and xs[0] <= 300 and 900 <= xs[1] <= 3500 and xs[1] % 50 == 0 and xs[2] >= xs[1]:
        return "Plate"
    return None


_PLATE_SIZE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(?:MM)?\s*[A-Z]?\s*[X×*-]\s*(\d+(?:\.\d+)?)\s*(?:MM)?\s*[X×*-]\s*(\d+(?:\.\d+)?)\s*(?:MM|M)?\s*$")


def item_family(it, family_hint=None):
    """532: an item line's family: its words; a plate size makes a line Plate unless its description says pipe (the
    reader often calls a plate line "Pipe"); else the old library's family."""
    pf, ps = family_of_product(it.get("product"), it.get("description")), plate_of_size(it.get("size_text"))
    if ps and pf in (None, "Pipe") and not _FAM_PIPE.search(str(it.get("description") or "").upper()):
        return ps
    return pf or (family_hint if family_hint in ("Pipe", "Flanges", "Fittings") else None)


_SPEC_FAM = (("Pipe", re.compile(r"A\s*-?\s*(?:106|53|333|312|790)\b|API\s*-?\s*5L|\b5L\b|SANS\s*(?:62|719|657)\b|"
                                 r"EN\s*1021[0679]\b|EN\s*1025[5]\b|EN\s*10208\b|BS\s*(?:1387|3601|3602)\b")),
             ("Fittings", re.compile(r"A\s*-?\s*(?:234|403|420)\b|\bWP[BLC]\b|B\s*16\.(?:9|11|28)\b|MSS\s*-?\s*SP|EN\s*10253\b")),
             ("Flanges", re.compile(r"B\s*16\.(?:5|47)\b|SANS\s*1123|BS\s*4504|EN\s*1092|\bBS\s*10\b")))


_NOT_PFF = re.compile(r"PLATE|COIL|SHEET|STRIP|\bBARS?\b|\bROD\b|BEAM|ANGLE|CHANNEL|BILLET|中厚板|钢板|卷")


def family_of_spec(*texts, items=()):
    """531: the family a cert's standard / grade names (A106 / API 5L pipe, A234 fittings, B16.5 flanges), if one; none
    for a cert of plate, coil, bar ... (X42 coil is not pipe)."""
    if any(_NOT_PFF.search(f"{i.get('product') or ''} {i.get('description') or ''}".upper()) for i in items if isinstance(i, dict)):
        return None
    t = " ".join(str(x) for x in texts if x).upper().replace("ASTM", " ")
    fams = {fam for fam, rx in _SPEC_FAM if rx.search(t)}
    return fams.pop() if len(fams) == 1 else None


# upper end of a plausible result (%), to tell a printed "19" under "C *100" (0.19) from a printed 0.20 under it
CHEM_MAX = {"C": 0.6, "Si": 1.5, "Mn": 2.5, "P": 0.1, "S": 0.1, "Cu": 1.0, "Ni": 30, "Cr": 30, "Mo": 5, "V": 0.5,
            "Nb": 0.2, "Ti": 0.5, "Al": 0.2, "N": 0.05, "B": 0.01, "Ceq": 1.0}


_EL_NAMES = {"CARBON": "C", "SILICON": "Si", "MANGANESE": "Mn", "PHOSPHORUS": "P", "PHOSPHOROUS": "P", "SULFUR": "S", "SULPHUR": "S",
             "CHROMIUM": "Cr", "NICKEL": "Ni", "MOLYBDENUM": "Mo", "COPPER": "Cu", "VANADIUM": "V", "NIOBIUM": "Nb", "COLUMBIUM": "Nb",
             "TITANIUM": "Ti", "ALUMINIUM": "Al", "ALUMINUM": "Al", "NITROGEN": "N", "BORON": "B"}


def _el(k):
    k = re.sub(r"[^A-Za-z]", "", str(k))
    if k.upper() in ("CE", "CEQ", "CEV", "CEIIW"):
        return "Ceq"
    if len(k) > 4:                                        # 727: Jinan Rich 19066-2 - CARBON, SILICON, PHOSPHORU
        import difflib
        hit = difflib.get_close_matches(k.upper(), list(_EL_NAMES), n=3, cutoff=0.85)
        if hit and len({_EL_NAMES[x] for x in hit}) == 1:
            return _EL_NAMES[hit[0]]
    return k.capitalize() if len(k) <= 2 else k


def _chem(h):
    """Result row -> % values. A column factor ("*100") is applied only when the printed number is out of range
    without it and in range with it (some mills print 0.20 under a "*100" heading)."""
    mult = h.get("chem_multiplier") or {}
    mult = {_el(k): _num(v) for k, v in mult.items()} if isinstance(mult, dict) else {}
    out = {}
    for k, v in (h.get("chem") or {}).items():
        x = _num(v)
        if x is None:
            continue
        el = _el(k)
        f, mx = mult.get(el), CHEM_MAX.get(el)
        if f and f > 1 and mx and x > mx and x / f <= mx:
            x = round(x / f, 5)
        out[el] = x
    return out


_TIGHT = ("C", "Si", "Mn", "P", "S")
_SUMI = re.compile(r"SUMITOMO|SUMIKIN")                  # 701
_SUMI_F = {"P": 1000, "S": 1000, "Nb": 1000, "B": 10000}


def _sumitomo_heats(a):
    """701: Sumitomo heat numbers are J and a digit (J0K4024, J0LB700): an O read for that 0 is the 0."""
    if not isinstance(a, dict) or not _SUMI.search(str(a.get("mill") or "").upper()):
        return a
    fix = lambda v: re.sub(r"^JO(?=[A-Z0-9])", "J0", str(v).strip().upper()) if v else v
    return dict(a, heats=[dict(h, heat_no=fix(h.get("heat_no"))) if isinstance(h, dict) else h for h in a.get("heats") or []],
                items=[dict(i, heat_no=fix(i.get("heat_no"))) if isinstance(i, dict) else i for i in a.get("items") or []])


def _chem_cols(v):
    """553: chem_columns -> [(element, factor)]; an entry may be a plain heading ("Mn", "P x1000")."""
    out = []
    for c in v if isinstance(v, list) else []:
        el, f = (c.get("el") or c.get("element"), c.get("factor")) if isinstance(c, dict) else (c, None)
        el = str(el or "")
        if f is None:
            m0 = re.search(r"[x×*]\s*(10+)\b|10\s*-\s*([234])", el, re.I)
            f = (m0.group(1) or 10 ** int(m0.group(2))) if m0 else None
            el = re.split(r"[x×*(]|10\s*-", el, flags=re.I)[0] if m0 else el
        m = re.search(r"\d+", str(f or ""))
        k = _el(el) if re.search(r"[A-Za-z]", el) and not ("+" in el or re.fullmatch(r"\*?\s*[UV]\d", el.strip())) else None
        n = int(m.group()) if m else 0
        out.append((k, 10 ** n if 2 <= n <= 6 else n if n >= 10 else None))  # a factor code under the heading: 2 = x100, 3 = x1000; 1 = none (694)
    return out


_FILL_MAX = {"Nb": 0.25, "Ti": 0.25, "V": 0.25, "Al": 0.3, "N": 0.05, "B": 0.02}
_RESID = ("Cr", "Ni", "Mo", "Cu", "V", "Nb", "Ti", "Al", "N", "B", "Ceq", "CE", "Others")


def _drop_fill(c, a=None):
    """711: values the model made up for blank cells: one value on 3 or more residual elements where it is impossible
    for one of them (Nb 0.45), and on a BNV page the MAX. row (Cu = Ni = Cr) read into the result."""
    if not isinstance(c, dict) or not c:
        return c
    c = {k: v for k, v in c.items() if k != "Others"}
    if any(isinstance(c.get(el), (int, float)) and c[el] > CHEM_MAX[el] for el in _TIGHT):
        return c                                          # a row still in printed whole numbers: a factor question, not a fill
    by = {}
    for el in _RESID:
        if isinstance(c.get(el), (int, float)) and 0 < c[el] < 1:
            by.setdefault(c[el], []).append(el)
    for v, els in by.items():
        if len(els) >= 3 and any(v > _FILL_MAX[el] for el in els if el in _FILL_MAX):
            c = {k: x for k, x in c.items() if k not in els}
    form = _form_of(a) if a else None
    if form and form["name"] == "BNV" and c.get("Cu") is not None and c.get("Cu") == c.get("Ni") and c["Cu"] >= 0.3:   # MAX. Cu 0.4 Ni 0.4
        c = {k: x for k, x in c.items() if k not in _RESID}
    if form and form["name"] == "BNV" and not any(c.get(el) is not None for el in ("Cu", "Ni", "Cr", "Mo")):
        c = {k: x for k, x in c.items() if k not in ("Ceq", "CE")}
    return c


def chem_of(h, a=None):
    return _drop_fill(_chem_of711(h, a), a)


def _chem_of711(h, a=None):
    """553: a heat's chemistry in %: its chem_row paired with the page's (or its own) chem_columns; else the old
    "chem" dict. None when the row and the headings differ in count and there is no dict."""
    cols = _chem_cols(h.get("chem_columns") or (a or {}).get("chem_columns"))
    row = h.get("chem_row")
    if cols and isinstance(row, list):
        if len(row) > len(cols) or len(row) < min(5, len(cols)):
            return _chem(h) if h.get("chem") else None    # 694: a shorter row (blank cells at the end) pairs from the left
        raw = [(el, _num(v), f) for (el, f), v in zip(cols, row) if el]
        raw = [(el, x, f) for el, x, f in raw if x is not None]
        if _SUMI.search(str((a or {}).get("mill") or "").upper()) and raw and all(float(x).is_integer() for _, x, _ in raw):
            raw = [(el, x, _SUMI_F.get(el, 100)) for el, x, f in raw]    # 701: the mill's legend, whatever was read
        unf = [(el, x) for el, x, f in raw if not f and el in _TIGHT and x > CHEM_MAX[el] and x >= 1 and float(x).is_integer()]
        if unf and all(x / 100 <= CHEM_MAX[el] for el, x in unf):    # 694: C 19 under no factor - the "OTHER: X100" columns
            raw = [(el, x, f or 100) for el, x, f in raw]
        whole = any(f and el in _TIGHT and x > CHEM_MAX[el] for el, x, f in raw)
        out = {}
        for el, x, f in raw:
            mx = CHEM_MAX.get(el)
            if f and (whole or (mx and x > mx and x / f <= mx)):
                x = round(x / f, 6)
            out.setdefault(el, x)
        return out
    return _chem(h)


def _mpa(v, unit):
    x = _num(v)
    if x is None:
        return None
    u = (unit or "MPa").upper()
    if "KSI" in u:
        return round(x * 6.894757) if x <= 200 else x     # 711: 325 "KSI" is MPa as printed
    if "PSI" in u:
        return round(x * 0.006894757)
    return x


_PEN = str.maketrans({"C": "0", "O": "0", "Q": "0", "D": "0", "U": "0", "Z": "2", "I": "1", "L": "1", "T": "7", "S": "5", "B": "8", "G": "6"})


def _hand_order(v):
    """641: a handwritten order number, pen letters read as digits fixed (as tc_copies): "DFZ866" -> "DF2866"."""
    s = re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
    m = re.fullmatch(r"[DUO]?F(\w{4,5})", s)
    if m and re.fullmatch(r"\d{4,5}", m.group(1).translate(_PEN)):
        return "DF" + m.group(1).translate(_PEN)
    m = re.fullmatch(r"A(\w{5})", s)
    if m and re.fullmatch(r"\d{5}", m.group(1).translate(_PEN)):
        return "A" + m.group(1).translate(_PEN)
    return str(v or "").strip()


def _link_items(a):
    """642: chemistry rows keyed by item number (Shanghai Gateway) -> rows under the item lines' heat numbers."""
    items = [i for i in a.get("items") or [] if isinstance(i, dict)]
    heats = [h for h in a.get("heats") or [] if isinstance(h, dict)]
    ih = {heat_key(i.get("heat_no")) for i in items if i.get("heat_no")}
    if not items or not heats or not ih or any(heat_key(h.get("heat_no")) in ih for h in heats):
        return a
    byline = {}
    for i in items:
        ln = _num(i.get("line"))
        if ln is not None and i.get("heat_no"):
            byline.setdefault(int(ln), i["heat_no"])
    pairs = [byline.get(int(_num(h.get("item_line")))) if _num(h.get("item_line")) is not None else None for h in heats]
    if not all(pairs):                                    # the charge carries the item heat: "16CT12181/16BS712", "7102352163" for 163
        iks = [(heat_key(i.get("heat_no")), i["heat_no"]) for i in items if i.get("heat_no")]
        def match(hn):
            k, first = heat_key(hn), heat_key(str(hn).split("/")[0])
            return next((v for ik, v in iks if ik == first), None) or                 next((v for ik, v in sorted(iks, key=lambda x: -len(x[0])) if len(ik) >= 3 and k.endswith(ik)), None)
        spec = lambda hn: re.search(r"ASTM|ASME|SPEC|MIN|MAX|\bEN\s?10", str(hn or "").upper())
        got = [match(h.get("heat_no")) for h in heats]
        if any(got) and all(g or spec(h.get("heat_no")) for g, h in zip(got, heats)):
            pairs = [g or h.get("heat_no") for g, h in zip(got, heats)]   # a spec row read as a heat stays as it was
        else:
            pairs = got
    if not all(pairs):                                    # rows in item order (Gateway, Double-Peace, Longgang forms)
        mill = str(a.get("mill") or "").upper()
        known = any(m in mill for m in ("GATEWAY", "PEACE", "LONGGANG"))
        rownos = all(re.fullmatch(r"\d{1,3}", str(h.get("heat_no") or "").strip()) for h in heats)
        if not ((known or rownos) and len(heats) == len(items)):
            return a
        pairs = [i.get("heat_no") for i in items]
    return dict(a, heats=[dict(h, heat_no=hn, charge_no=h.get("heat_no")) if hn != h.get("heat_no") else h for h, hn in zip(heats, pairs)])


def _drop_repeats(cert_pages):
    """641: a page whose cert number, item lines and heats repeat an earlier page's is another scan of the same paper."""
    seen, loose, keep, repeats = set(), [], [], []
    for p in cert_pages:
        a = p["answer"] or {}
        its = tuple(sorted((heat_key(i.get("heat_no")), str(i.get("qty")), str(i.get("size_text"))) for i in a.get("items") or [] if isinstance(i, dict)))
        hs = tuple(sorted(heat_key(h.get("heat_no")) for h in a.get("heats") or [] if isinstance(h, dict)))
        sig = (_HKEY(a.get("cert_no")), its, hs)
        ck = _HKEY(a.get("cert_no"))
        lit = tuple(sorted((heat_key(i.get("heat_no")), str(_num(i.get("qty")))) for i in a.get("items") or [] if isinstance(i, dict) and i.get("heat_no")))
        if ((its or hs) and sig in seen) or (lit and hs and any(lit == l2 and hs == h2 and (ck == c2 or _one_off(ck, c2))
                                                                 for c2, l2, h2 in loose)):   # 694: other size spacing, a letter off
            repeats.append(p)
            keep.append(dict(p, answer=dict(a, items=[], heats=[dict(h, mech=[]) for h in a.get("heats") or [] if isinstance(h, dict)])))
        else:                                             # 694: a copy still fills in elements the first scan missed
            seen.add(sig)
            loose.append((ck, lit, hs))
            keep.append(p)
    return keep, repeats


def merge(pages, source_name="", family_hint=None):
    """Page answers -> one file-level answer (header first-read wins, items concatenated, heats merged)."""
    cert_pages = [p for p in pages if (p["answer"] or {}).get("page_type") in CERT_TYPES]
    extra = [p for p in pages if p not in cert_pages and p.get("page")]
    d = {"cert_nos": [], "material_nos": [], "order_numbers": [], "items": [], "heats": {}, "pages": len(pages),
         "cert_pages": [p["page"] for p in cert_pages], "extra": [], "legibility": []}
    for p in cert_pages:                                  # 445: MPS's handwritten job / order number (re-asked pages)
        hw = (p["answer"] or {}).get("handwritten")
        if not isinstance(hw, dict):
            continue
        job, order = str(hw.get("job") or "").strip(), str(hw.get("order") or "").strip()
        m = re.search(r"(?<![A-Z0-9])A\s?-?\s?\d{5}(?!\d)", job.upper())
        if m and not order:                               # "JOB 4122 A49797" read as one: the A-number is the order
            order, job = m.group(0), (job[:m.start()] + job[m.end():]).strip()
        for v in [order] + [str(x) for x in (hw.get("orders") if isinstance(hw.get("orders"), list) else [])]:     # 641
            for tok in re.split(r"[;,/\s]+(?=[A-Za-z])", v.strip()):
                o = {"label": "handwritten", "value": _hand_order(tok)}
                if tok and o not in d["order_numbers"]:
                    d["order_numbers"].append(o)
        if job and not d.get("mps_job"):
            d["mps_job"] = job
    cert_pages, repeats = _drop_repeats(cert_pages)       # 641: the same paper scanned again
    cert_pages = _link_cert([dict(p, answer=_form_rules(p["answer"] or {})) for p in cert_pages])   # 711: forms, item no. across pages
    cert_pages = [dict(p, answer=_vote_rows(_sumitomo_heats(_link_items(p["answer"] or {})))) for p in cert_pages]   # 642: chemistry by item no.
    extra = extra + repeats
    for k in ("cert_type", "date_iso", "date_as_written", "mill", "supplier", "customer", "product", "standard", "grade"):
        d[k] = None
    for p in cert_pages:
        a = p["answer"]
        for k in ("cert_type", "date_iso", "date_as_written", "mill", "supplier", "customer", "product", "standard", "grade"):
            if not d[k] and _clean(a.get(k)):
                d[k] = _clean(a.get(k))
        for k, dst in (("cert_no", "cert_nos"), ("material_no", "material_nos")):
            v = _clean(a.get(k))
            if v and re.fullmatch(r"(?i)(?:cert(?:ificate)?\.?\s*)?(?:no|n[o°º]|number|nr)\.?", v):
                v = None                                            # the label read as the value ("No.")
            if v and k == "cert_no" and re.search(r"10204|10474", v):
                v = None                                            # 641: the inspection standard in the No. box
            if v and v not in d[dst]:
                d[dst].append(v)
        for o in a.get("order_numbers") or []:
            v = o if isinstance(o, dict) else {"label": None, "value": o}
            if _clean(v.get("value")) and v not in d["order_numbers"]:
                d["order_numbers"].append({"label": _clean(v.get("label")), "value": _clean(v.get("value"))})
        for it in a.get("items") or []:
            if isinstance(it, dict):
                d["items"].append(dict(it, page=p["page"]))
        for h in a.get("heats") or []:
            if not isinstance(h, dict) or not norm_heat(h.get("heat_no")):
                continue
            key = norm_heat(h["heat_no"])
            cur = d["heats"].setdefault(key, {"heat_no": key, "grade": None, "chem": {}, "mech": [], "pages": []})
            if h.get("charge_no") and not cur.get("charge_no"):
                cur["charge_no"] = norm_heat(h["charge_no"])      # 642: the material charge the chemistry is of
            cur["grade"] = cur["grade"] or _clean(h.get("grade"))
            ch = chem_of(h, a)                            # 553: headings + row, paired here
            if ch is None:
                d.setdefault("chem_mismatch", []).append(key)
                ch = {}
            for ek, ev in ch.items():
                cur["chem"].setdefault(ek, ev)
            for m in h.get("mech") or []:
                if isinstance(m, dict):
                    cur["mech"].append({"yield_mpa": _mpa(m.get("yield"), m.get("unit")), "tensile_mpa": _mpa(m.get("tensile"), m.get("unit")),
                                        "elongation_pct": _num(m.get("elongation_pct")), "hardness": _clean(m.get("hardness")),
                                        "impact_j": _num(m.get("impact_j")), "item_line": _num(m.get("item_line"))})
            cur["pages"].append(p["page"])
        if a.get("legibility"):
            d["legibility"].append(a["legibility"])
    for cur in d["heats"].values():                       # 641: yield and tensile printed the other way round
        for m in cur["mech"]:
            if m["yield_mpa"] and m["tensile_mpa"] and m["yield_mpa"] >= 1.15 * m["tensile_mpa"]:
                m["yield_mpa"], m["tensile_mpa"] = m["tensile_mpa"], m["yield_mpa"]
                d.setdefault("mech_swapped", []).append(cur["heat_no"])
    for p in extra:
        d["extra"].append((p["page"], (p["answer"] or {}).get("page_type") or "other"))
    return d


def page_ranges(extra):
    """[(3,'packing_list'),(4,'packing_list'),(6,'bill_of_lading')] -> '3-4 packing list; 6 bill of lading'."""
    out, run_ = [], None
    for pg, kind in sorted(extra, key=lambda x: x[0]):
        if run_ and run_[2] == kind and pg == run_[1] + 1:
            run_[1] = pg
        else:
            if run_:
                out.append(run_)
            run_ = [pg, pg, kind]
    if run_:
        out.append(run_)
    return "; ".join((f"{a}" if a == b else f"{a}-{b}") + " " + k.replace("_", " ") for a, b, k in out) or None


def to_fields(d, source_name="", family_hint=None):
    """Merged answer -> pipeline fields (design §6) + flags. family_hint: the old library's family (backfill)."""
    chk, flags = [], []
    f = {"form": "test_cert", "doc_label": "Test certificate"}
    cns = list({_HKEY(c): c for c in sorted(d["cert_nos"], key=lambda c: -str(c).count(" "))}.values())   # BY YF1577 = BYYF1577
    cns = [c for i, c in enumerate(cns) if not any(_one_off(_HKEY(o), _HKEY(c)) and (len(_HKEY(o)) > len(_HKEY(c)) or
                                                      (len(_HKEY(o)) == len(_HKEY(c)) and j < i)) for j, o in enumerate(cns))]
    f["cert_no"] = "; ".join(cns) or None                # 694: BYF8794 on a copy of BYYF8794 - the full number
    f["material_no"] = "; ".join(d["material_nos"]) or None
    ct = str(d.get("cert_type") or "").upper()
    f["cert_type"] = ("3.2" if "3.2" in ct else "3.1" if "3.1" in ct else "2.2" if "2.2" in ct else
                      "Inspection" if "INSP" in ct else None)
    f["date"] = parse_date(d.get("date_iso")) or parse_date(d.get("date_as_written"))
    f["date_as_written"] = d.get("date_as_written")
    f["mill"], f["supplier"], f["customer"] = d.get("mill"), d.get("supplier"), d.get("customer")
    f["standard"], f["grade"] = d.get("standard"), norm_grade(d.get("grade"))

    # orders: what the certificate prints, then the file name as a fallback
    vals = [o["value"] for o in d["order_numbers"]]
    mps, ronin, contracts = norm_orders(vals)
    nm, nr, _ = norm_orders([], os.path.basename(source_name or ""))
    for v in nm:
        if v not in mps:
            mps.append(v)
            chk.append(f"order {v} taken from the file name")
    for v in nr:
        if v not in ronin:
            ronin.append(v)
            chk.append(f"order {v} taken from the file name")
    f["mps_po"], f["ronin_po"], f["contract"] = "; ".join(mps) or None, "; ".join(ronin) or None, "; ".join(contracts) or None
    f["order_numbers"] = d["order_numbers"]
    f["mps_job"] = re.sub(r"[^0-9A-Z]", "", re.sub(r"\bJOB\b", "", str(d.get("mps_job") or "").upper())) or None   # 445
    cust = (f["customer"] or "").upper()
    if ronin and not mps:
        f["purchaser"] = "Ronin"
    elif mps and not ronin:
        f["purchaser"] = "MPS"
    elif mps and ronin:
        f["purchaser"] = "MPS"
        chk.append("both MPS and Ronin order numbers on the cert")
    elif "MINING PRESSURE" in cust:
        f["purchaser"] = "MPS"
    elif "RONIN" in cust:
        f["purchaser"] = "Ronin"
    else:
        f["purchaser"] = None

    # items
    items = []
    for n, it in enumerate(d["items"], 1):
        size_text = _clean(it.get("size_text") or it.get("description"))
        od = _num(it.get("od_mm"))
        nb = norm_nb(size_text) or nb_from_od(od)
        sch = norm_sch(it.get("wt_text"))
        if not sch and size_text:                      # '219.1*12.7*12.192' -> WT 12.7; '500NB X STD 90°3D' -> STD
            u = size_text.upper()
            m = re.search(r"\b(SCH(?:ED)?\.?\s*\d{2,3}|XXS|XS|STD)\b", u)
            m2 = re.match(r"\s*\d{2,4}(?:\.\d+)?\s*(?:MM|OD)?\s*[X*×]\s*(\d{1,2}(?:\.\d{1,2})?)(?!\d)", u)
            sch = norm_sch(m.group(1)) if m else (norm_sch(m2.group(1)) if m2 else None)
        fam = item_family(it, family_hint)                 # 532: plate by its size
        items.append({"#": n, "family": fam, "product": _clean(it.get("product")), "description": _clean(it.get("description")),
                      "size_nb": nb, "size2_nb": norm_nb(it.get("size2_text")), "size_text": size_text, "od_mm": od,
                      "schedule": sch, "schedule_names": sorted(sch_names_for(nb, sch)) or None,
                      "class": norm_class(it.get("class")), "standard": f["standard"], "grade": f["grade"],
                      "ends": _clean(it.get("ends")), "length_m": _num(it.get("length_m")), "qty": _num(it.get("qty")),
                      "unit": _clean(it.get("unit")), "total_m": _num(it.get("total_m")), "weight_kg": _num(it.get("weight_kg")),
                      "heat_no": norm_heat(it.get("heat_no")), "batch": _clean(it.get("batch")), "page": it.get("page")})
    f["items"] = items
    heats = list(d["heats"].values())
    f["heats"] = heats

    # family
    fams = {i["family"] for i in items if i["family"]}
    f["family"] = (fams.pop() if len(fams) == 1 else "Mixed" if len(fams) > 1 else
                   family_hint if family_hint in FAMILIES else
                   (items and family_of_spec(f.get("standard"), f.get("grade"), items=items)) or "Other")    # 531: its standard / grade
    if f["family"] == "Other" and items and "ARCELOR" in str(f.get("mill") or "").upper():
        f["family"] = "Pipe"                               # 532 (Dino): ArcelorMittal certs are plate or pipe
    if family_hint and f["family"] not in (family_hint, "Mixed") and items:
        chk.append(f"family read as {f['family']}; the old library said {family_hint}")

    # validation (design §3): flags, never fixes
    heat_keys = {heat_key(h["heat_no"]) for h in heats}
    for hn in dict.fromkeys(d.get("mech_swapped") or []):
        chk.append(f"heat {hn}: yield and tensile columns read the other way round - swapped")      # 641
    for hn in dict.fromkeys(d.get("chem_mismatch") or []):
        chk.append(f"heat {hn}: chemistry row and column headings differ in count - chemistry not read")    # 553
    for i in items:
        if i["heat_no"] and heats and heat_key(i["heat_no"]) not in heat_keys:
            chk.append(f"item {i['#']} heat {i['heat_no']} has no chemistry on the cert")
        if i["size_text"] and not i["size_nb"]:
            chk.append(f"item {i['#']} size '{i['size_text']}' not a standard NB")
    for h in heats:
        c = h["chem"]
        if not c:
            chk.append(f"heat {h['heat_no']}: no chemistry read")
        for el, lim in (("C", 0.5), ("Mn", 2.5), ("P", 0.1), ("S", 0.1), ("Si", 1.5)):
            if isinstance(c.get(el), (int, float)) and c[el] > lim:
                chk.append(f"heat {h['heat_no']}: {el} {c[el]} % is implausible (> {lim}) - check the factor")
        for m in h["mech"]:
            if m["yield_mpa"] and m["tensile_mpa"] and m["yield_mpa"] >= m["tensile_mpa"]:
                chk.append(f"heat {h['heat_no']}: yield {m['yield_mpa']} >= tensile {m['tensile_mpa']}")
    if not d["cert_pages"]:
        flags.append("not_a_test_cert")
    if not f["cert_no"]:
        flags.append("missing_cert_no")
    if not heats:
        flags.append("no_heats")
    if not items:
        flags.append("no_items")
    if f["family"] == "Other":
        flags.append("family_other")

    # summaries for the library columns
    f["heats_text"] = "; ".join(h["heat_no"] for h in heats) or "; ".join(dict.fromkeys(i["heat_no"] for i in items if i["heat_no"])) or None
    f["item_count"], f["heat_count"] = len(items), len(heats)
    f["pages"] = d["pages"]
    f["extra_pages"] = page_ranges(d["extra"])
    grades = list(dict.fromkeys(x for x in [f["standard"], f["grade"]] if x))
    classes = list(dict.fromkeys(i["class"] for i in items if i["class"]))
    f["grade_summary"] = " ".join(grades + classes)[:255] or None
    f["items_text"] = "\n".join(
        " ".join(str(x) for x in [i["qty"] if i["qty"] is not None else "", i["unit"] or "", "x" if i["qty"] is not None else "",
                                  f"{i['size_nb']}NB" if i["size_nb"] else (i["size_text"] or ""), i["schedule"] or "",
                                  i["class"] or "", i["product"] or "", f"heat {i['heat_no']}" if i["heat_no"] else ""] if x != "")
        for i in items) or None
    lg = d["legibility"]
    f["legibility"] = "poor" if "poor" in lg else "fair" if "fair" in lg else ("good" if lg else None)
    f["checks"] = chk
    f["review"] = "; ".join(chk) or None
    flags += ["check: " + c for c in chk]
    return f, flags


# ------------------------------------------------------------------------------------------------ old-site heat numbers
# Dino (25 Oct): the heat numbers on the old Test Cert site are correct; dashes and other non-alphanumerics were left out
# when they were typed. They are the check for the backfill: a heat read one character off (or with one extra/missing
# character) takes the old site's characters, keeping the cert's own punctuation where the lengths allow; old-site heats
# not read and heats read that are not on the old site become review checks.
_HKEY = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
_ILO = str.maketrans("ILO", "110")


def old_heats(old_columns):
    m = re.search(r"(?im)^\s*Heat\s*No\.?\s*:\s*(.+)$", str(old_columns or ""))
    if not m:
        return []
    return list(dict.fromkeys(k for k in (_HKEY(x) for x in re.split(r"[;,]", m.group(1))) if k))


def _lev(a, b):
    if abs(len(a) - len(b)) > 1:
        return 9
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def _apply_chars(read, good):
    """'11J2-3/11' read, '11J23II' good -> '11J2-3/II' (same number of letters and digits); otherwise the good value."""
    if len(_HKEY(read)) != len(good):
        return good
    it = iter(good)
    return "".join(next(it) if ch.isalnum() else ch for ch in str(read).upper())


def check_old_heats(f, flags=None):
    """Compare the heats read with the old site's (see above); corrects one-character misreads in heats[] and items[].
    Sets f["heat_check"] (counts and lists) and f["heat_check_text"]; adds review checks. Returns the corrections."""
    gold = old_heats(f.get("old_columns"))
    if not gold:
        return []
    heats = [h for h in (f.get("heats") or []) if isinstance(h, dict)]
    items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    read = list(dict.fromkeys(k for k in [_HKEY(h.get("heat_no")) for h in heats] + [_HKEY(i.get("heat_no")) for i in items] if k))
    gs = set(gold)
    exact = [r for r in read if r in gs]
    left_g = [g for g in gold if g not in set(read)]
    left_r = [r for r in read if r not in gs]
    fixes = {}
    ilo = lambda k: k.translate(_ILO)              # I/L read for 1 and O for 0 (or the other way), any number of them
    for g in list(left_g):
        cand = [r for r in left_r if ilo(r) == ilo(g)]
        if len(cand) != 1:
            cand = [r for r in left_r if len(g) >= 4 and _lev(r, g) <= 1]
        if len(cand) != 1:
            cand = [r for r in left_r if len(g) >= 4 and len(r) >= 4 and (g in r or r in g) and abs(len(g) - len(r)) <= 2]
        if len(cand) == 1:
            fixes[cand[0]] = g
            left_g.remove(g)
            left_r.remove(cand[0])
    done = []
    for rows in (heats, items):
        for x in rows:
            k = _HKEY(x.get("heat_no"))
            if k in fixes:
                new = _apply_chars(x["heat_no"], fixes[k])
                x.setdefault("heat_no_read", x["heat_no"])
                x["heat_no"] = new
                done.append((x["heat_no_read"], new))
    chk, before = list(f.get("checks") or []), len(f.get("checks") or [])
    for r, g in fixes.items():
        chk.append(f"heat read as {r}: {g} from the old site")
    for g in left_g:
        chk.append(f"old site heat {g} not read")
    for r in left_r:
        chk.append(f"heat {r} read but not on the old site")
    f["checks"] = chk
    f["review"] = "; ".join(chk) or None
    f["heat_check"] = {"old_site": len(gold), "exact": len(exact), "corrected": len(fixes), "not_read": left_g, "not_on_old_site": left_r}
    f["heat_check_text"] = (f"{len(exact) + len(fixes)} of {len(gold)} old-site heats" + (f" ({len(fixes)} corrected)" if fixes else "")
                            + (f"; {len(left_g)} not read" if left_g else "") + (f"; {len(left_r)} not on the old site" if left_r else ""))
    if flags is not None:
        flags += ["check: " + c for c in chk[before:]]
    if fixes:
        recompute(f)
    return done


def recompute(f):
    """After an edit on the dashboard: renumber the items and rebuild the summaries the library columns use (family,
    heat list, counts, grade summary, items text) the way to_fields builds them. Review checks are left as read."""
    items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    heats = [h for h in (f.get("heats") or []) if isinstance(h, dict)]
    for n, i in enumerate(items, 1):
        i["#"] = n
        if i.get("size_text") and not i.get("size_nb"):
            i["size_nb"] = norm_nb(i["size_text"])
        if i.get("heat_no"):
            i["heat_no"] = norm_heat(i["heat_no"])
    f["items"], f["heats"] = items, heats
    fams = {i.get("family") for i in items if i.get("family") in ("Pipe", "Flanges", "Fittings", "Plate")}       # 532
    if fams:
        f["family"] = fams.pop() if len(fams) == 1 else "Mixed"
    f["heats_text"] = "; ".join(str(h.get("heat_no")) for h in heats if h.get("heat_no")) or \
        "; ".join(dict.fromkeys(str(i["heat_no"]) for i in items if i.get("heat_no"))) or None
    f["item_count"], f["heat_count"] = len(items), len(heats)
    grades = list(dict.fromkeys(x for x in [f.get("standard"), f.get("grade")] if x))
    classes = list(dict.fromkeys(i["class"] for i in items if i.get("class")))
    f["grade_summary"] = " ".join(grades + classes)[:255] or None
    f["items_text"] = "\n".join(
        " ".join(str(x) for x in [i.get("qty") if i.get("qty") is not None else "", i.get("unit") or "",
                                  "x" if i.get("qty") is not None else "",
                                  f"{i['size_nb']}NB" if i.get("size_nb") else (i.get("size_text") or ""), i.get("schedule") or "",
                                  i.get("class") or "", i.get("product") or "", f"heat {i['heat_no']}" if i.get("heat_no") else ""]
                 if x != "")
        for i in items) or None
    mps = [x for x in re.split(r"[;,\s]+", str(f.get("mps_po") or "")) if x]
    ronin = [x for x in re.split(r"[;,\s]+", str(f.get("ronin_po") or "")) if x]
    if not f.get("purchaser"):
        f["purchaser"] = "MPS" if mps else "Ronin" if ronin else None
    return f


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="+")
    ap.add_argument("--family", default=None, help="the old library's family (Pipe / Flanges / Fittings): picks the worked examples")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--fewshot", default=FEWSHOT_DIR)
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="one JSON line per file (with the raw page answers)")
    a = ap.parse_args()
    for p in a.pdf:
        log = (lambda s: print(s, file=sys.stderr))
        pages, n = read(p, a.ollama, a.model, a.fewshot, a.family, a.max_pages, log=log)
        f, flags = to_fields(merge(pages, os.path.basename(p), a.family), os.path.basename(p), a.family)
        out = {"file": os.path.basename(p), "flags": flags, "fields": f}
        if a.json:
            out["pages"] = pages
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()


# ------------------------------------------------------------------------------------------------ batches (500)
def _cert_key(v):
    """The cert number as a grouping key, or None when it isn't one: the label ("No."), the inspection standard
    (EN10204-3.1, N10204 3.2 - as tc_incomplete.real_cert_no), a title (JR MATERIAL TEST CERTIFICATE), or under 3
    digits. "No.4500144015" and "4500144015" are the same cert."""
    s = re.sub(r"\s+", " ", str(_clean(v) or "")).strip().upper()
    s = re.sub(r"^(?:CERT(?:IFICATE)?\.?\s*)?(?:NO|N[O°º]|NUMBER|NR)\s*[.:]?\s*", "", s)
    s = re.sub(r"\s*-\s*\d{1,2}\s*/\s*\d{1,2}$", "", s)        # sheet 3 of 6 of one cert: 13L02-3/6 -> 13L02
    if not s or re.fullmatch(r"(BS ?)?(EN|N|DIN|ISO)? ?[-.]?(10204|10474)\b.*|3\.[12] ?[AB]?|2\.[12]", s) \
            or re.search(r"10204|10474|CERTIFICATE|MATERIAL|\bTEST\b", s) or len(re.findall(r"\d", s)) < 3:
        return None
    if re.fullmatch(r"(A\d{5}|DF\d{4,5}|PL\d{4})[A-Z]{0,3}|IL\d{5}ZA\d+", re.sub(r"[^A-Z0-9]", "", s)):
        return None                                     # MPS / Ronin / Polymer order or the L/C number, not the cert's
    return _HKEY(s).translate(_ILO)


def _one_off(x, y):
    """694: one number is the other with one character dropped, or (701) one letter read as another (BYVF8793 /
    BYYF8793); a digit apart is a neighbouring cert, not a copy."""
    if x and y and len(x) == len(y):
        d = [(a, b) for a, b in zip(x, y) if a != b]
        return len(d) == 1 and d[0][0].isalpha() and d[0][1].isalpha()
    if abs(len(x) - len(y)) != 1:
        return False
    a, b = (x, y) if len(x) > len(y) else (y, x)
    return any(a[:i] + a[i + 1:] == b for i in range(len(a)))


def split_certs(pages):
    """500 (Dino 5 Nov): a scanned batch -> [[page numbers] per test certificate number], in the order the numbers first
    appear. A cert page with a number starts that cert's group or joins it again; a page without one stays with the
    page before it; pages before the first numbered page go with the first cert. Numbers must match exactly once
    normalised (I/L/O read as 1/1/0): neighbouring certs are often numbered one apart."""
    groups, order, cur, lead, numbered, mills, anon = {}, [], None, [], 0, {}, 0
    gheats = {}                                           # 694: heats seen per cert, for copies with a misread number
    for p in pages:
        if not p.get("page"):
            continue
        a = p.get("answer") or {}
        cert = a.get("page_type") in CERT_TYPES
        k = _cert_key(a.get("cert_no")) if cert else None
        if k and k in {_HKEY(o.get("value") if isinstance(o, dict) else o).translate(_ILO) for o in a.get("order_numbers") or []}:
            k = None                                      # 506: the page's order number read into the cert number box
        numbered += bool(k)
        if k and k not in groups:                         # 694: BYF8793 on a copy of BYYF8793 (same heats)
            ph = {heat_key(x) for x in _page_heats(a)}
            k = next((g for g in order if ph and ph & gheats.get(g, set()) and _one_off(g, k)), k)
        mk = _mill_key(a.get("mill")) if cert else None
        if k:
            if k not in groups:
                if cur and cur.startswith("#") and (not mills.get(cur) or not mk or _same_mill(mk, mills[cur])):
                    groups[k] = groups.pop(cur)           # 532: the unnumbered pages before it are its own
                    order[order.index(cur)] = k
                    if cur in mills:
                        mills[k] = mills.pop(cur)
                else:
                    groups[k] = []
                    order.append(k)
            cur = k
        elif mk and (cur is None or (mills.get(cur) and not _same_mill(mk, mills[cur]))):
            anon += 1                                     # 532: no number of its own, another mill: another cert
            cur = f"#{anon}"
            groups[cur] = []
            order.append(cur)
        if cur and mk and cur not in mills:
            mills[cur] = mk
        (groups[cur] if cur else lead).append(p["page"])
        if cur and cert:
            gheats.setdefault(cur, set()).update(heat_key(x) for x in _page_heats(a))
    if not numbered or 2 * numbered < sum(1 for p in pages if _header_page(p)):
        return _split_by_header(pages)                    # 506: no numbers, or too few to group by
    if len(order) == 1:
        return [[p["page"] for p in pages if p.get("page")]]
    groups[order[0]] = lead + groups[order[0]]
    return [sorted(groups[k]) for k in order]


_MILL_SKIP = {"STEEL", "STEELS", "CO", "LTD", "LIMITED", "COMPANY", "GROUP", "IRON", "AND", "THE", "SOUTH", "AFRICA",
              "PTY", "INC", "CORP", "CORPORATION", "INTERNATIONAL", "INDUSTRIAL", "INDUSTRY", "INDUSTRIES", "MILL", "WORKS",
              "SPECIAL", "HIGH", "PRESSURE", "PIPE", "TUBE", "FLANGE", "FITTING", "PRODUCTS", "MANUFACTURING", "LIM"}


def _mill_key(v):
    """532: the distinctive words of a mill's name ("ArcelorMittal South Africa Lim" -> {ARCELORMITTAL})."""
    toks = {t for t in re.findall(r"[A-Z]{3,}|[\u4e00-\u9fff]{2,}", str(_clean(v) or "").upper()) if t not in _MILL_SKIP}
    return frozenset(toks) or None


def _same_mill(a, b):
    """532: two mill names are the same mill (misreads allowed); a name in Chinese and one in Latin letters can't be
    told apart (鑫鹏源 / Xinpengyuan), so they count as the same."""
    import difflib
    cjk = lambda s: all(re.fullmatch(r"[\u4e00-\u9fff]+", t) for t in s)
    if cjk(a) != cjk(b):
        return True
    return any(x == y or (len(x) >= 5 and len(y) >= 5 and difflib.SequenceMatcher(None, x, y).ratio() >= 0.8) for x in a for y in b)


def _page_heats(a):
    return frozenset(k for k in ([norm_heat(h.get("heat_no")) for h in a.get("heats") or [] if isinstance(h, dict)]
                                 + [norm_heat(i.get("heat_no")) for i in a.get("items") or [] if isinstance(i, dict)]) if k)


def _header_page(p):
    a = p.get("answer") or {}
    return bool(p.get("page")) and a.get("page_type") == "mtc" and bool(_clean(a.get("mill"))) and bool(_page_heats(a))


def _split_by_header(pages):
    """No cert numbers anywhere in the file (DOC051126-05112026094800: Shandong Zhongnuo certs with an empty "No." box,
    one heat a page): a page with its own header (mtc, mill read) whose date or heats differ from the cert before
    starts a new cert; a continuation or other page stays with the page before it."""
    out, last = [], None
    for p in pages:
        if not p.get("page"):
            continue
        a = p.get("answer") or {}
        heats = frozenset(k for k in ([norm_heat(h.get("heat_no")) for h in a.get("heats") or [] if isinstance(h, dict)]
                                      + [norm_heat(i.get("heat_no")) for i in a.get("items") or [] if isinstance(i, dict)]) if k)
        sig = (_clean(a.get("date_iso")), heats)
        if a.get("page_type") == "mtc" and _clean(a.get("mill")) and heats and (not out or sig != last):
            out.append([])
            last = sig
        if not out:
            out.append([])
        out[-1].append(p["page"])
    return out


# 694: the 10 Nov prompt ({element: value} chemistry), for cert pages the headings + rows answer fails on
PROMPT_V1 = __import__("base64").b64decode("WW91IGFyZSByZWFkaW5nIE9ORSBQQUdFIG9mIGEgZmlsZSBvZiBzdGVlbCB0ZXN0IGNlcnRpZmljYXRlcyBib3VnaHQgYnkgTWluaW5nIFByZXNzdXJlIFN5c3RlbXMgKE1QUykgb3IKUm9uaW4sIFNvdXRoIEFmcmljYTogcGlwZSwgZmxhbmdlcyBhbmQgcGlwZSBmaXR0aW5ncyAoZWxib3dzL2JlbmRzLCB0ZWVzLCByZWR1Y2VycywgY2Fwcywgc3R1YiBlbmRzKS4KCkZpcnN0IGRlY2lkZSB3aGF0IHRoZSBwYWdlIGlzIChwYWdlX3R5cGUpOgotICJtdGMiOiBhIG1pbGwgdGVzdCBjZXJ0aWZpY2F0ZSAvIG1hdGVyaWFsIHRlc3QgY2VydGlmaWNhdGUgLyBpbnNwZWN0aW9uIGNlcnRpZmljYXRlIHRvIEVOIDEwMjA0ICgzLjEsIDMuMiwgMi4yKQogIG9yIElTTyAxMDQ3NCB3aXRoIGEgY2hlbWljYWwgY29tcG9zaXRpb24gdGFibGUgKGFuZCB1c3VhbGx5IG1lY2hhbmljYWwgcHJvcGVydGllcykgZm9yIG9uZSBvciBtb3JlIGhlYXRzLgotICJtdGNfY29udGludWF0aW9uIjogdGhlIG5leHQgcGFnZSBvZiBhIGNlcnRpZmljYXRlIChtb3JlIGl0ZW1zLCBtb3JlIGhlYXRzIG9yIHRoZSBtZWNoYW5pY2Fscykgd2l0aG91dCBpdHMgb3duIGhlYWRlci4KLSAiaW5zcGVjdGlvbl9vbmx5IjogYW4gaW5zcGVjdGlvbiAvIGNvbmZvcm1pdHkgLyBxdWFsaXR5IGNlcnRpZmljYXRlIHdpdGggTk8gY2hlbWlzdHJ5IHRhYmxlLgotICJoeWRyb19yZXBvcnQiLCAicGFja2luZ19saXN0IiwgImJpbGxfb2ZfbGFkaW5nIiwgImludm9pY2UiLCAibGV0dGVyX29mX2NyZWRpdCIsICJvdGhlciIuCkZvciBhbnkgcGFnZV90eXBlIG90aGVyIHRoYW4gbXRjIC8gbXRjX2NvbnRpbnVhdGlvbiAvIGluc3BlY3Rpb25fb25seSwgcmV0dXJuIG9ubHkgcGFnZV90eXBlIGFuZCBsZWdpYmlsaXR5IGFuZApsZWF2ZSBldmVyeSBvdGhlciBmaWVsZCBudWxsIG9yIGVtcHR5LgoKSGVhZGVyIGZpZWxkcyAoYXMgcHJpbnRlZDsgbnVsbCB3aGVuIG5vdCBvbiB0aGlzIHBhZ2UpLiBXaGVyZSBhIG5hbWUgaXMgcHJpbnRlZCBpbiBDaGluZXNlIGFuZCBFbmdsaXNoLCBnaXZlIHRoZSBFbmdsaXNoLgotIGNlcnRfbm86IHRoZSBjZXJ0aWZpY2F0ZSBudW1iZXIgKCJOby4iLCAiQ2VydCBObyIsICJDZXJ0aWZpY2F0ZSBObyIsICJDZXJ0aWZpY2F0ZSBSZWZlcmVuY2UgTm8iLCAiQ0VSVElGSS5OTyIpLgogIE5ldmVyIHJldHVybiB0aGUgbGFiZWwgaXRzZWxmOiBpZiB0aGUgIk5vLiIgYm94IGlzIGVtcHR5LCBjZXJ0X25vIGlzIG51bGwuCi0gbWF0ZXJpYWxfbm86IGEgc2VwYXJhdGUgbWF0ZXJpYWwgLyBwcm9kdWN0IG51bWJlciB3aGVuIHByaW50ZWQgKEFyY2Vsb3JNaXR0YWw6ICJNYXRlcmlhbCBObyIpLgotIGNlcnRfdHlwZTogIjMuMSIsICIzLjIiLCAiMi4yIiBvciAiaW5zcGVjdGlvbiIgKEVOMTAyMDQgMy4xQiBjb3VudHMgYXMgMy4xKTsgaXQgbWF5IG9ubHkgYmUgaW4gYSByZW1hcmsgb3IgZm9vdGVyLgotIGRhdGVfYXNfd3JpdHRlbiAvIGRhdGVfaXNvOiB0aGUgY2VydGlmaWNhdGUgKGlzc3VlKSBkYXRlIC0gbWF5IGJlIGluIHRoZSBmb290ZXIgKCJEYXRlIG9mIFJlbGVhc2UiLCAiRmlsbGluZyBkYXRlCiAgWUVBUiAyMDExIE1PTlRIIDExIERBWSAyNSIsICLml6XmnJ8iKS4KLSBtaWxsOiB0aGUgbWFudWZhY3R1cmVyIHdob3NlIGxldHRlcmhlYWQgaXQgaXMgKGUuZy4gIkpJQU5HWUlOIENJVFkgU0VBTUxFU1MgU1RFRUwgVFVCRSBGQUNUT1JZIiwKICAiWkhBTkdRSVUgUlVJWUUgTUVDSEFOSUNBTCBDTy4sIExURCIsICJTSEFOR0hBSSBHQVRFV0FZIEZJVFRJTkdTIENPLiwgTFREIiwgIkFyY2Vsb3JNaXR0YWwgU291dGggQWZyaWNhIExpbWl0ZWQiKS4KLSBzdXBwbGllcjogYSB0cmFkZXIgLyBzdG9ja2lzdCBuYW1lZCBhcyBzZWxsZXIgb3IgY3VzdG9tZXIgd2hlbiBpdCBpcyBub3QgTVBTIG9yIFJvbmluIChOU1RLIC8gTmlwcG9uIFN0ZWVsIGFuZAogIFN1bWlraW4gQnVzc2FuLCBLZW5kbywgTWFjc3RlZWwsIEluY2xlZG9ucyk7IG51bGwgaWYgbm9uZS4KLSBjdXN0b21lcjogdGhlIGN1c3RvbWVyIC8gcHVyY2hhc2VyIC8gY29uc2lnbmVlIGFzIHByaW50ZWQuCi0gb3JkZXJfbnVtYmVyczogRVZFUlkgb3JkZXIsIGNvbnRyYWN0LCBQTywgaW52b2ljZSwgaW5kZW50IGFuZCByZWZlcmVuY2UgbnVtYmVyIHByaW50ZWQgYW55d2hlcmUgb24gdGhlIHBhZ2UgLSB0aGUKICBoZWFkZXIsIGEgc2FsZXMtY29udHJhY3QgYm94ICgiU0MgTm8uREYwOTQwL0RGMDk0MSIgLT4gREYwOTQwIGFuZCBERjA5NDEpLCB0aGUgU0hJUFBJTkcgTUFSS1MKICAoIk1QUy9BMjY2NTAvSzE4MzIvRFVSQkFOIiAtPiBBMjY2NTApIGFuZCB0aGUgZm9vdGVyLCBpbmNsdWRpbmcgdGhlIGRvY3VtZW50YXJ5IGNyZWRpdCAvIEwvQyBudW1iZXIKICAoIkRvY3VtZW50YXJ5IENyZWRpdCBOdW1iZXI6IElMMTYzNDFaQTA3MDQ5OTIiKSwgZXhhY3RseSBhcyBwcmludGVkLCBlYWNoIHdpdGggaXRzIGxhYmVsOgogIFt7ImxhYmVsIjogIk9SREVSIE5PIiwgInZhbHVlIjogIkRGMDkwNiJ9LCB7ImxhYmVsIjogIkNvbnRyYWN0IE5vIiwgInZhbHVlIjogIkE0MTI0OSJ9XS4KICBNUFMgb3JkZXJzIGxvb2sgbGlrZSBBNDEyNDksIFJvbmluIG9yZGVycyBsaWtlIERGMDkwNiwgY29udHJhY3RzIGxpa2UgQ05aQU4yMDE4OEdTLTEuCi0gcHJvZHVjdDogZS5nLiAiU0VBTUxFU1MgQ0FSQk9OIFNURUVMIFBJUEUiLCAiRkxBTkdFUyIsICJCVVRUIFdFTERJTkcgRklUVElOR1MiLgotIHN0YW5kYXJkOiB0aGUgc3BlY2lmaWNhdGlvbihzKSBhcyBwcmludGVkICgiQVNUTSBBMTA2IEIgLyBBUEkgNUwgWDQyIFBTTC0xIiwgIlNBTlMgMTEyMyIsICJBU1RNIEEyMzQgV1BCIikuCi0gZ3JhZGU6IHRoZSBzdGVlbCBncmFkZSAoWDQyLCBHci5CLCBTMzU1SlIsIFdQQiwgQTEwNSwgMzE2TCkuCgppdGVtczogb25lIGVudHJ5IHBlciBQUk9EVUNUIGxpbmUgKHRoZSBpdGVtIC8gc2l6ZSAvIHByb2R1Y3QgbGlzdCAtIG5vdCBhIHRlc3Qtc3VtbWFyeSByb3cpLiBBIHNpemUgdGhhdCBjb250aW51ZXMKb24gdGhlIG5leHQgcm93IHdpdGggbm8gbmV3IHF0eSBpcyBPTkUgbGluZToKICB7ImxpbmUiOiBudW1iZXJ8bnVsbCwgInByb2R1Y3QiOiAiUGlwZXxGbGFuZ2V8RWxib3cgOTB8RWxib3cgNDV8QmVuZHxUZWV8UmVkdWNlcnxDYXB8U3R1YiBlbmR8Li4uIiwKICAgImRlc2NyaXB0aW9uIjogdGV4dCBhcyBwcmludGVkLCAic2l6ZV90ZXh0Ijogc2l6ZSBhcyBwcmludGVkICgiMjE5LjEqMTIuNyoxMi4xOTIiLCAiMiAxLzJcIiIsICIyNTBOQiIsICJETjUwIiksCiAgICJzaXplMl90ZXh0Ijogc2Vjb25kIHNpemUgZm9yIHJlZHVjZXJzL3RlZXMgb3IgbnVsbCwgIm9kX21tIjogbnVtYmVyfG51bGwsICJ3dF90ZXh0Ijogd2FsbCB0aGlja25lc3Mgb3Igc2NoZWR1bGUgYXMKICAgcHJpbnRlZCAoIjEyLjciLCAiU0NIODAiLCAiU1REIiwgIjQuMm1tIiksICJjbGFzcyI6IHByZXNzdXJlIGNsYXNzIGFzIHByaW50ZWQgKCIxMDAwLzMiLCAiMTYwMC8zIiwgIjE1MCMiLCAiUE4xNiIsCiAgICJUMTAwMCIpLCAiZW5kcyI6IGVuZHMgLyBmYWNpbmcgKCJQRSIsICJCRSIsICJGRiIsICJSRiIsICJQTEZGIiksICJsZW5ndGhfbSI6IGxlbmd0aCBvZiBvbmUgcGllY2UgaW4gbWV0cmVzLAogICAicXR5IjogbnVtYmVyIG9mIFBJRUNFUywgInVuaXQiOiAicGNzIiAob3IgIm0iIC8gImtnIiB3aGVuIG9ubHkgdGhhdCBpcyBnaXZlbiksICJ0b3RhbF9tIjogdG90YWwgbWV0cmVzfG51bGwsCiAgICJ3ZWlnaHRfa2ciOiB3ZWlnaHQgaW4ga2cgKGEgY29sdW1uIGluIHRvbm5lcyAiV1QgKHQpIDM4LjA0MSIgPSAzODA0MSBrZyksCiAgICJoZWF0X25vIjogdGhlIEhFQVQgLyBDQVNUIC8gQ0hBUkdFIC8gTUVMVCBudW1iZXIgKOeCieWPtykgZm9yIHRoaXMgbGluZSAtIG5ldmVyIHRoZSBiYXRjaCwgbG90LCB3b3JrIG9yIE1GRyBudW1iZXIsCiAgICJiYXRjaCI6IGEgYmF0Y2ggLyBsb3QgLyBNRkcgLyB3b3JrIG51bWJlciBmb3IgdGhpcyBsaW5lLCBvciBudWxsfQogIEEgc2l6ZSBzdHJpbmcgbGlrZSAiOFwiKlhYUyo5LjE0NCIgb3IgIjE0XCIoMzU1LjYpKlM0MCgxMS4xMykqMTIxOTIiIGlzIFNJWkUgKiBXQUxMICogTEVOR1RIOiA5LjE0NCAvIDEyMTkyIGlzCiAgdGhlIGxlbmd0aCAobSAvIG1tKSwgbmV2ZXIgdGhlIHdhbGwgdGhpY2tuZXNzLgpoZWF0czogb25lIGVudHJ5IHBlciBoZWF0IChjYXN0IC8gY2hhcmdlIC8gbWVsdCkgbnVtYmVyIHdpdGggaXRzIGNoZW1pc3RyeSBhbmQgbWVjaGFuaWNhbCByZXN1bHRzLgogIFJlYWQgZWFjaCBjaGVtaXN0cnkgdmFsdWUgZnJvbSB0aGUgY29sdW1uIHdob3NlIEhFQURJTkcgaXMgdGhhdCBlbGVtZW50OiBtaWxscyBvcmRlciB0aGUgY29sdW1ucyBkaWZmZXJlbnRseQogIChDIFNpIE1uIFAgUywgQyBNbiBTaSBQIFMsIEMgU2kgTW4gUyBQIENyIFYgTW8gTmkgQ3UgLi4uKS4gV2hlbiB0aGUgdGFibGUgaGFzIGEgaGVhdCAvIGxhZGxlIC8gbWVsdGluZyByb3cgKEwsIFIsCiAgIkhlYXQiKSBhbmQgYSBwcm9kdWN0IHJvdyAoUCwgQywgIlByb2R1Y3QiKSwgZ2l2ZSB0aGUgaGVhdCByb3cuCiAgT25lIG1lY2ggZW50cnkgcGVyIHJlc3VsdCByb3cgb3IgcGFpciAoIigxKSBBY3R1YWwiLCAiKDIpIEFjdHVhbCIsICIzNjMvMzY0IiA9IHR3byBlbnRyaWVzKS4gV2hlbiBtZWNoYW5pY2FsIHJvd3MKICBjYXJyeSBubyBoZWF0IG51bWJlciB0aGV5IGZvbGxvdyB0aGUgaGVhdHMgaW4gdGhlIG9yZGVyIHRoZSBjZXJ0aWZpY2F0ZSBsaXN0cyB0aGVtLCBpbmNsdWRpbmcgaGVhdHMgbGlzdGVkIG9uCiAgZWFybGllciBwYWdlcyAobmFtZWQgYmVsb3cgdGhlIHByb21wdCB3aGVuIHRoZXJlIGFyZSBhbnkpLgogIHsiaGVhdF9ubyI6IHRleHQsICJncmFkZSI6IHRleHR8bnVsbCwKICAgImNoZW1fbXVsdGlwbGllciI6IHsiQyI6IDEwMCwgIlAiOiAxMDAwLCAuLi59IE9OTFkgd2hlbiB0aGUgY29sdW1uIGhlYWRpbmcgc2hvd3MgYSBmYWN0b3Igc3VjaCBhcyAiKjEwMCIgb3IgIngxMDAwIiwKICAgImNoZW0iOiB7IkMiOiBudW1iZXIsICJTaSI6IG51bWJlciwgIk1uIjogbnVtYmVyLCAiUCI6IG51bWJlciwgIlMiOiBudW1iZXIsICJDciI6IC4uLiwgIk5pIjogLi4uLCAiTW8iOiAuLi4sICJDdSI6IC4uLiwKICAgICAgICAgICAgIlYiOiAuLi4sICJOYiI6IC4uLiwgIlRpIjogLi4uLCAiQWwiOiAuLi4sICJOIjogLi4uLCAiQiI6IC4uLiwgIkNlcSI6IC4uLn0gLSB0aGUgUkVTVUxUIHJvdyBmb3IgdGhhdCBoZWF0LAogICAgICAgICAgICBudW1iZXJzIGV4YWN0bHkgYXMgcHJpbnRlZCAoZG8gbm90IGFwcGx5IHRoZSBmYWN0b3IgeW91cnNlbGYpOyBza2lwIHRoZSBzcGVjIG1pbiAvIG1heCByb3dzLAogICAibWVjaCI6IFt7InlpZWxkIjogbnVtYmVyfG51bGwsICJ0ZW5zaWxlIjogbnVtYmVyfG51bGwsICJ1bml0IjogIk1QYXxOL21tMnxrc2l8cHNpIiwgImVsb25nYXRpb25fcGN0IjogbnVtYmVyfG51bGwsCiAgICAgICAgICAgICAiaGFyZG5lc3MiOiB0ZXh0fG51bGwsICJpbXBhY3RfaiI6IG51bWJlcnxudWxsLCAiaXRlbV9saW5lIjogbnVtYmVyfG51bGx9XX0KSWYgdGhlIGl0ZW0gdGFibGUgZ2l2ZXMgdGhlIGhlYXQgbnVtYmVyIHBlciBsaW5lLCBldmVyeSBpdGVtIGhlYXRfbm8gbXVzdCBhbHNvIGFwcGVhciBpbiBoZWF0cy4KCmxlZ2liaWxpdHk6ICJnb29kfGZhaXJ8cG9vciIuClJldHVybiBPTkxZIHRoaXMgSlNPTiBvYmplY3Q6CnsicGFnZV90eXBlIjogIi4uLiIsICJjZXJ0X25vIjogbnVsbCwgIm1hdGVyaWFsX25vIjogbnVsbCwgImNlcnRfdHlwZSI6IG51bGwsICJkYXRlX2FzX3dyaXR0ZW4iOiBudWxsLAogImRhdGVfaXNvIjogbnVsbCwgIm1pbGwiOiBudWxsLCAic3VwcGxpZXIiOiBudWxsLCAiY3VzdG9tZXIiOiBudWxsLCAib3JkZXJfbnVtYmVycyI6IFtdLCAicHJvZHVjdCI6IG51bGwsCiAic3RhbmRhcmQiOiBudWxsLCAiZ3JhZGUiOiBudWxsLCAiaXRlbXMiOiBbXSwgImhlYXRzIjogW10sICJsZWdpYmlsaXR5IjogImdvb2QifQpDb3B5IG51bWJlcnMgYW5kIGNvZGVzIGV4YWN0bHkgYXMgcHJpbnRlZC4gVXNlIG51bGwgd2hlbiB5b3UgY2Fubm90IHJlYWQgYSB2YWx1ZSAtIGRvIG5vdCBndWVzcy4=").decode("utf-8")

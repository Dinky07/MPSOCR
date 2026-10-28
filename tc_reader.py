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
import argparse, base64, glob, json, os, re, subprocess, sys, tempfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import po_reader                       # parse_answer: tolerant JSON (cut-off answers, trailing text)
except Exception:                          # pragma: no cover
    po_reader = None

FEWSHOT_DIR = os.path.join(HERE, "tc_fewshot")
NUM_PREDICT = 6144
FAMILIES = ("Pipe", "Flanges", "Fittings", "Mixed", "Other")

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
  Never return the label itself: if the "No." box is empty, cert_no is null.
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
- product: e.g. "SEAMLESS CARBON STEEL PIPE", "FLANGES", "BUTT WELDING FITTINGS".
- standard: the specification(s) as printed ("ASTM A106 B / API 5L X42 PSL-1", "SANS 1123", "ASTM A234 WPB").
- grade: the steel grade (X42, Gr.B, S355JR, WPB, A105, 316L).

items: one entry per PRODUCT line (the item / size / product list - not a test-summary row). A size that continues
on the next row with no new qty is ONE line:
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
heats: one entry per heat (cast / charge / melt) number with its chemistry and mechanical results.
  Read each chemistry value from the column whose HEADING is that element: mills order the columns differently
  (C Si Mn P S, C Mn Si P S, C Si Mn S P Cr V Mo Ni Cu ...). When the table has a heat / ladle / melting row (L, R,
  "Heat") and a product row (P, C, "Product"), give the heat row.
  One mech entry per result row or pair ("(1) Actual", "(2) Actual", "363/364" = two entries). When mechanical rows
  carry no heat number they follow the heats in the order the certificate lists them, including heats listed on
  earlier pages (named below the prompt when there are any).
  {"heat_no": text, "grade": text|null,
   "chem_multiplier": {"C": 100, "P": 1000, ...} ONLY when the column heading shows a factor such as "*100" or "x1000",
   "chem": {"C": number, "Si": number, "Mn": number, "P": number, "S": number, "Cr": ..., "Ni": ..., "Mo": ..., "Cu": ...,
            "V": ..., "Nb": ..., "Ti": ..., "Al": ..., "N": ..., "B": ..., "Ceq": ...} - the RESULT row for that heat,
            numbers exactly as printed (do not apply the factor yourself); skip the spec min / max rows,
   "mech": [{"yield": number|null, "tensile": number|null, "unit": "MPa|N/mm2|ksi|psi", "elongation_pct": number|null,
             "hardness": text|null, "impact_j": number|null, "item_line": number|null}]}
If the item table gives the heat number per line, every item heat_no must also appear in heats.

legibility: "good|fair|poor".
Return ONLY this JSON object:
{"page_type": "...", "cert_no": null, "material_no": null, "cert_type": null, "date_as_written": null,
 "date_iso": null, "mill": null, "supplier": null, "customer": null, "order_numbers": [], "product": null,
 "standard": null, "grade": null, "items": [], "heats": [], "legibility": "good"}
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


def rule_page_type(text):
    """Page type from a text layer when it is plain (shipping papers with no cert wording), else None (ask the model)."""
    t = text or ""
    if len(t.strip()) < 80 or _CERT.search(t):
        return None
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


def read_page(path, page, ollama_url, model, fewshot_dir, family=None, px=1600, hint=None, timeout=1200,
              keep_alive="15m", num_ctx=49152, earlier_heats=None):   # 49152 = the pipeline readers' value: no model reload
    with tempfile.TemporaryDirectory() as tmp:
        if str(path).lower().endswith(".pdf"):
            png = render(path, page, px, os.path.join(tmp, "p.png"))
        else:
            png = str(path)
        prompt = PAGE_ASK + (f"\n\nHeats listed on earlier pages of this file, in order: {', '.join(earlier_heats)}"
                             if earlier_heats else "")
        prompt += (f"\n\nThe page's text layer (OCR of the scan - a hint only, digits and letters are often "
                   f"wrong; trust the image):\n{hint[:3000]}" if hint else "")
        body = {"model": model, "stream": False, "format": "json", "keep_alive": keep_alive,
                "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": NUM_PREDICT},
                "messages": [{"role": "system", "content": PROMPT}] + fewshot_msgs(fewshot_dir, family)
                            + [{"role": "user", "content": prompt, "images": [base64.b64encode(open(png, "rb").read()).decode()]}]}
        req = urllib.request.Request(ollama_url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read())["message"]["content"]
    return parse_answer(raw)


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
            pages.append({"page": p, "how": how, "answer": a})
        except Exception as e:                      # one bad page does not lose the file
            pages.append({"page": p, "how": "error", "answer": {"page_type": "error"}, "error": str(e)[:300]})
        if log:
            log(f"  p{p}: {pages[-1]['how']} {pages[-1]['answer'].get('page_type')}")
    if n > max_pages:
        pages.append({"page": None, "how": "skipped", "answer": {"page_type": "not_read"}, "error": f"pages {max_pages + 1}-{n} not read"})
    return pages, n


# ------------------------------------------------------------------------------------------------ answer -> fields
def family_of_product(p, description=""):
    t = f"{p or ''} {description or ''}".upper()
    if re.search(r"FLANGE|\bPLFF\b|\bSORF\b|\bWNRF\b|\bBLRF\b|BLIND", t):
        return "Flanges"
    if re.search(r"ELBOW|BEND|\bTEE\b|REDUCER|\bCAP\b|STUB|FITTING|\bLR\b|\bSR\b|90\s*DEG|45\s*DEG", t):
        return "Fittings"
    if re.search(r"PIPE|TUBE|SMLS|SEAMLESS|ERW|LSAW", t):
        return "Pipe"
    return None


# upper end of a plausible result (%), to tell a printed "19" under "C *100" (0.19) from a printed 0.20 under it
CHEM_MAX = {"C": 0.6, "Si": 1.5, "Mn": 2.5, "P": 0.1, "S": 0.1, "Cu": 1.0, "Ni": 30, "Cr": 30, "Mo": 5, "V": 0.5,
            "Nb": 0.2, "Ti": 0.5, "Al": 0.2, "N": 0.05, "B": 0.01, "Ceq": 1.0}


def _el(k):
    k = re.sub(r"[^A-Za-z]", "", str(k))
    if k.upper() in ("CE", "CEQ", "CEV", "CEIIW"):
        return "Ceq"
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


def _mpa(v, unit):
    x = _num(v)
    if x is None:
        return None
    u = (unit or "MPa").upper()
    if "KSI" in u:
        return round(x * 6.894757)
    if "PSI" in u:
        return round(x * 0.006894757)
    return x


def merge(pages, source_name="", family_hint=None):
    """Page answers -> one file-level answer (header first-read wins, items concatenated, heats merged)."""
    cert_pages = [p for p in pages if (p["answer"] or {}).get("page_type") in CERT_TYPES]
    extra = [p for p in pages if p not in cert_pages and p.get("page")]
    d = {"cert_nos": [], "material_nos": [], "order_numbers": [], "items": [], "heats": {}, "pages": len(pages),
         "cert_pages": [p["page"] for p in cert_pages], "extra": [], "legibility": []}
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
            cur["grade"] = cur["grade"] or _clean(h.get("grade"))
            for ek, ev in _chem(h).items():
                cur["chem"].setdefault(ek, ev)
            for m in h.get("mech") or []:
                if isinstance(m, dict):
                    cur["mech"].append({"yield_mpa": _mpa(m.get("yield"), m.get("unit")), "tensile_mpa": _mpa(m.get("tensile"), m.get("unit")),
                                        "elongation_pct": _num(m.get("elongation_pct")), "hardness": _clean(m.get("hardness")),
                                        "impact_j": _num(m.get("impact_j")), "item_line": _num(m.get("item_line"))})
            cur["pages"].append(p["page"])
        if a.get("legibility"):
            d["legibility"].append(a["legibility"])
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
    f["cert_no"] = "; ".join(d["cert_nos"]) or None
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
        fam = family_of_product(it.get("product"), it.get("description")) or (
            family_hint if family_hint in ("Pipe", "Flanges", "Fittings") else None)
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
                   family_hint if family_hint in FAMILIES else "Other")
    if family_hint and f["family"] not in (family_hint, "Mixed") and items:
        chk.append(f"family read as {f['family']}; the old library said {family_hint}")

    # validation (design §3): flags, never fixes
    heat_keys = {heat_key(h["heat_no"]) for h in heats}
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
    fams = {i.get("family") for i in items if i.get("family") in ("Pipe", "Flanges", "Fittings")}
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

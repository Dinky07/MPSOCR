"""
CPU tier for supplier invoices: text layer or Tesseract OCR + rule-based parsing + arithmetic checks.

An invoice is accepted on CPU only if everything checks out:
  - supplier identified from its VAT number (lookup table seeded from Sage and learned from GPU results)
  - invoice number and invoice date found, date plausible
  - subtotal + VAT = total (within R1), and VAT matches a known rate
  - (if require_items) line items parsed where qty x price = amount, and the lines add up to the subtotal or total
Anything else returns the reasons, and the worker sends the invoice to the GPU (Qwen) instead.
"""
import csv, re, subprocess, tempfile
from datetime import date
from pathlib import Path

MONEY = re.compile(r"(?<![\w.,/])-?R?\s?(\d{1,3}(?:[ ,]\d{3})+[.,]\d{2}|\d+[.,]\d{2})(?![\d])")
NUMTOK = re.compile(r"(?<![\w/\-.])(\d{1,3}(?:[ ,]\d{3})+(?:[.,]\d{1,3})?|\d+(?:[.,]\d{1,3})?)(?![\w/\-])")
VATNO = re.compile(r"(?<!\d)(4\d{9})(?!\d)")
# a VAT number with its label ("VAT No.: 4050103557", "Vat Reg No 4..."): bank account numbers etc. don't have one
VAT_LABELLED = re.compile(r"(?i)\bVAT\s*(?:Reg(?:istration)?\.?\s*)?(?:No|Number|Nr|#)?\.?\s*[:.]?\s*(4\d{9})(?!\d)")
INVNO_COLUMN_LABELS = ("Tax Invoice No", "Invoice Number", "Invoice No", "Inv No", "Document No", "Doc No")
JOBNO = re.compile(r"(?<![\w])(\d{4,5}[A-E])(?![\w])")     # job series A-E (mps_jobs.JOB_STRICT)

L_SUB = re.compile(r"\b(sub\s*-?\s*total|total\s*(excl?|ex)\b|total\s+excluding|nett?\s+(total|amount)|amount\s+excl?)", re.I)
L_VAT = re.compile(r"\b(vat|tax)\b", re.I)
L_VAT_NOT = re.compile(r"\b(no|nr|number|reg|registration|invoice|incl|excl?|exempt|code)\b", re.I)
L_ANYTOT = re.compile(r"\btotal\b(?!\s+(?:estimated|mass|weight|qty|quantity|units|pages|nett\s+price))", re.I)
L_TOT = re.compile(r"\b(total\s*(incl?|including)|invoice\s+total|total\s+(due|payable|amount)|amount\s+(due|payable)|balance\s+due|grand\s+total)\b|^\s*total\b", re.I)
L_INVNO = re.compile(r"(?:tax\s+)?(?:invoice|inv|document|doc)\s*(?:no\.?|nr\.?|number|num|#)\s*[:.]?\s*([A-Z0-9][A-Z0-9/\-_]{2,})", re.I)
L_DATE = re.compile(r"(?:invoice|document|inv|tax\s+invoice|doc)?\s*date\s*[:.]?\s*([0-9A-Za-z ,./\-]{6,22})", re.I)
L_DUE = re.compile(r"(?:due\s+date|payment\s+due|due\s+by)\s*[:.]?\s*([0-9A-Za-z ,./\-]{6,22})", re.I)
L_ORDER = re.compile(r"(?:order\s*(?:no\.?|number|ref)|your\s+(?:ref(?:erence)?|order)|p\.?\s?o\.?\s*(?:no\.?|number)|cust(?:omer)?\s+(?:order|ref))\s*[:.#]?\s*([A-Z0-9][A-Z0-9/\-]{1,})", re.I)


def run(cmd, timeout=180):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout


# --------------------------------------------------------------------------- text
def get_text(pdf, max_pages=4, dpi=300, lang="eng"):
    """Returns (text, source). Uses the PDF's own text layer when it has one, otherwise Tesseract."""
    pdf = Path(pdf)
    if pdf.suffix.lower() == ".pdf":
        try:
            t = run(["pdftotext", "-layout", "-l", str(max_pages), str(pdf), "-"], 60)
            if len(re.sub(r"\s", "", t)) > 150:
                return t, "text_layer"
        except Exception:
            pass
    with tempfile.TemporaryDirectory() as td:
        if pdf.suffix.lower() == ".pdf":
            run(["pdftoppm", "-r", str(dpi), "-gray", "-png", "-l", str(max_pages), str(pdf), f"{td}/p"])
            images = sorted(Path(td).glob("p*.png"))
        else:
            images = [pdf]
        pages = []
        for img in images:
            try:                                  # light despeckle helps Tesseract on noisy scans
                from PIL import Image, ImageFilter
                clean = Path(td) / f"clean_{img.stem}.png"
                Image.open(img).convert("L").filter(ImageFilter.MedianFilter(3)).save(clean)
                img = clean
            except Exception:
                pass
            pages.append(run(["tesseract", str(img), "stdout", "-l", lang, "--psm", "6"]))
    return "\n".join(pages), "tesseract"


# --------------------------------------------------------------------------- helpers
def money(s):
    s = s.replace("R", "").strip()
    if re.search(r"\d[ ,]\d{3}", s) and re.search(r"[.,]\d{2}$", s):
        s = s[:-3].replace(" ", "").replace(",", "").replace(".", "") + "." + s[-2:]
    else:
        s = s.replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def amounts(line):
    return [v for v in (money(m.group(1)) for m in MONEY.finditer(line)) if v is not None]


def labelled_amount(lines, i):
    vals = amounts(lines[i])
    if not vals and i + 1 < len(lines):
        vals = amounts(lines[i + 1])
    return vals[-1] if vals else None


# --------------------------------------------------------------------------- suppliers
class SupplierMap:
    """VAT number -> supplier name. Seeded from a CSV (e.g. Sage vendor export), learned from GPU results."""

    def __init__(self, db, csv_path=None, own_vat=()):
        self.db = db
        self.own = set(own_vat or ())
        db.execute("""CREATE TABLE IF NOT EXISTS suppliers(vat_no TEXT PRIMARY KEY, name TEXT, source TEXT, seen INTEGER, at TEXT)""")
        db.commit()
        if csv_path and Path(csv_path).exists():
            self.load_csv(csv_path)

    def load_csv(self, path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return 0
        cols = list(rows[0])
        vcol = next((c for c in cols if re.search(r"vat|tax", c, re.I)), None)
        ncol = next((c for c in cols if re.search(r"name|supplier|vendor", c, re.I)), None)
        n = 0
        for r in rows:
            v = re.sub(r"\D", "", r.get(vcol) or "") if vcol else ""
            if ncol and re.fullmatch(r"4\d{9}", v) and v not in self.own:
                self.db.execute("INSERT INTO suppliers VALUES(?,?,?,0,datetime('now')) ON CONFLICT(vat_no) DO UPDATE SET name=excluded.name, source='csv'",
                                (v, r[ncol].strip(), "csv"))
                n += 1
        self.db.commit()
        return n

    def lookup(self, vat_no):
        row = self.db.execute("SELECT name FROM suppliers WHERE vat_no=?", (vat_no,)).fetchone()
        return row[0] if row else None

    def learn(self, vat_no, name):
        v = re.sub(r"\D", "", vat_no or "")
        if not name or not re.fullmatch(r"4\d{9}", v) or v in self.own:
            return
        self.db.execute("""INSERT INTO suppliers VALUES(?,?,'learned',1,datetime('now'))
                           ON CONFLICT(vat_no) DO UPDATE SET seen=seen+1, at=datetime('now'),
                           name=CASE WHEN source='csv' THEN name ELSE excluded.name END""", (v, name.strip()))
        self.db.commit()


# --------------------------------------------------------------------------- parsing
ITEMTOK = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d{2,5})?|\d{1,7}(?:[.,]\d{1,5})?")


def _num(tok):
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", tok):      # 5,978.26 / 11,782.60
        t = tok.replace(",", "")
    elif re.fullmatch(r"\d+,\d{1,5}", tok):                     # 122,50 (decimal comma)
        t = tok.replace(",", ".")
    else:
        t = tok.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _groupings(tail):
    """Ways to read trailing tokens as numbers when a space may separate thousands:
    '16 239.13' -> [16, 239.13] or [16239.13]. Returns (joins, [(match, text, value)]), fewest joins first."""
    out = []

    def walk(i, acc, joins):
        if i == len(tail):
            out.append((joins, list(acc)))
            return
        m, t = tail[i]
        walk(i + 1, acc + [(m, t, _num(t))], joins)
        if re.fullmatch(r"\d{1,3}", t):                        # "1 234 567.89", "16 239.13", "4 296.60"
            txt, j = t, i + 1
            while j < len(tail) and re.fullmatch(r"\d{3}(?:[.,]\d{2})?", tail[j][1]):
                txt += tail[j][1].replace(",", ".")
                if re.search(r"[.,]\d{2}$", tail[j][1]):
                    walk(j + 1, acc + [(m, tail[j][1], _num(txt))], joins + 1)
                    break
                j += 1

    if len(tail) <= 10:
        walk(0, [], 0)
    else:
        out.append((0, [(m, t, _num(t)) for m, t in tail]))
    out.sort(key=lambda x: x[0])
    return out


def _is_money(t, places=(2,)):
    return any(re.search(r"[.,]\d{%d}$" % n, t) for n in places)


def _match_line(nums, joins, rates=(0.15, 0.14)):
    """Find qty x price = amount in one reading of a line's trailing numbers.
    Returns (key, (first_match, qty, price, amount)); lower key = better reading."""
    best = None
    n = len(nums)
    targets = []
    if n >= 3 and nums[-1][2] and _is_money(nums[-1][1]):
        targets.append((n - 1, nums[-1][2]))                    # amount is the last column
    if n >= 4 and _is_money(nums[-1][1]) and _is_money(nums[-2][1]) and nums[-1][2] and nums[-2][2]:
        ex = round(nums[-1][2] - nums[-2][2], 2)                # ... tax | total incl. -> amount excl. = incl - tax
        if ex > 0 and any(abs(nums[-2][2] - r * ex) <= max(0.02, 0.005 * ex) for r in rates):
            targets.append((n - 2, ex))
    for ai, amount in targets:
        for pi in range(ai - 1, -1, -1):
            _, pt, price = nums[pi]
            if not price or not _is_money(pt, (2, 3, 4, 5)):
                continue
            for qi in range(ai - 1, -1, -1):
                if qi == pi:
                    continue
                qm, qt, q = nums[qi]
                if not q or not 0 < q <= 100000 or abs(q * price - amount) > max(0.02, 0.002 * amount):
                    continue
                if qi > pi and _is_money(qt, (3, 4, 5)):
                    continue                                    # a qty after the price must look like a count
                lo = min(pi, qi)
                strays = sum(1 for k in range(lo, ai) if k not in (pi, qi))   # unused numbers in between
                key = (strays, 0 if qi < pi else 1, joins)    # usual qty-before-price order beats fewer joins
                if best is None or key < best[0]:
                    first = nums[lo][0]
                    best = (key, (first, q, price, round(amount, 2)))
    return best


def parse_items(lines):
    """Lines where some quantity x some price equals the line amount.

    Works on whitespace-separated tokens, because OCR and PDF text often merge or split columns:
    "16 239.13" is one number with a space for thousands, a text 'note' column can sit between price
    and amount, the unit price can come before the quantity (meter billing), and some layouts end with
    tax | total incl. Every reading is tried; qty x price = amount (with no stray numbers in between)
    picks the right one. A line with numbers but no words takes its description from the line above."""
    items = []
    prev_text = None
    for raw in lines:
        if L_SUB.search(raw) or L_TOT.search(raw):
            prev_text = None
            continue
        ln = re.sub(r"(?<=\d)[:;](?=\d{2}\b)", ".", raw)          # OCR: 22:75 -> 22.75
        toks = list(re.finditer(r"\S+", ln))
        tail, skipped = [], 0
        for m in reversed(toks):
            t = m.group(0).lstrip("R").rstrip(".") if m.group(0) not in ("R",) else ""
            if t and ITEMTOK.fullmatch(t):
                tail.insert(0, (m, t))
            elif tail and skipped < 3 and len(m.group(0)) <= 8 and not re.search(r"[a-z]", m.group(0)):
                skipped += 1                                   # EA / R / ZAR / a note code like 'UBW'
            elif tail:
                break
            elif not re.search(r"[a-z]", m.group(0)) and len(m.group(0)) <= 8 and skipped < 3:
                skipped += 1                                   # trailing code after the amount
            else:
                break
        best = None
        if len(tail) >= 3:
            for joins, nums in _groupings(tail):
                r = _match_line(nums, joins)
                if r and (best is None or r[0] < best[0]):
                    best = r
        if not best or best[0][0] > 1:
            if re.search(r"[A-Za-z]{3,}", ln) and len(tail) < 2:
                prev_text = ln.strip()
            continue
        m_q, q, p, amount = best[1]
        head = ln[:m_q.start()].strip(" -|:")
        code = None
        hs = head.split()
        if re.search(r"[A-Za-z]{2,}", head) and len(hs) >= 2 and re.fullmatch(r"[A-Z0-9][A-Z0-9\-/.]{2,}", hs[0]) and re.search(r"\d", hs[0]):
            code, head = hs[0], " ".join(hs[1:])
        if not re.search(r"[A-Za-z]{2,}", head):
            if not prev_text:
                continue
            head = prev_text                                   # description printed on the line above
            ps = head.split()
            if code is None and len(ps) >= 2 and re.fullmatch(r"[A-Z0-9][A-Z0-9\-/.]{0,}", ps[0]) and re.search(r"\d", ps[0]):
                code, head = ps[0], " ".join(ps[1:])
        prev_text = None
        items.append({"item_code": code, "description": re.sub(r"\s{2,}", " ", head), "quantity": q,
                      "unit_price": p, "amount": amount})
    return items

def pick_totals(subs, vats, tots, rates=(0.15, 0.14)):
    """Invoices print several 'totals' (Total excl, Total (Incl), a table header 'Sub Total' above a
    line value...). Pick the subtotal/VAT/total that agree with each other, latest on the page first
    (summary blocks sit at the bottom); fall back to the first subtotal/VAT and the largest total."""
    S = list(dict.fromkeys(reversed(subs)))
    V = list(dict.fromkeys(reversed(vats)))
    T = list(dict.fromkeys(reversed(tots)))
    for t in T:
        for v in V:
            for s in S:
                if abs(s + v - t) <= 1.0:
                    return s, v, t
            s = round(t - v, 2)                        # no printed subtotal ("Charges:" etc): derive it
            if s > 0 and any(abs(v - r * s) <= max(1.0, 0.005 * s) for r in rates):
                return s, v, t
    sub = subs[0] if subs else None
    vat = vats[0] if vats else None
    tot = max(tots) if tots else None
    if sub is None and tot is not None and vat is not None:
        sub = round(tot - vat, 2)
    return sub, vat, tot


def ocr_made_text_layer(pdf):
    """True when the PDF's text layer was added by an OCR tool (OCRmyPDF/Tesseract, ABBYY, scanner software...):
    its digits can be misread just like Tesseract's, so treat it as OCR text, not as born-digital text."""
    try:
        info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return False
    meta = " ".join(l for l in info.splitlines() if l.startswith(("Creator", "Producer")))
    return bool(re.search(r"ocr|tesseract|abbyy|finereader|readiris|omnipage|paperport|naps2", meta, re.I))


def mentions_invoice(text):
    return bool(re.search(r"\binvoice\b", text or "", re.I))


def _is_own(v, own):
    """MPS's own VAT number, or one digit off it (a supplier's system or the OCR got a digit wrong)."""
    return any(len(v) == len(o) and sum(a != b for a, b in zip(v, o)) <= 1 for o in own)


def supplier_vat(text, own_vat=(), suppliers=None, known_only=False):
    """The supplier's VAT number in the text, or (None, None). Invoices to MPS also print MPS's own number, and
    often bank account numbers that look like VAT numbers, so: labelled numbers first, the Sage list breaks ties.
    known_only: accept only a number on the Sage / learned list (for Tesseract text, which can misread digits)."""
    own = set(own_vat or ())
    labelled = [v for v in dict.fromkeys(VAT_LABELLED.findall(text or "")) if not _is_own(v, own)]
    anywhere = [v for v in dict.fromkeys(VATNO.findall(text or "")) if not _is_own(v, own)]
    known = lambda vs: [v for v in vs if suppliers is not None and suppliers.lookup(v)]
    order = [(known(labelled), "labelled VAT number on the Sage list"), (known(anywhere), "only VAT number in the text on the Sage list")]
    if not known_only:
        order[1:1] = [(labelled, "only labelled VAT number in the text")]
        order.append((anywhere, "only VAT-like number in the text"))
    for vs, how in order:
        if len(vs) == 1:
            return vs[0], how
    return None, None


def invoice_number_column(text):
    """Invoice number printed under a column heading ("Acc  Document No  Date" with the values on the next line),
    or after a label on the same line. Returns (number, how) or (None, None)."""
    lines = (text or "").splitlines()
    is_invoice = bool(re.search(r"\binvoice\b", text or "", re.I))
    for lab in INVNO_COLUMN_LABELS:
        if lab.startswith("Doc") and not is_invoice:       # "Doc No" on a bill of lading, delivery note... isn't an invoice number
            continue
        rx = re.compile(r"(?i)(?<![A-Za-z])" + r"\s*".join(map(re.escape, lab.split())) + r"\.?(?![A-Za-z])")
        for i, line in enumerate(lines):
            m = rx.search(line)
            if not m:
                continue
            same = re.match(r"\s*[.:#]+\s*([A-Z0-9][A-Z0-9\-/]{2,30})", line[m.end():], re.I)
            if same and re.search(r"\d", same.group(1)):
                return same.group(1).upper(), f"after '{lab}'"
            for nxt in lines[i + 1:i + 3]:
                if not nxt.strip():
                    continue
                for t in re.finditer(r"\S+", nxt):
                    tok = t.group()
                    if t.start() <= m.end() + 2 and t.end() >= m.start() - 2 and re.search(r"\d", tok) \
                            and re.fullmatch(r"[A-Z0-9][A-Z0-9\-/]{2,30}", tok, re.I) \
                            and not re.fullmatch(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}", tok):
                        return tok.upper(), f"under the '{lab}' column heading"
                break
    return None, None


def parse(text, suppliers, parse_date, own_vat=()):
    lines = [l for l in text.splitlines() if l.strip()]
    f = {k: None for k in ("supplier_name", "supplier_vat_no", "invoice_number", "invoice_date", "due_date",
                           "order_ref", "job_number", "currency", "subtotal", "vat", "total")}
    f["currency"] = "ZAR"

    v, _how = supplier_vat(text, own_vat, suppliers)
    if v:
        f["supplier_vat_no"], f["supplier_name"] = v, suppliers.lookup(v)
    else:                                    # several candidates: the first known one, else the first one (as before)
        vats = [v for v in dict.fromkeys(VATNO.findall(text)) if v not in set(own_vat)]
        known = [(v, suppliers.lookup(v)) for v in vats]
        known = [(v, n) for v, n in known if n]
        if known:
            f["supplier_vat_no"], f["supplier_name"] = known[0]
        elif vats:
            f["supplier_vat_no"] = vats[0]

    subs, vats_amt, tots = [], [], []
    for i, ln in enumerate(lines):
        if f["invoice_number"] is None:
            m = L_INVNO.search(ln)
            if m and re.search(r"\d", m.group(1)):
                inv = m.group(1).upper().strip("-/_")
                more = re.match(r" (\d{3,})(?=\s{2,}|\s*$)", ln[m.end():])
                if more and inv.isdigit():             # "4001 01398705": one number printed in two groups
                    inv += more.group(1)
                f["invoice_number"] = inv
        if f["due_date"] is None:
            m = L_DUE.search(ln)
            if m:
                f["due_date"] = parse_date(m.group(1))
        if f["invoice_date"] is None and not L_DUE.search(ln):
            m = L_DATE.search(ln)
            if m:
                f["invoice_date"] = parse_date(m.group(1))
        if f["order_ref"] is None:
            m = L_ORDER.search(ln)
            if m and re.search(r"\d", m.group(1)):
                f["order_ref"] = m.group(1).upper()
        if L_SUB.search(ln):
            v = labelled_amount(lines, i)
            if v is not None:
                subs.append(v)
        elif L_TOT.search(ln) or L_ANYTOT.search(ln):
            v = labelled_amount(lines, i)
            if v is not None:
                tots.append(v)
        elif L_VAT.search(ln) and not L_VAT_NOT.search(ln):
            v = labelled_amount(lines, i)
            if v is not None:
                vats_amt.append(v)
    if f["invoice_number"] is None:
        f["invoice_number"] = invoice_number_column(text)[0]
    f["subtotal"], f["vat"], f["total"] = pick_totals(subs, vats_amt, tots)
    if f["invoice_date"] is None:            # fall back to the first date anywhere that isn't a due date
        for ln in lines:
            if L_DUE.search(ln):
                continue
            for m in re.finditer(r"\b(\d{1,4}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b", ln):
                d = parse_date(m.group(1))
                if d:
                    f["invoice_date"] = d
                    break
            if f["invoice_date"]:
                break
    jobs = JOBNO.findall(text)
    if jobs:
        f["job_number"] = jobs[0]
    f["items"] = parse_items(lines)
    return f


def validate(f, cfg):
    reasons = []
    if not f["supplier_name"]:
        reasons.append("supplier_unknown_vat" if f["supplier_vat_no"] else "no_supplier_vat_no")
    for k in ("invoice_number", "invoice_date", "total"):
        if f.get(k) in (None, ""):
            reasons.append(f"no_{k}")
    if f.get("invoice_date"):
        d = date.fromisoformat(f["invoice_date"])
        if d > date.today() or d.year < date.today().year - 10:
            reasons.append("implausible_date")
    sub, vat, tot = f.get("subtotal"), f.get("vat"), f.get("total")
    if None in (sub, vat, tot):
        reasons.append("no_vat_breakdown")
    else:
        if abs(sub + vat - tot) > 1.0:
            reasons.append("totals_dont_add_up")
        rates = cfg.get("vat_rates", [0.15, 0.14])
        if vat != 0 and not any(abs(vat - r * sub) <= max(1.0, 0.005 * sub) for r in rates):
            reasons.append("vat_rate_unexpected")
    if cfg.get("require_items", True):
        items = f.get("items") or []
        if not items:
            reasons.append("no_line_items")
        else:
            s = round(sum(i["amount"] for i in items), 2)
            if not any(t is not None and abs(s - t) <= 1.0 for t in (sub, tot)):
                reasons.append("line_items_dont_sum")
    return reasons


def extract(pdf, db_suppliers, parse_date, cfg, max_pages=4, text=None):
    """Returns (fields, reasons, text, text_source). Empty reasons means accepted on CPU.
    text: (text, source) already read from these pages (inv_split reads each page once), so no second OCR run."""
    text, source = text or get_text(pdf, max_pages, int(cfg.get("ocr_dpi", 300)), cfg.get("ocr_lang", "eng"))
    f = parse(text, db_suppliers, parse_date, cfg.get("own_vat_numbers", []))
    return f, validate(f, cfg), text, source

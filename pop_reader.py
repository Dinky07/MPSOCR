"""Proofs of payment (bank payment confirmations filed with the orders), first layout: Standard Bank
"CUSTOMER ALL PAYMENTS FINAL AUDIT REPORT" printouts, scanned (no text layer).

Two independent readings of every proof of payment:
  * the vision model (Ollama, via the worker's extract_builtin with SCHEMA / PROMPT below), and
  * Tesseract + the fixed Standard Bank layout (parse_standard_bank).
They are compared field by field (merge): where they agree the value is safe; where they disagree the model's value is
kept and the document is flagged, because Tesseract reads the 1 in "13,078.66" as a 4 on these 200 dpi bilevel scans.

Also used on purchase-order scans: is_payment_page() spots payment pages inside a PO file (the file stays one document
in the Purchase Orders library, with "Proof of Payment" added to its Document Type).
"""
import os
import re
import subprocess
import tempfile
from datetime import date

PAYMENT_PAGE = re.compile(
    r"FINAL\s+AUDIT\s+REPORT|ALL\s+PAYMENTS|Statement\s+Ref|Creditor\s+Code|PROOF\s+OF\s+PAYMENT|PAYMENT\s+CONFIRMATION|"
    r"Payment\s+Notification|Beneficiary\s+(Name|Reference)", re.I)


def _s(desc):
    return {"type": ["string", "null"], "description": desc}


def _n(desc):
    return {"type": ["number", "null"], "description": desc}


SCHEMA = {
    "type": "object",
    "properties": {
        "bank": _s("Bank that issued the confirmation, e.g. Standard Bank"),
        "date": _s("Payment / action date, YYYY-MM-DD"),
        "amount": _n("Amount paid (Rand), a plain number"),
        "statement_ref": _s("Statement reference (shown on the beneficiary's statement), labelled 'Statement Ref'"),
        "account_name": _s("Beneficiary account name (who was paid), labelled 'Account Name'"),
        "creditor_code": _s("Creditor code, labelled 'Creditor Code'"),
        "beneficiary_account": _s("Beneficiary account number, labelled 'Acc No / CDI' or 'Account Number'"),
        "bank_reference": _s("The bank's batch / payment reference, labelled 'Reference' in the header"),
        "from_account_name": _s("Account paid from, labelled 'From Account Name'"),
        "payments_on_page": _n("How many separate payments (Trans No blocks) are on the page"),
    },
    "required": ["date", "amount", "account_name"],
}

PROMPT = ("This is a bank proof of payment / payment confirmation for a payment made by Mining Pressure Systems or a group "
          "company to a supplier (often a Standard Bank 'CUSTOMER ALL PAYMENTS FINAL AUDIT REPORT'). Read the printed values "
          "exactly. date = the Action date as YYYY-MM-DD (20211129 -> 2021-11-29). amount = the Amount as a plain number "
          "(13,078.66 -> 13078.66); read every digit carefully. account_name = the beneficiary 'Account Name' (who was paid), "
          "never the 'From Account Name' or 'User Name'. Use null for anything not printed.")


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[R\s]", "", str(v)).replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _iso(v):
    s = str(v or "").strip()
    m = re.fullmatch(r"(20\d\d)[-/.]?(\d\d)[-/.]?(\d\d)", s)
    if not m:
        m2 = re.fullmatch(r"(\d\d)[-/.](\d\d)[-/.](20\d\d)", s)
        if not m2:
            return None
        m = (None, m2.group(3), m2.group(2), m2.group(1))
        y, mo, d = m[1], m[2], m[3]
    else:
        y, mo, d = m.group(1), m.group(2), m.group(3)
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def _words(text):
    """Real-looking words in OCR text: a sideways page gives mostly 1-3 character noise."""
    return len(re.findall(r"[A-Za-z]{4,}", text or ""))


def _rotated(png, angle):
    """A copy of png turned by angle degrees (PIL, else ImageMagick); None when neither is available."""
    out = png[:-4] + f"_r{angle}.png"
    try:
        from PIL import Image
        with Image.open(png) as im:
            im.rotate(angle, expand=True).save(out)
        return out
    except Exception:
        pass
    try:
        subprocess.run(["convert", png, "-rotate", str(-angle), out], check=True, capture_output=True, timeout=120)
        return out
    except Exception:
        return None


def ocr_pages(pdf, max_pages=4, dpi=300, upright=False):
    """Tesseract text per page (list). upright=True: a page that reads as noise (a landscape report scanned sideways)
    is read again turned a quarter each way, and the best reading kept."""
    def tess(f):
        return subprocess.run(["tesseract", f, "stdout", "--psm", "6"], capture_output=True, text=True, timeout=300).stdout

    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", "-l", str(max_pages), str(pdf), os.path.join(td, "p")],
                       check=True, capture_output=True, timeout=300)
        out = []
        for f in sorted(x for x in os.listdir(td) if "_r" not in x):
            src = os.path.join(td, f)
            text = tess(src)
            if upright and not is_payment_page(text) and _words(text) < 40:
                for angle in (90, 270):
                    r = _rotated(src, angle)
                    t2 = tess(r) if r else ""
                    if _words(t2) > _words(text):
                        text = t2
                    if is_payment_page(text):
                        break
            out.append(text)
        return out


def is_payment_confirmation(text):
    """Stricter than is_payment_page, for a single page that must be a payment confirmation on its own (not a credit
    application or statement that merely mentions bank details): a Standard Bank audit report, or a page headed
    Payment Confirmation / Payment Notification / Proof of Payment near the top."""
    t = text or ""
    if re.search(r"FINAL\s+AUDIT\s+REPORT|CUSTOMER\s+ALL\s+PAYMENTS", t, re.I) or parse_standard_bank(t).get("amount"):
        return True
    top = "\n".join(t.splitlines()[:15])
    return bool(re.search(r"PAYMENT\s+(CONFIRMATION|NOTIFICATION)|PROOF\s+OF\s+PAYMENT", top, re.I)) and \
        bool(re.search(r"Beneficiary|Recipient|Amount", t, re.I))


def is_payment_page(text):
    return len(PAYMENT_PAGE.findall(text or "")) >= 2 or bool(re.search(r"FINAL\s+AUDIT\s+REPORT", text or "", re.I))


def parse_standard_bank(text):
    """Fields from Tesseract text of one Standard Bank audit-report page; {} when it isn't that layout."""
    t = text or ""
    if not re.search(r"Standard\s+Bank|AUDIT\s+REPORT", t, re.I):
        return {}

    def grab(label, rest=r"(.+?)\s*$"):
        m = re.search(r"^\s*" + label + r"\s*[:.]?\s*" + rest, t, re.I | re.M)
        return re.sub(r"[\s\]\[|_~'`.,;:-]+$", "", m.group(1)).strip() if m else None   # OCR noise at the line end

    amount = grab(r"Amount", r"([0-9][0-9,]*\.\d\d)")
    acc_no = grab(r"Acc\s*No\s*/?\s*CDI", r"(\d{6,})")
    f = {
        "bank": "Standard Bank",
        "date": _iso(grab(r".*?Action\s*date", r"(\d{8})")),
        "amount": _num(amount),
        "statement_ref": grab(r"Statement\s*Ref"),
        "account_name": grab(r"Account\s*Name"),
        "creditor_code": grab(r"Creditor\s*Code", r"(\S+)"),
        "beneficiary_account": acc_no,
        "bank_reference": grab(r".*?\bReference", r"(\d{6,})"),
        "from_account_name": grab(r".*?From\s*Account\s*Name"),
        "payments_on_page": len(re.findall(r"^\s*Trans\s*No\b", t, re.I | re.M)) or None,
    }
    return {k: v for k, v in f.items() if v not in (None, "")}


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def merge(model, ocr):
    """Model reading + Tesseract reading -> (fields, flags). The model wins; disagreements are flagged."""
    model = {k: v for k, v in (model or {}).items() if v not in (None, "")}
    f = {
        "bank": model.get("bank") or ocr.get("bank"),
        "date": _iso(model.get("date")) or ocr.get("date"),
        "amount": _num(model.get("amount")) if model.get("amount") is not None else ocr.get("amount"),
        "statement_ref": model.get("statement_ref") or ocr.get("statement_ref"),
        "account_name": model.get("account_name") or ocr.get("account_name"),
        "creditor_code": model.get("creditor_code") or ocr.get("creditor_code"),
        "beneficiary_account": model.get("beneficiary_account") or ocr.get("beneficiary_account"),
        "bank_reference": model.get("bank_reference") or ocr.get("bank_reference"),
        "from_account_name": model.get("from_account_name") or ocr.get("from_account_name"),
        "payments_on_page": model.get("payments_on_page") or ocr.get("payments_on_page"),
    }
    flags, checks = [], []
    if ocr:
        a_m, a_o = _num(model.get("amount")), ocr.get("amount")
        if a_m is not None and a_o is not None and abs(a_m - a_o) > 0.005:
            checks.append(f"amount: image read R{a_m:,.2f}, OCR read R{a_o:,.2f}")
        d_m = _iso(model.get("date"))
        if d_m and ocr.get("date") and d_m != ocr["date"]:
            checks.append(f"date: image read {d_m}, OCR read {ocr['date']}")
        n_m, n_o = _norm(model.get("account_name")), _norm(ocr.get("account_name"))
        if n_m and n_o and n_m != n_o and n_m not in n_o and n_o not in n_m:
            checks.append(f"account name: image read {model.get('account_name')}, OCR read {ocr.get('account_name')}")
        f["cross_checked"] = not checks
    else:
        f["cross_checked"] = False
        checks.append("not a Standard Bank audit report: amount not cross-checked")
    for k, label in (("date", "missing_date"), ("amount", "missing_amount"), ("account_name", "missing_account_name")):
        if f.get(k) in (None, ""):
            flags.append(label)
    if (f.get("payments_on_page") or 1) > 1:
        checks.append(f"{int(f['payments_on_page'])} payments on one page: only the first is recorded")
    f["checks"] = checks
    f["review"] = "; ".join(checks) or None
    f["library_doc_type"] = "Proof of Payment"
    flags += ["check: " + c for c in checks]
    return {k: v for k, v in f.items()}, flags

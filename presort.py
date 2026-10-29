#!/usr/bin/env python3
"""Pre-sort for the Purchase Orders queue (Dino 27 Oct, after the independent review of the first sorter): before the
purchase-order reader reads a page, decide on the CPU whether it is something else - supplier invoice, MPS delivery
note, proof of payment, workshop sheet, job transfer, test certificate, pro forma, quotation, statement, credit note,
supplier delivery note - so it can go to its own queue instead of costing a full 49k-context order read.

SHADOW first: the worker records the verdict in the sidecar ("presort") and changes nothing; presort_eval.py scores it
against the hand-labelled test set. Diverting only after it passes the gate.

verdict(pdf, name=None, folder=None, texts=None) -> {
    "kind":  the document kind (see KINDS), "unsure" when nothing decides it,
    "dest":  the queue it belongs in (PurchaseOrders keeps pro formas, quotations, statements, cancelled orders...),
    "strength": "sure" | "likely" | "unsure",
    "signals": [why, ...], "prior": hint from file name / folder (recorded, never decides), "text": text source, "secs"}
Signals, cheapest first, title area (top ~14 lines) for document titles, word boundaries everywhere:
  * delivery-note barcode (zbarimg: IN + 5-7 digits)                     -> delivery_note         sure
  * pop_reader.is_payment_page (the worker's own payment-page test)     -> proof_of_payment      likely
  * title words: TAX INVOICE / PRO FORMA / QUOTATION / STATEMENT / CREDIT NOTE / DELIVERY NOTE / JOB TRANSFER /
    INSPECTION or MILL TEST CERTIFICATE / PURCHASE ORDER (+ supporting body signals: supplier VAT, invoice no, heat no,
    EN 10204, MPS / Polymer form wording)
  * ws_reader.looks_like_ws (the worker's own workshop-sheet test)       -> workshop_sheet        likely
Two signals pointing at different kinds -> unsure. "sure" is kept for the barcode and for a title backed by a body
signal; everything else is "likely" until the test set shows it is right >= 99.5 % of the time.
"""
import os, re, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import pop_reader                     # noqa: E402
except Exception:                         # pragma: no cover
    pop_reader = None
try:
    import ws_reader                      # noqa: E402
except Exception:                         # pragma: no cover
    ws_reader = None

VERSION = "presort-4b (29 Oct: Ronin orders)"
KINDS = {
    "purchase_order": "PurchaseOrders", "polymer_order": "PurchaseOrders", "pro_forma": "PurchaseOrders",
    "quotation": "PurchaseOrders", "statement": "PurchaseOrders", "credit_note": "PurchaseOrders",
    "supplier_delivery_note": "PurchaseOrders",
    "ronin_order": "RoninOrders",
    "supplier_invoice": "SupplierInvoices", "delivery_note": "DeliveryNotes", "proof_of_payment": "ProofOfPayments",
    "workshop_sheet": "WorkshopSheets", "job_transfer": "JobTransfers", "test_cert": "TestCerts",
}
OWN_VAT = {"4320123039"}
VATNO = re.compile(r"(?<!\d)(4\d{9})(?!\d)")


def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def page_text(pdf, texts=None):
    """(text of page 1, source). Reuses texts the worker already has (payment-page OCR); else the text layer; else
    Tesseract with the page turned upright (pop_reader.ocr_pages(upright=True))."""
    if texts and (texts[0] or "").strip():
        return texts[0], "worker OCR"
    try:
        t = _run(["pdftotext", "-layout", "-f", "1", "-l", "1", str(pdf), "-"], 60).stdout.decode("utf8", "ignore")
        if len(re.sub(r"\s", "", t)) > 80:
            return t, "text layer"
    except Exception:
        pass
    if pop_reader:
        try:
            return (pop_reader.ocr_pages(pdf, 1, dpi=200, upright=True) or [""])[0], "tesseract"
        except Exception:
            pass
    return "", "no text"


def barcodes(pdf):
    with tempfile.TemporaryDirectory() as td:
        try:
            _run(["pdftoppm", "-r", "150", "-gray", "-f", "1", "-l", "1", "-png", "-singlefile", str(pdf), f"{td}/b"])
            return _run(["zbarimg", "-q", "--raw", f"{td}/b.png"], 60).stdout.decode("utf8", "ignore").split()
        except Exception:
            return []


def _title(text, n=14):
    lines = [l.strip() for l in (text or "").splitlines() if len(l.strip()) > 2]
    return re.sub(r"\s+", " ", " ".join(lines[:n])).upper()


def prior(name, folder):
    s = f"{folder or ''} {name or ''}".upper()
    for pat, kind in ((r"PROOF OF PAYMENT", "proof_of_payment"), (r"PRO ?FORMA", "pro_forma"), (r"\bQUOT|\bQUO\d", "quotation"),
                      (r"\bJT ?\d", "job_transfer"), (r"\bINV(OICE)?\b|\bINV\d|\bSINV\d", "supplier_invoice"),
                      (r"CANCEL", "purchase_order"), (r"\bSTATEMENT\b", "statement")):
        if re.search(pat, s):
            return kind
    return None


def verdict(pdf, name=None, folder=None, texts=None):
    t0 = time.time()
    pdf = Path(pdf)
    text, src = page_text(pdf, texts)
    T = _title(text)                                       # title area
    B = re.sub(r"\s+", " ", text or "").upper()            # whole page
    has = lambda pat, s=B: re.search(pat, s) is not None
    votes = []                                             # (kind, strength, why)

    codes = [c for c in barcodes(pdf) if re.fullmatch(r"IN\d{5,7}", c.upper())]
    if codes:
        votes.append(("delivery_note", "sure", f"delivery-note barcode {codes[0]}"))

    vat = [v for v in dict.fromkeys(VATNO.findall(B)) if v not in OWN_VAT]
    inv_no = has(r"\bINV(OICE)?\.? ?(NO|NUMBER|NR|#)\b|\bDOCUMENT NO\b")
    heat = has(r"\b(HEAT|CAST|MELT) ?(NO|NUMBER|NR)\b|\bEN ?10204\b|\b3\.1 ?B?\b.*\bCERTIFICATE\b")
    mps_form = has(r"\bMINING PRESSURE SYSTEMS\b|\bKUSASA\b", T) and has(r"\bPURCHASE ORDER\b|\bORDER NO\b|\bINTER ?GROUP\b|\bIGT\b", T)

    if has(r"\bPRO ?-?FORMA\b", T):
        votes.append(("pro_forma", "likely", "title: pro forma"))
    elif has(r"\bTAX INVOICE\b", T) and (vat or inv_no):
        votes.append(("supplier_invoice", "sure" if (vat and inv_no) else "likely", "title: tax invoice" + (" + supplier VAT" if vat else "") + (" + invoice no" if inv_no else "")))
    elif has(r"\bTAX INVOICE\b", T):
        votes.append(("supplier_invoice", "likely", "title: tax invoice"))
    if has(r"\bCREDIT NOTE\b", T):
        votes.append(("credit_note", "likely", "title: credit note"))
    if has(r"\bQUOTATION\b|\bQUOTE\b", T) and not has(r"\bTAX INVOICE\b", T):
        votes.append(("quotation", "likely", "title: quotation"))
    if has(r"\bSTATEMENT\b", T) and has(r"\bBALANCE\b|\bAGE(ING|D)\b|\bCURRENT\b|\b(30|60|90) DAYS\b"):
        votes.append(("statement", "likely", "title: statement + ageing/balance"))
    if has(r"\bJOB TRANSFER\b", T):
        votes.append(("job_transfer", "sure" if has(r"\bJT ?\d{4,6}\b|\bTRANSFER(RED)? (FROM|TO)\b") else "likely", "title: job transfer"))
    if has(r"\b(INSPECTION|MILL TEST|MATERIAL TEST|TEST) CERTIFICATE\b|\bCERTIFICATE OF (CONFORMITY|COMPLIANCE)\b", T) and heat:
        votes.append(("test_cert", "likely", "title: certificate + heat / EN 10204"))
    if has(r"\bDELIVERY NOTE\b|\bWAYBILL\b|\bGOODS RECEIVED\b", T) and not codes and not has(r"\bTAX INVOICE\b", T):
        if has(r"\bMINING PRESSURE SYSTEMS\b", T) and has(r"\bIN ?\d{5,7}\b"):
            votes.append(("delivery_note", "likely", "title: MPS delivery note, IN number, no barcode read"))
        else:
            votes.append(("supplier_delivery_note", "likely", "title: delivery note"))
    ronin_head = (has(r"TILE\s*&\s*ALL\s*BLACK", T) or has(r"\b4570132524\b") or has(r"\bRONIN\b", T)
                  or has(r"\bFL\w{2,5}S\s+(AND|&)\s+F\w{4,6}S\b", T))
    ronin_df = re.search(r"\bORDER\s*:?\s*[DP]F\s*-?\s*(\d{4})\b", B)
    ronin_form = has(r"JUMBO\s*PRESS|\bSIGNATURE\b")
    if ronin_head and (ronin_df or has(r"\bORDER\s*:?\s*[DP]F\b", B)) and not has(r"\bTAX INVOICE\b|\bSTATEMENT\b|\bCREDIT NOTE\b", T):
        votes.append(("ronin_order", "sure" if (ronin_df and ronin_form) else "likely",
                      "Ronin order form" + (f" (ORDER DF {ronin_df.group(1)} as OCR'd)" if ronin_df else ", DF number not read")))
    elif mps_form:
        votes.append(("polymer_order" if has(r"\bPOLYMER LINERS\b|\bPL ?\d{4}\b") else "purchase_order", "likely", "MPS order form wording"))
    elif has(r"\bPOLYMER LINERS\b", T) and has(r"\bORDER\b", T):
        votes.append(("polymer_order", "likely", "Polymer order form wording"))
    if pop_reader:
        try:
            if pop_reader.is_payment_page(text):
                votes.append(("proof_of_payment", "likely", "payment page (pop_reader)"))
        except Exception:
            pass
    if ws_reader and not votes:                           # the drawing check costs a render: only when nothing else fired
        try:
            ok, score, why = ws_reader.looks_like_ws(pdf, name or pdf.name, text=text)
            if ok:
                votes.append(("workshop_sheet", "likely", f"workshop sheet ({why})"))
        except Exception:
            pass

    kinds = list(dict.fromkeys(k for k, _, _ in votes))
    if not kinds:
        kind, strength = "unsure", "unsure"
    elif len(kinds) == 1:
        kind = kinds[0]
        strength = "sure" if any(s == "sure" for k, s, _ in votes) else "likely"
    else:
        sure = [k for k, s, _ in votes if s == "sure"]
        if len(set(sure)) == 1 and not ({"delivery_note"} == set(sure) and "supplier_invoice" in kinds):
            kind, strength = sure[0], "likely"             # a sure signal against a likely one: kept, but not sure
        else:
            kind, strength = "unsure", "unsure"            # conflicting signals: a person decides
    return {"version": VERSION, "kind": kind, "dest": KINDS.get(kind), "strength": strength,
            "signals": [f"{k}: {w} ({s})" for k, s, w in votes], "prior": prior(name or pdf.name, folder),
            "text": src, "secs": round(time.time() - t0, 1),
            "_page_text": text}                               # for the worker's own checks (popped before the sidecar is written)


if __name__ == "__main__":
    import json
    for f in sys.argv[1:]:
        print(f, json.dumps(verdict(f), indent=1))

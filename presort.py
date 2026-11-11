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

VERSION = "presort-9 (11 Nov: Dino's 629 dashboard moves - test certs, invoices, drawings, inspection reports, customer orders)"
KINDS = {
    "purchase_order": "PurchaseOrders", "polymer_order": "PurchaseOrders", "pro_forma": "PurchaseOrders",
    "quotation": "PurchaseOrders", "statement": "PurchaseOrders", "credit_note": "PurchaseOrders",
    "supplier_delivery_note": "PurchaseOrders",
    "ronin_order": "RoninOrders",
    "supplier_invoice": "SupplierInvoices", "delivery_note": "DeliveryNotes", "proof_of_payment": "ProofOfPayments",
    "workshop_sheet": "WorkshopSheets", "job_transfer": "JobTransfers", "test_cert": "TestCerts",
    "customer_order": "CustomerOrders",
}
# presort-6: kinds that belong in Other Documents (category = Dino's folder); no queue, so the worker never routes them
OD_CATEGORY = {"id_document": "ID", "drivers_licence": "Drivers Licence", "vehicle_handover": "Vehicle Handover",
               "vehicle_registration": "Vehicle RC1", "collection_slip": "Collection Slips", "saheti": "Saheti",
               "letter": "Letter", "medical": "Medical",
               "credit_application": "Credit Applications", "icft": "Ivor Crabbia Family Trust",
               # presort-9: Dino files supplier quotations / pro formas and supplier delivery notes here (131 + 30 moves)
               "quotation": "Quotations", "supplier_delivery_note": "Supplier Delivery Notes",
               "inspection_report": "Inspection Reports"}
SQ_KINDS = {"sales_quotation"}                             # 471b: to Sales Quotes (read there as a quote)
COMMON = re.compile(r"\b(THE|AND|FOR|DATE|TOTAL|VAT|TEL|PAGE|INVOICE|ORDER|NUMBER|QTY|DESCRIPTION|AMOUNT|PRICE|NAME|ADDRESS|SIGNATURE|PTY|LTD)\b")


def upright_text(pdf, text):
    """presort-7: OCR text with no ordinary words (a sideways / upside-down scan) -> Tesseract's orientation check on
    page 1 and, when it is turned, the page read again upright. None when it is upright or can't be judged."""
    if len(COMMON.findall((text or "").upper())) >= 3:
        return None
    try:
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            _run(["pdftoppm", "-png", "-r", "200", "-gray", "-f", "1", "-l", "1", "-singlefile", str(pdf), f"{td}/u"])
            out = _run(["tesseract", f"{td}/u.png", "stdout", "--psm", "0"], 120).stdout.decode("utf8", "ignore")
            rot, conf = re.search(r"Rotate:\s*(\d+)", out), re.search(r"Orientation confidence:\s*([\d.]+)", out)
            deg = int(rot.group(1)) if rot else 0
            if deg not in (90, 180, 270) or not conf or float(conf.group(1)) < 2:
                return None
            with Image.open(f"{td}/u.png") as im:
                im.rotate(-deg, expand=True).save(f"{td}/r.png")
            t2 = _run(["tesseract", f"{td}/r.png", "stdout", "--psm", "6"], 300).stdout.decode("utf8", "ignore")
            return (t2, deg) if len(COMMON.findall(t2.upper())) > len(COMMON.findall((text or "").upper())) else None
    except Exception:
        return None
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
    for pat, kind in ((r"PROOF OF PAYMENT", "proof_of_payment"), (r"PRO ?FORMA", "quotation"), (r"\bQUOT|\bQUO\d", "quotation"),
                      (r"\bJT ?\d", "job_transfer"), (r"\bINV(OICE)?\b|\bINV\d|\bSINV\d", "supplier_invoice"),
                      (r"CANCEL", "purchase_order"), (r"\bSTATEMENT\b", "statement")):
        if re.search(pat, s):
            return kind
    return None


def verdict(pdf, name=None, folder=None, texts=None):
    t0 = time.time()
    pdf = Path(pdf)
    text, src = page_text(pdf, texts)
    if src != "text layer":                                # presort-7: a sideways scan's OCR is noise: read it upright
        u = upright_text(pdf, text)
        if u:
            text, src = u[0], f"{src}, turned {u[1]}"
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
    # presort-5 (labels 30 Oct): letters, goods returned notes and supplier pro formas also carry the MPS name and an
    # order number; the printed "ORDER A" / IGT heading is often below the title area
    # presort-5b: our letterhead heads our own forms; a customer's order names us lower down (as its supplier)
    own_head = has(r"\bMINING PRESSURE SYS|\bKUSASA\b", _title(text, 5)) or has(r"DEEP DOWN,? WE CARE", T)
    own_other = has(r"\bGOODS RETURNED\b|\bCONTRACT\b|\bCERTIFICATE\b|\bTAX INVOICE\b|\bPRO ?-?FORMA\b|\bQUOTATION\b|\bDELIVERY NOTE\b|\bSTATEMENT\b|\bCREDIT NOTE\b|\bYOUR VAT\b", T)
    mps_form = own_head and not own_other and (has(r"\bPURCHASE ORDER\b|\bORDER NO\b|\bINTER ?GROUP\b|\bIGT\b", T)
                                               or has(r"\bORDER\s*:?\s*A\s*[0-9/]|\bINTERNAL\s*GROUP\s*TRANSFER\b|\bI\s*\.?\s*G\s*\.?\s*T\s*\.?\s*NO\b"))

    if has(r"\bPRO ?-?FORMA\b", T):
        votes.append(("quotation", "likely", "title: pro forma"))         # presort-5: one kind with quotations
    elif has(r"\bTAX INVOICE\b", T) and (vat or inv_no):
        votes.append(("supplier_invoice", "sure" if (vat and inv_no) else "likely", "title: tax invoice" + (" + supplier VAT" if vat else "") + (" + invoice no" if inv_no else "")))
    elif has(r"\bTAX INVOICE\b", T):
        votes.append(("supplier_invoice", "likely", "title: tax invoice"))
    # presort-7: "TAX INVOICE" below the title area (Daimler) or ACCOUNT / WARRANTY INVOICE (Hyundai), supplier VAT
    if vat and not any(k == "supplier_invoice" for k, _, _ in votes) and has(r"\bTAX INVOICE\b|\bWARRANTY INVOICE\b") \
            and not has(r"\bPRO ?-?FORMA\b|\bQUOTATION\b|\bCREDIT NOTE\b|\bSTATEMENT\b|\bDELIVERY NOTE\b|\bPURCHASE ORDER\b|\bORDER (NO|NUMBER)\b", T) \
            and (inv_no or has(r"\bBILL TO\b|\bINVOICE (DATE|NUMBER|NO)\b|\bDOCUMENT NO\b")):
        votes.append(("supplier_invoice", "likely", "tax / warranty invoice below the title + supplier VAT + invoice no"))
    # presort-9: a plain INVOICE title (no TAX) with an invoice number, supplier VAT or billing block
    if not any(k == "supplier_invoice" for k, _, _ in votes) and has(r"\bINVOICE\b", T) and not own_head \
            and not has(r"\bPRO ?-?FORMA\b|QUOT\s?ATION|\bQUOTE\b|\bESTIMATE\b|NOT A (VAT|TAX) INVOICE|\bSTATEMENT\b|\bCREDIT NOTE\b"
                        r"|\bPURCHASE ORDER\b|\bDELIVERY NOTE\b|\bCERTIFICAT", T) \
            and (vat or inv_no or has(r"\bINVOICE ?(#|NO\b|NUMBER\b)|\bINV ?-?\d{1,8}\b|\bBILL(ING)? (TO|ADDRESS)\b|\bDUE DATE\b")):
        votes.append(("supplier_invoice", "likely", "title: invoice + invoice no / supplier VAT / billing"))
    if has(r"\bESTIMATE\b", T) and not has(r"\bTAX INVOICE\b|\bINVOICE ?(#|NO\b|NUMBER\b)", T):
        votes.append(("quotation", "likely", "title: estimate"))
    if has(r"\bCREDIT NOTE\b", T):
        votes.append(("credit_note", "likely", "title: credit note"))
    if has(r"\bQUOTATION\b|\bQUOTE\b", T) and not has(r"\bTAX INVOICE\b", T):
        votes.append(("quotation", "likely", "title: quotation"))
    if has(r"\bSTATEMENT\b", T) and has(r"\bBALANCE\b|\bAGE(ING|D)\b|\bCURRENT\b|\b(30|60|90) DAYS\b"):
        votes.append(("statement", "likely", "title: statement + ageing/balance"))
    if has(r"\bJOB TRANSFER\b", T):
        votes.append(("job_transfer", "sure" if has(r"\bJT ?\d{4,6}\b|\bTRANSFER(RED)? (FROM|TO)\b") else "likely", "title: job transfer"))
    # presort-9: plural / OCR-damaged titles, CERTIFICATE OF INSPECTION, and the mill-cert body wording besides heat numbers
    en10204 = has(r"[E£]N ?1020 ?4")
    tc_title = has(r"\b(INSPECTION|MILL ?TEST|MATERIALS? ?TEST|TEST|QUALITY|PRODUCT) CERTI\w{0,3}CATES?\b"
                   r"|\bCERTIFICATE OF (INSPECTION|QUALITY|CONFORMITY|COMPLIANCE|ANALYSIS|TEST)\b|\bINSPECTION CERTI\w*|\bTEST CERTS?\b", T)
    tc_body = heat or en10204 or has(r"\bASTM ?A ?\d{2,4}\b|\bAPI ?5L\b|\bASME ?S?A ?\d{2,4}\b|\bTENSILE\b|\bYIELD\b|\bCHEMICAL COMPOSITION\b")
    tc_invoiceish = has(r"\bTAX INVOICE\b|\bQUOTATION\b|\bPRO ?-?FORMA\b|\bSTATEMENT\b", T)   # certs print "PURCHASE ORDER NO"
    if (tc_title and tc_body or has(r"\bCERTIFICAT\w*", T) and en10204) and not tc_invoiceish:
        votes.append(("test_cert", "likely", "title: certificate + " + ("heat / EN 10204" if heat or en10204 else "mill-cert wording")))
    if not (tc_title or en10204 or heat) and not tc_invoiceish and not has(r"\bMOTOR ?VEHICLE\b|\bRC ?[1I]\b|\bVOERTUIG") \
            and has(r"\bTHIS IS TO CERTIFY\b|\bTEST REPORT\b|\bPRESSURE TEST(ING)?\b|\bINTEGRITY TESTING\b|\bINSPECTION REPORT\b"
                    r"|\bPRESSURE VESSEL CERTIFICATION\b|\bCERTIFICATE OF REGISTRATION\b", T) and not has(r"\bQUOT|\bINVOICE\b|\bDEAR\b", T):
        votes.append(("inspection_report", "likely", "inspection / test report or registration certificate"))
    if has(r"\bDELIVERY NOTE\b|\bWAYBILL\b|\bGOODS RECEIVED\b", T) and not codes and not has(r"\bTAX INVOICE\b", T) \
            and not has(r"\bCERTIFICAT", T):                # presort-5b: certificates quote the delivery note number
        if has(r"\bMINING PRESSURE SYSTEMS\b", T) and has(r"\bIN ?\d{5,7}\b"):
            votes.append(("delivery_note", "likely", "title: MPS delivery note, IN number, no barcode read"))
        else:
            votes.append(("supplier_delivery_note", "likely", "title: delivery note"))
    elif (has(r"\bTANK DIP (BEFORE|AFTER)\b") or has(r"\bDELIVE\s+RY NOTE\b", T)) and not has(r"\bTAX INVOICE\b", T):
        votes.append(("supplier_delivery_note", "likely", "fuel delivery note (tank dip) / split DELIVERY NOTE title"))
    # presort-9: a customer's purchase order to us (Dino's Customer Orders moves): their letterhead, our name as supplier
    if has(r"\bPURCHASE ORDER\b", T) and not own_head and not mps_form and not tc_title and not has(r"CERTIFIC", T) \
            and has(r"\bMINING PRESSURE SYS|\bKUSASA\b") \
            and not has(r"\bTAX INVOICE\b|\bINVOICE\b|\bQUOTATION\b|\bPRO ?-?FORMA\b|ACKNOWLEDG|CONFIRMATION|\bSTATEMENT\b", T) \
            and has(r"\bDELIVER(Y)? (TO|ADDRESS)\b|\bORDER (NUMBER|NO|DATE)\b|\bP\.?O\.? ?(NUMBER|NO)\b|\bDOCUMENT NO\b"):
        votes.append(("customer_order", "likely", "a customer's purchase order to MPS"))
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
    # presort-7: workshop sheet forms the drawing check misses (handwritten ones, printed title blocks)
    wsw = [w for w, p in (("sheet x of y", r"\b(SHEET|SHT)\.? ?:? ?\d{1,2} ?(OF|-) ?\d{1,2}\b"), ("destination", r"\bDESTINATION\b"),
                          ("corrosion", r"\bCORROSION ?(INT|EXT)"), ("issued for construction", r"ISSUED FOR CONSTRUCTION|\bQ ?A APPROVED\b"),
                          ("MPS dwg", r"\bMPS DWG\b"),
                          # presort-9: the drawing title block (Dino's Workshop Sheets moves)
                          ("project", r"\bPRO[JY]ECT\s*[:~\"-]"), ("job", r"\bJOB\s*(NO\.?)?\s*[:)]"), ("dwg no", r"\bDWG\.? ?NO\b")) if has(p)]
    if len(wsw) >= 2 and not has(r"\bTAX INVOICE\b|\bQUOTATION\b|\bPRO ?-?FORMA\b|\bSTATEMENT\b|\bCERTIFICATE\b|\bREQUISITION\b"
                                 r"|\bINVOICE\b|\bPURCHASE ORDER\b|\bDELIVERY NOTE\b|\bJOB TRANSFER\b", T) and not mps_form \
            and not heat and not has(r"CERTIFICAT|\bMILL\b|CHEMICAL|\bTENSILE\b"):
        votes.append(("workshop_sheet", "likely", "workshop sheet form (" + ", ".join(wsw) + ")"))
    if has(r"\bOEM LETTER\b|\bDECLARATION BY PRODUCER\b") and has(r"\bRULES OF ORIGIN\b|\bHEREBY DECLARE\b|\bPRODUCER\b"):
        votes.append(("letter", "likely", "OEM letter (declaration by producer)"))
    med = [w for w, q in (("practice no", r"\bPRACTICE (NO|NUMBER|NR)\b|\bPR\.? ?NO\b"), ("medical aid", r"\bMEDICAL AID\b|\bMED\.? ?AID\b|\bDISCOVERY\b"),
                          ("patient", r"\bPATIENT\b"), ("ICD-10", r"\bICD ?-? ?10\b"), ("HPCSA", r"\bHPCSA\b"),
                          ("doctor", r"\bDR\.? [A-Z]|\bPHYSIOTHERAPIST\b|\bOPHTHALMOLOGIST\b|\bDENTIST\b|\bORTHODONTIST\b|\bDERMATOLOGIST\b|\bGYNAECOLOGIST\b|\bOPTOMETRIST\b")) if has(q)]
    if len(med) >= 2 and has(r"\bPRACTICE (NO|NUMBER|NR)\b|\bPR\.? ?NO\b|\bMEDICAL AID\b|\bMED\.? ?AID\b|\bPATIENT\b|\bHPCSA\b"):
        votes.append(("medical", "likely", "medical (" + ", ".join(med) + ")"))
    fname = str(name or pdf.name).upper()
    if (has(r"\bMPSHQ ?-? ?\d{4,6}\b") or re.search(r"QUOTE?S? APP?ROVAL|MPSHQ", fname)) and has(r"\bQUOT(E|ATION)\b") \
            and (own_head or has(r"\bKUSASA\b|\bMINING PRESSURE SYS", T)):
        votes.append(("sales_quotation", "likely", "MPS / Kusasa sales quote (MPSHQ)"))
    if has(r"\bPURCHASE REQUISITION\b") and has(r"\bBOARD MEMBER\b|\bNO ?1 ACCOUNT\b|\bSAHETI\b"):
        votes.append(("saheti", "likely", "Saheti purchase requisition"))
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

    # presort-6 (Dino's hold corrections, 3 Nov): personal and vehicle papers filed with the orders
    handover = has(r"\bVEHICLE HAND ?-?OVER\b")
    if handover:
        votes.append(("vehicle_handover", "likely", "vehicle handover letter"))
    rc1 = [w for w, p in (("certificate of registration", r"CERTIFICATE OF REGISTRATION|SERTIFIKAAT VAN REGISTRASIE"),
                          ("motor vehicle", r"\bMOTOR ?VEHICLE\b"), ("RC1", r"\bRC ?[1I]\b"),
                          ("VIN / voertuig", r"VEHICLE IDENTIFICATION|\bVOERTUIG"),
                          ("road traffic act", r"ROAD TRAFFIC ACT|PADVERKEERSWET"),
                          ("registering authority", r"REGISTERING AUTHORITY|REGISTRASIE-? ?OWERHEID")) if has(p)]
    if len(rc1) >= 2 and not handover:
        votes.append(("vehicle_registration", "likely", "vehicle registration (" + ", ".join(rc1) + ")"))
    idw = [w for w, p in (("ID no", r"\bI\.? ?D\.? ?NO\b|\bIDENTITY NUMBER\b"), ("SA citizen", r"\bS\.? ?A\.? ?(CITIZEN|BURGER)\b"),
                          ("national identity", r"\bNATIONAL IDENTITY\b|\bIDENTITEITSDOKUMENT\b"),
                          ("home affairs", r"\bHOME AFFAIRS\b|\bBINNELANDSE SAKE\b"), ("forenames", r"\bFORENAMES\b|\bVOORNAME\b"),
                          ("place of birth", r"\bCOUNTRY OF BIRTH\b|\bGEBOORTEDISTRIK\b"),
                          ("registered address", r"\bGEREGISTREERDE\b|\bWOON-? ?EN ?POSADRES\b|\bREGISTERED RESIDENTIAL\b")) if has(p)]
    birth = has(r"\bBIRTH CERTIFICATE\b|\bGEBOORTESERTIFIKAAT\b|\bABRIDGED\b", T) or "BIRTH CERT" in str(name or pdf.name).upper()
    if len(idw) >= 2 and not handover and not birth:          # birth certificates: not an ID (no category yet)
        votes.append(("id_document", "likely", "identity document (" + ", ".join(idw) + ")"))
    lic = has(r"\bDRIVING LICEN[CS]E\b|\bVEHICLE RESTRICTIONS?\b", T)     # not "licence no": a pro forma had one
    if not handover and (lic or re.search(r"\bDRIVERS?'?S? ?LICEN[CS]E\b", str(name or pdf.name).upper())):
        votes.append(("drivers_licence", "likely", "driving licence" + ("" if lic else " (file name)")))
    if has(r"\bCOLLECTION SLIP\b", T) or has(r"\bOPTIMUM RECYCLING\b") and has(r"\bSLIP\b"):
        votes.append(("collection_slip", "likely", "title: collection slip"))

    # 471b: a doctor's statement / invoice is medical, an OEM letter is a letter, our own quote is a sales quote,
    # whatever its title says
    # presort-8: a supplier's credit application we sign (title), the Ivor Crabbia Family Trust's papers
    if has(r"\bCREDIT APPLICATION\b|\bAPP?LICATION FOR (A |AN )?(CREDIT|ACCOUNT)\b|\bAPP?LICATION FOR DEFERRED PAYMENT\b"
           r"|\bCUSTOMER APPLICATION FOR (AN )?ACCOUNT\b|\bACCOUNT APPLICATION\b|\bCREDIT FACILIT(Y|IES) APPLICATION\b", T):
        votes.append(("credit_application", "likely", "title: credit / account application"))
    if (has(r"\bIVOR CRABBIA FAMILY TRUST\b", T) or has(r"\bIT ?2462 ?/ ?(20)?09\b")) and not has(r"\bIVOR AND ANDRIANE\b"):
        votes.append(("icft", "likely", "Ivor Crabbia Family Trust (IT 2462/2009)"))
    if any(k in ("medical", "letter", "sales_quotation", "credit_application", "icft") for k, _, _ in votes):
        votes = [x for x in votes if x[0] not in ("statement", "supplier_invoice", "quotation", "credit_note", "purchase_order", "polymer_order")]
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

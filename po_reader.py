#!/usr/bin/env python3
"""
Purchase-order reader for the MPS OCR pipeline: the "v2" reader trained on the PO training batch (po_ocr_v2.py in
the po_train folder), as a module the worker calls. Same prompt, same worked examples (few-shot), same image size and
the same post-processing (tidy) as the run that read the 2017-2026 archive, so results match what was checked.

  - Knows the four order forms (MPS ORDER A#####, POLYMER LINERS PL####, INTERNAL GROUP TRANSFER #####, RONIN DF####)
    and that the supplier is the name on the "M ____" line, never the letterhead.
  - Melyssa's handwriting notes, payment terms kept apart from notes, open-quantity lines, cancelled orders.
  - Worked examples: <fewshot_dir>/<name>.png + <name>.json, shown to the model before each page.
Generated from po_ocr_v2.py; edit the prompt there and regenerate, or here if the training folder is retired.
"""
import base64, glob, json, os, re, subprocess, tempfile, urllib.request

OWN = re.compile('MINING PRESSURE|^MPS\\b|POLYMER LINERS|^POLYMER$|KUSASA|RONIN FLANGES|ALOE ?CAP|JUMBO PRESS|BEARDSLEY', re.I)

PROMPT = 'You are reading a handwritten order form from the Mining Pressure Systems group, Boksburg, South Africa.\nThey are pre-printed forms filled in by hand in BLOCK CAPITALS (mostly by Melyssa, sometimes Ivor). The forms:\n\n1. MPS ORDER - logo "MINING PRESSURE SYSTEMS" top left. Red printed number after "ORDER A", e.g. "ORDER A 80532" -> "A80532".\n2. POLYMER LINING SYSTEMS ORDER - logo "POLYMER LINERS" top left (VAT 4240207755). Red number after "ORDER PL",\n   e.g. "ORDER PL 7512" -> "PL7512". A handwritten "PO 6573" near the top right is other_po_ref.\n3. INTERNAL GROUP TRANSFER - heading "INTERNAL GROUP TRANSFER", "I.G.T. NO. 16013" in red. "TRANSFERRED FROM" is the\n   supplier, "TO" the receiving company, "REASON/SENDER\'S JOB NO." holds the job number (e.g. 31367E).\n4. RONIN FLANGES & FITTINGS ORDER - group company (VAT 4570132524); order numbers are "DF" + 4 digits, e.g. "DF2914".\nAnything else (a supplier invoice, delivery note, quotation, proof of payment, drawing or a document attached to\nsupport an order) is not an order form: set form to "not_an_order" and doc_type to what it is.\n\nIn the box at the top left (after the printed "M"):\n  line 1 = the SUPPLIER (the company the order is placed with). NEVER the company printed in the letterhead\n           (Mining Pressure Systems, Polymer Liners, Ronin) - those are the buyer.\n  line 2 = left: the salesperson at the supplier it is for the attention of (e.g. "DAWIE", "TSUMBEDZO", "SALES") -> att_of\n           right: how it was sent ("EMAIL", "PHONE", "CONF" = confirmed by phone) -> order_method\nNext to the DATE there is often a short handwritten allocation: CONS, VEHICLES, KUSASA, POLYMER, IAC, RONIN, a site name,\nor a job number like 30660E / 9176E (#### or ##### followed by E or D) -> job numbers go in job_number.\nPayment terms such as "DISC: 2.5% 30 DAYS", "DISC 2.5/1.30 DAYS", "30 DAYS" go in payment_terms, NOT in notes.\nCancelled orders: one or two big diagonal lines (or a cross) drawn through the whole form, usually with "CANC", "CANCEL" or "CANCELLED" written across it or stamped on it -> cancelled = true. Still read everything underneath as normal.\nAmended orders: "*AMENDED*" (or "AMENDED") handwritten or stamped at the very top of the form, usually with prices, quantities\nor dates crossed out and rewritten (often in another colour) -> amended = true. Where a value is crossed out and rewritten, read the NEW value.\nwriter = the person who wrote/signed the order (the name in the SIGNATURE / TRANSFERRED BY box, e.g. "Melyssa", "Ivor").\n\nHandwriting: dates are day.month.year. Melyssa\'s digits are tricky:\n  her 4 looks like a capital "L" but the end of the bottom stroke FLICKS UPWARD (L.03.2025 = 4 March 2025,\n  L.09.2025 = 4 September 2025). A real letter L (e.g. in "EMAIL") has a flat bottom with no flick.\n  Her 6 has a closed loop at the bottom (looks like a "b"). Her 1 is only ever a single plain vertical stroke -\n  never read a 4 as 1, and do not drop 4s. Count digits carefully: job cards like 005564 have 6 digits.\n  Her V looks like a U: read the word that makes sense (\"LONG VALVE\", not \"LONG VALUE\").\nThe R in front of a price is the Rand sign, not a digit: \"R86-96 ea\" is 86.96 - never 286.96 or 886.96.\nLines below the items that start with a dash (- QUOTE:, - DISC:, - REG:, a name) are header details, not order lines:\nQUOTE -> quote_ref, DISC/days -> payment_terms, the rest (REG numbers, who it is for) -> notes, joined with \"; \".\nCheck dates are real dates and amounts multiply out (qty x unit price) when a total is given.\nAbbreviations: "ea" = each; "PKG" or "P/KG" = per kilogram; "PL" or "P/L" after a price = per litre;\n"PLEN" or "P/LEN" = per length; "LEN" = length; "PM" or "P/M" = per metre; "HDPE" = high density polyethylene.\nSome orders (often Polymer) are open quantity: a price and unit but no quantity because it is not known yet -\nthen qty = null and open_quantity = true for that line.\nLines like "QUOTE: QUO 0000599" are quote_ref; delivery instructions go in notes - they are not order lines.\n\nReturn ONLY this JSON object:\n{\n "doc_type": "purchase_order|internal_transfer|invoice|quotation|proof_of_payment|delivery_note|support_document|other",\n "form": "mps_order|polymer_order|internal_group_transfer|ronin_order|not_an_order",\n "order_number": string|null,\n "other_po_ref": string|null,\n "supplier": string|null,\n "att_of": string|null,\n "order_method": string|null,\n "transfer_from": string|null,\n "transfer_to": string|null,\n "date_as_written": string|null,\n "date_iso": "YYYY-MM-DD"|null,\n "allocation": string|null,\n "job_number": string|null,\n "lines": [{"qty": number|null, "open_quantity": true|false, "description": string, "unit_price": number|null,\n            "price_unit": "each|kg|litre|length|metre|lot|null"}],\n "quote_ref": string|null,\n "payment_terms": string|null,\n "notes": string|null,\n "delivery_address": string|null,\n "writer": string|null,\n "signed_by": string|null,\n "cancelled": true|false,\n "amended": true|false,\n "legibility": "good|fair|poor"\n}\nCopy numbers, codes and names exactly as written. Amounts are Rand, plain numbers ("R71-52" -> 71.52).\nUse null when you cannot read a value - do not guess.'


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = re.sub(r"[R\s]", "", str(v)).replace("-", ".").replace(",", ".")
    s = re.sub(r"\.(?=.*\.)", "", s)
    try:
        return float(s)
    except ValueError:
        return None


_VOCAB = None
_VOCAB_KEY = None
EXTRA_VOCAB = None      # set by the worker: <pipeline root>/training/suppliers_verified.json (names from checked orders)


def supplier_vocab():
    """Known supplier names: suppliers you confirmed on checked orders first (EXTRA_VOCAB, reloaded when it changes),
    then po_suppliers.json beside this module (the PO library build). Keys 'all' and 'pl' (Polymer)."""
    global _VOCAB, _VOCAB_KEY
    try:
        key = os.path.getmtime(EXTRA_VOCAB) if EXTRA_VOCAB else None
    except OSError:
        key = None
    if _VOCAB is None or key != _VOCAB_KEY:
        voc = {"all": [], "pl": []}
        for path in ([EXTRA_VOCAB] if key else []) + [os.path.join(os.path.dirname(os.path.abspath(__file__)), "po_suppliers.json")]:
            try:
                with open(path, encoding="utf-8") as fh:
                    v = json.load(fh)
            except (OSError, ValueError):
                continue
            for k in ("all", "pl"):
                for n in (v.get(k) or {}):
                    if n not in voc[k]:
                        voc[k].append(n)
        voc["all"] += [n for n in voc["pl"] if n not in voc["all"]]
        _VOCAB, _VOCAB_KEY = voc, key
    return _VOCAB


def folder_supplier(source_folder):
    """The order's original folder is its supplier (Dino 28 Oct 17:25): Melyssa\\Orders-MPS\\2024\\V\\VACUUM SERVICES."""
    parts = [x for x in re.split(r"[\\/]+", str(source_folder or "")) if x.strip()]
    if len(parts) >= 3 and not re.fullmatch(r"\d{4}|[A-Z0-9]|ORDERS?.*|MELYSSA|OTHER|EMAIL", parts[-1].strip(), re.I):
        return parts[-1].strip()
    return None


_RULES, _RULES_KEY = [], None


def supplier_rule(name):
    """supplier_rules.json beside this module: [{"match": regex, "name": supplier, "why": ...}] -> (name, why) or None."""
    global _RULES, _RULES_KEY
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supplier_rules.json")
    try:
        key = os.path.getmtime(path)
    except OSError:
        return None
    if key != _RULES_KEY:
        try:
            with open(path, encoding="utf-8") as fh:
                _RULES = [(re.compile(r["match"], re.I), r["name"], r.get("why", "")) for r in json.load(fh).get("rules", [])]
        except (OSError, ValueError, KeyError, re.error):
            _RULES = []
        _RULES_KEY = key
    for rx, n, why in _RULES:
        if rx.search(str(name or "")):
            return n, why
    return None


def snap_supplier(name, polymer=False):
    """A misread supplier ('G STONELOR') -> the known name it clearly is ('STONCOR'), or None. Clearly = similar enough and
    well ahead of the next candidate; Polymer orders try Polymer's own suppliers first."""
    import difflib
    key = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
    k = key(name)
    if len(k) < 3:
        return None
    v = supplier_vocab()
    for names in ((v["pl"], v["all"]) if polymer else (v["all"],)):
        if not names:
            continue
        if any(key(n) == k for n in names):
            return None                                   # already a known name
        sc = sorted(((difflib.SequenceMatcher(None, k, key(n)).ratio(), n) for n in names), reverse=True)[:2]
        if sc and sc[0][0] >= 0.72 and (len(sc) < 2 or sc[0][0] - sc[1][0] >= 0.04):
            return sc[0][1]
    return None


def polymer_po(text, handwritten=False):
    """'PO 6795' / 'PO6795' / 'PD7032' -> 'PO6795' (digits only compared; never the PL number).
    handwritten=True (a model reading): her 4 looks like an L, so 'PO 665L' is PO6654."""
    t = str(text or "").upper()
    m = re.search(r"(?<![A-Z])P\s*[O0D]\s*[-#:.]?\s*([0-9L]{3,6})(?![0-9L])" if handwritten else
                  r"(?<![A-Z])P\s*[O0D]\s*[-#:.]?\s*(\d{3,6})(?!\d)", t)
    if not m or not re.search(r"\d", m.group(1)):
        return None
    return f"PO{int(m.group(1).replace('L', '4'))}"


def _plausible_date(s):
    """A real date from 2000 to next year (the archive goes back to 2017 and earlier)."""
    import datetime as _dt
    m = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)", s)
    if not m:
        return False
    try:
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return 2000 <= d.year <= _dt.date.today().year + 1


_ORDER_TOKEN = re.compile(r"^(A ?\d{5}|PL ?S? ?\d{3,4}|PO ?\d{3,5}|DF ?\d{4}|\d{3,6}|[A-Z]?\d{8,})$")
_NOT_SUPPLIER = re.compile(r"QUOTE|APPROVAL|^POLYMER$|^MPS\b|AMENDED|CANCEL|^VEHICLES$|^CONS$|^DOC\d|^IMG|^SCAN|MAINT$|IGT")


def file_supplier(base):
    """Supplier typed into the scan's file name ("NUVO RUBBER-PLS2200-POLYMER-...", "PO7032-PL8239-STONCOR-POLYMER"):
    the name next to an order number. None when the name doesn't follow that pattern."""
    segs = [x.strip() for x in os.path.splitext(base)[0].upper().split("-")]
    for i, x in enumerate(segs):
        if _ORDER_TOKEN.match(x) or len(re.sub(r"[^A-Z]", "", x)) < 2:
            continue
        near = (i + 1 < len(segs) and _ORDER_TOKEN.match(segs[i + 1])) or (i > 0 and _ORDER_TOKEN.match(segs[i - 1]))
        return None if (_NOT_SUPPLIER.search(x) or not near) else x
    return None


# ---------------------------------------------------------------- job numbers
# The job-number rule (series letters, completion from the job list by date), the allocation rule and the vehicle
# registration / order-date checks live in mps_jobs.py beside this module, shared with the other readers, the
# dashboard and fix_jobs.py. JOBS_DIR is kept here because the worker, dashboard and scripts set po_reader.JOBS_DIR.
import mps_jobs

JOBS_DIR = None         # set by the worker / dashboard: the pipeline root (jobs_*.csv, sage_invoices.csv, orders_index.csv)
PRICE_MEMORY = None     # path override (eval); default <JOBS_DIR>/training/price_memory.json, built by training.py
try:
    import price_memory
except ImportError:     # the reader still works without it
    price_memory = None
_NAME_DATE = re.compile(r"(?<![0-9])(\d{1,2})-(\d{1,2})-((?:19|20)\d{2})(?![0-9])")


def name_date(base):
    """The date in an archive file name: scans filed by hand are named SUPPLIER-ORDER-DD-MM-YYYY(-CANCELLED).pdf.
    On the 683 checked orders this date was right on 681 (the other 2: January orders typed with last year, which the
    scan-date / neighbouring-order checks correct). Scanner names (DOC150124-..., MPS-05112025...) have no such date."""
    m = _NAME_DATE.search(base or "")
    if not m:
        return None
    iso = f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return iso if _plausible_date(iso) else None
E_SERIES_START = mps_jobs.E_SERIES_START
_JOB_ANY = mps_jobs.JOB_ANY
_JOB_LETTER = mps_jobs.JOB_LETTERED


def _sync_jobs_dir():
    if JOBS_DIR and mps_jobs.JOBS_DIR != JOBS_DIR:
        mps_jobs.JOBS_DIR = JOBS_DIR


def job_index():
    _sync_jobs_dir()
    return mps_jobs.job_index()


def _norm_job(digits, letter):
    return mps_jobs.norm_job(digits, letter)


def _job_desc(r):
    return mps_jobs.job_desc(r)


def complete_job(digits, letter, date_iso, chk, written=None):
    """See mps_jobs.complete_job. Kept here for the dashboard, ws_reader and fix_jobs.py."""
    _sync_jobs_dir()
    return mps_jobs.complete_job(digits, letter, date_iso, chk, written)


def find_job(d, base, chk):
    """Job number(s) for a read order: the scan's file name first (typed by whoever scanned it), then the job_number
    the model read (all numbers in it, letter optional), the allocation (letter optional) and the notes (letter
    required). Several jobs ("5919-5911D" in the file name, "5919 5911" written) are kept, joined with ", ".
    Sets job_number, job_number_read, job_customer, job_name."""
    taken = mps_jobs.own_numbers(d)

    def found(text, rx):
        out = []
        for m in rx.finditer(re.sub(r"(?<![A-Z])J(?:OB)?\s*(?:NO\.?)?\s*[:#]?\s*(?=\d)", " ", str(text or "").upper())):   # "J30060E"
            dg = m.group(1)
            if dg in taken or dg.lstrip("0") in taken or (m.group(2) is None and 1990 <= int(dg) <= 2099):
                continue                    # the order / PO number, or a year
            out.append((dg, m.group(2), m.group(0).strip()))
        return out

    # jobs typed in the file name: "...-A53308-6939D-12012017104450.pdf"; "...-5919-5911D-..." = 5919D and 5911D
    named, pending, prev = [], [], ""
    for seg in re.split(r"[-_\s]+", re.sub(r"\d{8,}", " ", os.path.splitext(base)[0].upper())):
        m = re.fullmatch(r"0*(\d{4,5})([A-E])?", seg)
        after_igt, prev = prev in ("IGT", "IGTNO"), seg            # "KUSASA-IGT-11414-5812D": 11414 is the IGT number
        if not m or after_igt or m.group(1) in taken or (not m.group(2) and 1990 <= int(m.group(1)) <= 2099):
            pending = []
            continue
        if m.group(2):      # a bare number just before a lettered one shares its letter when it has as many digits
            named += [(x, m.group(2)) for x in pending if len(x) == len(m.group(1))] + [(m.group(1), m.group(2))]
            pending = []
        else:
            pending.append(m.group(1))
    read = found(d.get("job_number"), _JOB_ANY) or found(d.get("allocation"), _JOB_ANY)[:1] or found(d.get("notes"), _JOB_LETTER)[:1]
    if read:
        d["job_number_read"] = " ".join(r[2] for r in read)
    jobs, recs = [], []
    if named:
        for dg, lt in named:
            job, rec = complete_job(dg, lt, d.get("date_iso"), chk)
            if job and job not in jobs:
                jobs.append(job); recs.append(rec)
        nd = {int(x) for x, _ in named}
        if read and not all(int(r[0]) in nd for r in read):
            chk.append(f"job: file name says {', '.join(jobs)}, form reads {d['job_number_read']}")
        elif read and any(r[1] and (int(r[0]), r[1]) not in {(int(x), l) for x, l in named} for r in read):
            chk.append(f"job: file name says {', '.join(jobs)}, form reads {d['job_number_read']}")
    else:
        for dg, lt, txt in read:
            job, rec = complete_job(dg, lt, d.get("date_iso"), chk, written=txt)
            if job and job not in jobs:
                jobs.append(job); recs.append(rec)
    rec = next((r for r in recs if r), None)
    d["job_number"] = ", ".join(jobs) or None
    d["job_customer"] = rec.get("customer") if rec else None
    d["job_name"] = rec.get("name") if rec else None
    return d["job_number"]


# order numbers used as examples in PROMPT: a document whose number reads as one of these (and whose file name doesn't
# carry it) almost always had no order number at all - the model filled the gap with the example
EXAMPLE_NUMBERS = {"A80532", "PL7512", "DF2914", "A84403", "16013"}     # prompt + worked examples


def tidy(d, rel, source_folder=None, source_pdf=None, source_modified=None, source_sha256=None):
    chk = []
    base = re.sub(r"^[A-Z]+__", "", os.path.basename(rel).upper())    # the worker's copy is "<Type>__<file name>"
    on = re.sub(r"[^A-Z0-9]", "", str(d.get("order_number") or "").upper())
    dig = re.sub(r"\D", "", on)
    form = d.get("form") or ""
    # an MPS order made OUT TO Polymer: "POLYMER" in the supplier box makes the model call it a Polymer form and the
    # number a PL number, but the scan is named after its red A-number (POLYMER-A77128-10541E-HARMONY.pdf). A Polymer
    # order's scan never carries an A-number, so: A-number in the file name with the same last digits, no PL number
    m_fa = re.search(r"(?<![A-Z0-9])A(\d{5})(?!\d)", base.replace(" ", ""))
    if m_fa and (form == "polymer_order" or on.startswith("PL")) and not re.search(r"(?<![A-Z0-9])PLS?\d{4}(?!\d)", base.replace(" ", "")) \
            and (not dig or m_fa.group(1).endswith(dig[-4:])):
        chk.append(f"MPS order A{m_fa.group(1)} made out to Polymer (read as a Polymer form{', ' + on if on else ''}): taken as an MPS order")
        form = d["form"] = "mps_order"
        d["doc_type"] = "purchase_order"
        on, dig = "A" + m_fa.group(1), m_fa.group(1)
        if not str(d.get("supplier") or "").strip():
            d["supplier"] = "POLYMER"
    if m_fa and (form == "ronin_order" or on.startswith("DF")) and not re.search(r"(?<![A-Z0-9])DF\d{4}(?!\d)", base.replace(" ", "")) \
            and (not dig or m_fa.group(1).endswith(dig[-4:])):
        chk.append(f"MPS order A{m_fa.group(1)} made out to Ronin (read as a Ronin form{', ' + on if on else ''}): taken as an MPS order")
        form = d["form"] = "mps_order"
        d["doc_type"] = "purchase_order"
        on, dig = "A" + m_fa.group(1), m_fa.group(1)
        if not str(d.get("supplier") or "").strip():
            d["supplier"] = "RONIN FLANGES & FITTINGS"
    if on.startswith("DF") or form == "ronin_order":
        on2 = "DF" + dig[-4:] if dig else None
    elif on.startswith("PL") or form == "polymer_order" or (len(dig) == 4 and form != "internal_group_transfer"):
        on2 = "PL" + dig[-4:] if dig else None
    elif form == "internal_group_transfer" or "KUSASA IGT" in base:
        on2 = dig[-5:] if dig else None
    elif dig:
        on2 = "A" + dig[-5:]
    else:
        on2 = None
    if on2 and on2 in EXAMPLE_NUMBERS and re.sub(r"\D", "", on2) not in re.sub(r"\D", "", base):
        # the model repeated the example from its instructions (seen on letters, reports, attachments): not read
        chk.append(f"order number read as {on2}, the example number in the reader's instructions: ignored")
        on2 = None
    if on2 != (d.get("order_number") or None):
        d["order_number_read"] = d.get("order_number")
    d["order_number"] = on2
    m = re.search(r"(?<![A-Z0-9])(A\d{5}|PLS?\d{4}|DF\d{4})(?!\d)", base.replace(" ", ""))
    on_file = m.group(1).replace("PLS", "PL") if m else None       # 2017 file names write PL orders as PLS2252
    if on_file and on2 and on_file != on2:
        chk.append(f"file name says {on_file}, form reads {on2}")
    elif on_file and not on2:
        chk.append(f"order number not read: {on_file} taken from the file name")
        d["order_number"] = on2 = on_file
    # supplier must not be our own company
    sup = (d.get("supplier") or "").strip()
    if form == "internal_group_transfer" and d.get("transfer_from"):
        sup = d["transfer_from"]
    # a Ronin order (DF####): Ronin is the buyer printed in the letterhead, never the supplier (Dino 29 Oct)
    if (form == "ronin_order" or str(on2 or "").startswith("DF")) and re.match(r"^\W*(RFF|RONIN)\b", sup, re.I):
        chk.append(f"supplier read as {sup}: Ronin is the buyer on a Ronin order, not the supplier")
        sup = ""
    if OWN.search(sup) and not (form == "internal_group_transfer" and "KUSASA" in sup.upper()) \
            and not (form == "mps_order" and re.match(r"POLYMER\b", sup.upper()) and "LINERS" not in sup.upper()) \
            and not (form == "mps_order" and re.match(r"^\W*(RONIN|RFF)\b", sup.upper())):
        chk.append(f"supplier read as own company ({sup})")
        sup = ""
    d["supplier"] = sup or None
    fsup = file_supplier(base) if form != "internal_group_transfer" else None
    if fsup and (form == "ronin_order" or str(on2 or "").startswith("DF")) and re.match(r"^\W*(RFF|RONIN)\b", fsup, re.I):
        fsup = None                                      # a file named after the buyer (RONIN-DF2914-...): not the supplier
    if fsup:
        k1, k2 = re.sub(r"[^A-Z0-9]", "", fsup), re.sub(r"[^A-Z0-9]", "", str(d["supplier"] or "").upper())
        if not k2:
            d["supplier"] = fsup
            chk.append(f"supplier not read: {fsup} taken from the file name")
        elif not (k1 in k2 or k2 in k1):
            chk.append(f"file name says supplier {fsup}")
        d["supplier_file"] = fsup
    dsup = folder_supplier(source_folder) if form != "internal_group_transfer" else None
    if dsup and not d["supplier"]:
        d["supplier"] = dsup
        chk.append(f"supplier not read: {dsup} taken from the order's folder")
    if dsup:
        d["supplier_folder"] = dsup
    rule = supplier_rule(d["supplier"]) if d["supplier"] else None
    if rule:
        if d["supplier"] != rule[0]:
            d["supplier_read"], d["supplier"] = d["supplier"], rule[0]
        d["supplier_rule"] = rule[1]
        kr = re.sub(r"[^A-Z0-9]", "", rule[0].upper())
        if fsup and (re.sub(r"[^A-Z0-9]", "", fsup) in kr or kr in re.sub(r"[^A-Z0-9]", "", fsup)):
            chk[:] = [c for c in chk if c != f"file name says supplier {fsup}"]
    if d["supplier"] and not rule:
        known = snap_supplier(d["supplier"], form == "polymer_order" or str(d.get("order_number") or "").upper().startswith("PL"))
        if known:
            chk.append(f"supplier read as {d['supplier']}, matched to {known}")
            d["supplier_read"], d["supplier"] = d["supplier"], known
    # date
    di = d.get("date_iso")
    if not di and d.get("date_as_written"):                       # "16.01.2017": the model wrote it but didn't convert it
        mw = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})\s*", str(d["date_as_written"]))
        if mw:
            y = int(mw.group(3)); y = y + 2000 if y < 100 else y
            di = d["date_iso"] = f"{y:04d}-{int(mw.group(2)):02d}-{int(mw.group(1)):02d}"
    if di and not _plausible_date(str(di)):
        chk.append(f"date read as {di}")
        d["date_iso"] = None
    # archive scans carry the date in the file name: that beats reading the handwriting (Melyssa's 4 reads as 1/6/L).
    # The scan-date and neighbouring-order checks further down still run on it.
    nd = name_date(base)
    if nd and nd != d.get("date_iso"):
        chk.append(f"date read as {d.get('date_iso') or d.get('date_as_written') or 'nothing'}: {nd} taken from the file name")
        d["date_read"], d["date_iso"], d["date_from_name"] = d.get("date_iso"), nd, True
    for l in d.get("lines") or []:
        l["qty"] = num(l.get("qty")); l["unit_price"] = num(l.get("unit_price"))
    # unit prices against what this supplier was paid for the same item on checked orders (price_memory.py)
    if price_memory and d.get("lines") and d.get("supplier"):
        mem = price_memory.load(PRICE_MEMORY or (os.path.join(JOBS_DIR, "training", "price_memory.json") if JOBS_DIR else None))
        for i, old_p, new_p, desc in price_memory.fix_lines(d["lines"], d["supplier"], mem):
            chk.append(f"line {i}: price read as {old_p:g}, corrected to {new_p:g} (paid for {str(desc)[:40]} on an earlier "
                       f"checked order) - check")
            d.setdefault("prices_corrected", []).append({"line": i, "read": old_p, "corrected": new_p})
    d.setdefault("attention", d.get("att_of"))          # old key kept for the PO library build
    if not d.get("signed_by") and d.get("writer"):
        d["signed_by"] = d["writer"]
    if "CANCEL" in base:
        if not d.get("cancelled"):
            chk.append("file name says CANCELLED")
        d["cancelled"] = True
    if "AMEND" in base:
        if not d.get("amended"):
            chk.append("file name says AMENDED")
        d["amended"] = True
    # Polymer (PL) orders: the Polymer Sage PO number written at the top ("PO 6795"; her O can read as D)
    if (d.get("order_number") or "").startswith("PL") or form == "polymer_order":
        po = polymer_po(d.get("other_po_ref"), handwritten=True)
        po_file = polymer_po(re.sub(r"PL\s*\d+", " ", base))
        if po and po_file and po != po_file:
            chk.append(f"PO number: file name says {po_file}, form reads {po}")
        d["polymer_po"] = po_file or po          # the file name is typed by whoever scanned it: trusted over the reading
        if not d["polymer_po"]:
            chk.append("no Polymer PO number read")
    if d.get("_cut_off"):
        chk.append(f"the reader's answer was cut off at '{d['_cut_off']}' (runaway text): that field and the ones after it are missing")
    # the order's date against the orders numbered around it (orders_index.csv): a misread digit is corrected and flagged
    _sync_jobs_dir()
    if d.get("date_iso") and d.get("order_number"):
        fixed, changed = mps_jobs.check_order_date(d["order_number"], d["date_iso"], chk,
                                                   scan=mps_jobs.scan_date(source_folder, os.path.basename(rel), source_pdf,
                                                                           modified=source_modified, sha256=source_sha256))
        if changed:
            d["date_read"], d["date_iso"], d["date_corrected"] = d["date_iso"], fixed, True
    # the slash rule: a job in the allocation box -> job_number, terms -> payment_terms, a method -> order_method,
    # allocation words normalised, VEHICLES -> registration into other_po_ref, IAC -> ordered by Ivor
    mps_jobs.apply_allocation(d, chk)
    # the model sometimes copies the order date into the job box ("2024-01-09"): not a job
    jb = str(d.get("job_number") or "").strip()
    if jb and (re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", jb)
               or not any(not (m.group(2) is None and 1990 <= int(m.group(1)) <= 2099)
                          for m in _JOB_ANY.finditer(re.sub(r"(?<![A-Z])J(?:OB)?\s*(?:NO\.?)?\s*[:#]?\s*(?=\d)", " ", jb.upper()))
                          if len(m.group(1)) >= 4)):
        chk.append(f"job box read as {jb}: not a job number, ignored")
        d["job_number_read"], d["job_number"] = jb, None
    # a lettered job written in the "other reference" box of an MPS order (never on Polymer orders: that box is the PO number)
    if not d.get("job_number") and form not in ("polymer_order",) and not (d.get("order_number") or "").startswith("PL"):
        m_ref = re.fullmatch(r"\s*(?:JOB\s*)?0*(\d{4,5})\s?([A-E])\s*", str(d.get("other_po_ref") or "").upper())
        if m_ref:
            d["job_number"], d["other_po_ref"] = m_ref.group(1) + m_ref.group(2), None
            chk.append(f"job {d['job_number']} taken from the other-reference box")
    elif d.get("job_number") and form != "polymer_order" and \
            re.sub(r"[^0-9A-E]", "", str(d.get("other_po_ref") or "").upper().lstrip("JOB ")) == re.sub(r"[^0-9A-E]", "", str(d["job_number"]).upper()):
        d["other_po_ref"] = None             # the same job written in both boxes: keep it once
    # job number (after the date and the Polymer PO: both are used)
    find_job(d, base, chk)
    d["checks"] = chk
    return d


_FEW = {}


def fewshot_msgs(fewshot_dir):
    """Worked examples (image + verified answer), cached per folder."""
    if fewshot_dir in _FEW:
        return _FEW[fewshot_dir]
    msgs = []
    if fewshot_dir and os.path.isdir(fewshot_dir):
        for j in sorted(glob.glob(os.path.join(fewshot_dir, "*.json"))):
            png = j[:-5] + ".png"
            if os.path.exists(png):
                msgs.append({"role": "user", "content": PROMPT, "images": [base64.b64encode(open(png, "rb").read()).decode()]})
                msgs.append({"role": "assistant", "content": json.dumps(json.load(open(j, encoding="utf-8")), ensure_ascii=False)})
    _FEW[fewshot_dir] = msgs
    return msgs


def read(path, ollama_url, model="qwen2.5vl:7b", fewshot_dir=None, pages=2, px=1600, timeout=1200, keep_alive="30m",
         first_page=1, source_folder=None, source_pdf=None, source_modified=None, source_sha256=None):
    """Returns (data, pages_read, raw): data is the tidied answer, as po_ocr_v2.py writes it under "data".
    first_page: start here (e.g. 2 when page 1 of the file is a proof of payment)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = str(path)
        if p.lower().endswith(".pdf"):
            subprocess.run(["pdftoppm", "-png", "-scale-to", str(px), "-f", str(first_page), "-l", str(first_page + pages - 1),
                            p, os.path.join(tmp, "p")],
                           check=True, capture_output=True, timeout=300)
            pngs = sorted(glob.glob(os.path.join(tmp, "p*.png")))
        else:
            pngs = [p]
        if not pngs:
            raise RuntimeError("no pages could be rendered")
        body = {"model": model, "stream": False, "format": "json", "keep_alive": keep_alive,
                "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
                "messages": fewshot_msgs(fewshot_dir) + [{"role": "user", "content": PROMPT,
                            "images": [base64.b64encode(open(x, "rb").read()).decode() for x in pngs]}]}
        req = urllib.request.Request(ollama_url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
            raw = resp["message"]["content"]
    global LAST_TIMING                               # review F22 (28 Oct): is the worked-example prefix cached between reads?
    LAST_TIMING = {"prompt_tokens": resp.get("prompt_eval_count"), "prompt_eval_ms": round((resp.get("prompt_eval_duration") or 0) / 1e6),
                   "eval_tokens": resp.get("eval_count"), "eval_ms": round((resp.get("eval_duration") or 0) / 1e6),
                   "load_ms": round((resp.get("load_duration") or 0) / 1e6), "total_ms": round((resp.get("total_duration") or 0) / 1e6)}
    data = parse_answer(raw)
    return tidy(data, os.path.basename(p), source_folder, source_pdf or p, source_modified, source_sha256), len(pngs), raw


LAST_TIMING = None       # timings of the last read() (worker logs them; review F22)
NUM_PREDICT = 4096      # an order's answer is ~300-1,500 tokens; caps a runaway answer (one field repeating) at ~1 minute
NUM_CTX = 49152         # prompt + worked examples (~3,900 tokens each at 1600 px) + the order's pages + the answer must fit:
                        # 7 examples + 2 pages is ~33,000 tokens, over the old 32,768 (answers came back cut off, 23 Oct).
                        # ~2.8 GB of KV cache on the GPU for qwen2.5vl:7b; jt_reader / ws_reader use the same value so
                        # Ollama doesn't reload the model between document types


def parse_answer(raw):
    """The model's JSON. An answer cut off part-way (a runaway field, or the token cap) keeps every complete field
    before the cut and is flagged, instead of failing the file."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    lines = raw.strip().splitlines()
    for i in range(len(lines) - 1, 0, -1):           # drop lines from the end until what's left closes as an object
        head = "\n".join(lines[:i]).rstrip().rstrip(",")
        for tail in ("\n}", "\n]}", "\n}]}"):
            try:
                data = json.loads(head + tail)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data:
                cut = re.match(r'\s*"([a-z_]+)"', lines[i] if i < len(lines) else "")
                data["_cut_off"] = cut.group(1) if cut else "end"
                return data
    raise RuntimeError(f"model returned non-JSON: {raw[:200]!r}")


FORM_LABEL = {"mps_order": "MPS order", "polymer_order": "Polymer order", "internal_group_transfer": "Internal group transfer",
              "ronin_order": "Ronin order", "not_an_order": "Not an order"}


LIB_DOCTYPE = {"purchase_order": "Purchase Order", "internal_transfer": "IGT", "proof_of_payment": "Proof of Payment"}


def company_of(d):
    """Which group company placed the order - the PO library's Company column (MPS / Kusasa / Polymer, fill-in)."""
    form = d.get("form") or ""
    alloc = " ".join(str(d.get(k) or "") for k in ("allocation", "notes")).upper()
    if form == "polymer_order":
        return "Polymer"
    if form == "ronin_order":
        return "Ronin"
    if form == "internal_group_transfer":
        to = str(d.get("transfer_to") or "").upper()
        return "Kusasa" if "KUSASA" in to else "Polymer" if "POLYMER" in to else "MPS"
    if "KUSASA" in alloc:
        return "Kusasa"
    if "POLYMER" in alloc:
        return "Polymer"
    return "MPS"


def library_name(f, source_name, ext=".pdf"):
    """Name used in the PO library: SUPPLIER-ORDERNUMBER-dd-MM-yyyy (parts left out when unread), as the library build did."""
    sup = (f.get("supplier") or "UNKNOWN SUPPLIER").upper()
    num_ = f.get("order_number") or os.path.splitext(os.path.basename(source_name))[0]
    parts = [sup, num_]
    if f.get("date"):
        y, m, dd = str(f["date"]).split("-")
        parts.append(f"{dd}-{m}-{y}")
    if f.get("amended"):
        parts.append("AMENDED")
    if f.get("cancelled"):
        parts.append("CANCELLED")
    if f.get("library_doc_type") == "Proof of Payment":      # a payment confirmation filed with the orders
        parts.append("POP")
    name = "-".join(parts)
    name = re.sub(r'[\\/:*?"<>|#%\t\r\n]+', " ", name)
    return re.sub(r"\s+", " ", name).strip(" .")[:180] + ext


def to_fields(d):
    """Pipeline fields + flags from the reader's answer. Keeps the reader's own names (order_number, supplier, att_of,
    payment_terms ...) and adds the names MPSDocs' ingest already uses (po_number, supplier_name, ordered_by,
    delivery_to, items[].quantity/amount, total), so SQL keeps working unchanged."""
    lines = []
    for l in d.get("lines") or []:
        if not isinstance(l, dict) or not str(l.get("description") or "").strip():
            continue
        q, p = num(l.get("qty")), num(l.get("unit_price"))
        lines.append({"quantity": q, "description": str(l.get("description")).strip(), "unit_price": p,
                      "price_unit": l.get("price_unit"), "open_quantity": bool(l.get("open_quantity")),
                      "amount": round(q * p, 2) if q is not None and p is not None else None})
    amounts = [l["amount"] for l in lines if l["amount"] is not None]
    priced = [l for l in lines if l["unit_price"] is not None]
    total = round(sum(amounts), 2) if priced and len(amounts) == len(priced) else None
    def lt(l):
        q = "" if l["quantity"] is None else (f"{l['quantity']:g} x " if isinstance(l["quantity"], (int, float)) else f"{l['quantity']} x ")
        pr = "" if l["unit_price"] is None else f" @ R{l['unit_price']:,.2f}" + (f" per {l['price_unit']}" if l.get("price_unit") not in (None, "each", "null") else "")
        return f"{q}{l['description']}{pr}"
    f = {
        "order_number": d.get("order_number"), "supplier": d.get("supplier"), "order_method": d.get("order_method"),
        "att_of": d.get("att_of"), "date": d.get("date_iso"), "date_as_written": d.get("date_as_written"),
        "allocation": d.get("allocation"), "job_number": d.get("job_number"), "other_po_ref": d.get("other_po_ref"),
        "job_number_read": d.get("job_number_read"), "job_customer": d.get("job_customer"), "job_name": d.get("job_name"),
        "quote_ref": d.get("quote_ref"), "notes": d.get("notes"), "payment_terms": d.get("payment_terms"),
        "signed_by": d.get("signed_by"), "writer": d.get("writer"), "form": d.get("form"),
        "form_label": FORM_LABEL.get(d.get("form") or "", d.get("form")), "po_doc_type": d.get("doc_type"),
        "transfer_from": d.get("transfer_from"), "transfer_to": d.get("transfer_to"),
        "delivery_address": d.get("delivery_address"), "cancelled": bool(d.get("cancelled")),
        "amended": bool(d.get("amended")), "legibility": d.get("legibility"),
        "items": lines, "line_count": len(lines), "lines_text": "\n".join(lt(l) for l in lines) or None,
        "order_value": total, "checks": d.get("checks") or [],
        "value_note": f"{len(priced)} of {len(lines)} lines priced" if lines else None,
        "polymer_po": d.get("polymer_po"),
        "allocation_known": d.get("allocation_known"), "allocation_changes": d.get("allocation_changes"),
        "date_read": d.get("date_read"), "date_corrected": bool(d.get("date_corrected")),
        "pl_order": d.get("order_number") if (d.get("order_number") or "").startswith("PL") else None,
        "company": company_of(d), "library_doc_type": LIB_DOCTYPE.get(d.get("doc_type") or "", "Other"),
        "review": "; ".join(d.get("checks") or []) or None,
        # names MPSDocs.dbo.usp_IngestDocument reads for a 'po'
        "po_number": d.get("order_number"), "supplier_name": d.get("supplier"), "ordered_by": d.get("ordered_by") or d.get("signed_by"),
        "delivery_to": d.get("delivery_address"), "total": total,
    }
    flags = []
    if d.get("form") == "not_an_order" or (d.get("doc_type") and d.get("doc_type") not in ("purchase_order", "internal_transfer")):
        flags.append("not_an_order")
    for k in ("order_number", "supplier", "date"):
        if not f.get(k):
            flags.append(f"missing_{k}")
    if f["cancelled"]:
        flags.append("cancelled")
    if d.get("legibility") == "poor":
        flags.append("poor_legibility")
    for c in f["checks"]:
        flags.append("check: " + c)
    return f, flags

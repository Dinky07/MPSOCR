#!/usr/bin/env python3
"""Sorts the Delivery Notes hold queue (Dino 27 Oct). Documents held as "not a delivery note" (no MPS barcode, not our
green stationery) are one of:
  1. a SUPPLIER's delivery note to MPS: MPS's name / VAT number on it as the customer, made out by a company on our
     supplier list -> out/SupplierDeliveryNotes/ (the PC sync files it in Supplier Documents / Supplier Delivery Notes,
     with Purchase Order and Supplier Invoice links); file name "<SUPPLIER>-<original name>"
  2. a CUSTOMER's delivery note for a delivery MPS made on their behalf: MPS is not on it, but its quantities and
     descriptions match one of our delivery notes (sage_invoices.csv lines) dated within +-3 days, made out to that
     customer -> out/DeliveryNotes/ as "<FROM COMPANY>-<original name>", on_behalf_of_customer = true, linked to our
     IN number and job (not given our IN number as its own Document No, so it never replaces our delivery note)
  anything else stays on hold, with what was found (fields.dn_sort) and a reason.
The vision model reads each document once (issuer, customer, date, numbers, lines); the text layer / Tesseract and the
Sage supplier list decide who issued it. DRY RUN unless --apply; reports/dn hold sort <date>.csv.

    python3 dn_hold_sort.py [--apply --from-report "reports/dn hold sort <stamp> dry run.csv"] [--limit N]
--from-report: only documents whose sorting (kind, supplier, our delivery note) is the same as in the dry-run report Dino
checked are moved; any that come out differently stay on hold and are listed.
"""
import argparse, base64, csv, difflib, glob, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import pop_reader                                   # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
CFG = json.load(open("/srv/ocr/pipeline/config.json"))
OLLAMA, MODEL, NUM_CTX = CFG["ollama_url"].rstrip("/"), "qwen2.5vl:7b", 49152
OWN_VAT = "4320123039"
STOP = {"PTY", "LTD", "LIMITED", "CC", "THE", "AND", "OF", "SA", "SOUTH", "AFRICA", "(PTY)", "T/A", "TA", "INC", "CO", "GROUP", "HOLDINGS", "BK", "&"}
GENERIC = {"SYSTEMS", "SYSTEM", "PIPING", "PIPE", "PIPES", "INDUSTRIAL", "INDUSTRIES", "SUPPLIES", "SUPPLY", "SUPPLIERS", "ENGINEERING",
           "MINING", "SALES", "TRADING", "SERVICES", "SERVICE", "STEEL", "INTERNATIONAL", "PRODUCTS", "EQUIPMENT", "SOLUTIONS",
           "AFRICAN", "LOGISTICS", "ENTERPRISES", "ENTERPRISE", "PROJECTS", "TECHNOLOGIES", "CONSTRUCTION", "INLAND", "GAUTENG",
           "JOHANNESBURG", "CONSULTING", "MANUFACTURING", "INVESTMENTS", "RSA", "ZA", "PRESSURE", "GENERAL", "TRADERS", "TRANSPORT"}
PROMPT = ("Read this delivery note (or waybill). Answer only JSON: {\"from_company\": the company that issued it (letterhead), "
          "\"from_vat\": its VAT number or null, \"to_company\": the customer it is made out to, \"deliver_to\": delivery address "
          "or site, \"date_iso\": \"YYYY-MM-DD\" or null, \"dn_number\": its own delivery note number, \"order_number\": the "
          "customer's order number on it (e.g. A12345) or null, \"lines\": [{\"qty\": number, \"description\": text}]}")

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--days", type=int, default=3)
ap.add_argument("--from-report", default="")
a = ap.parse_args()


def norm_words(s):
    return [w for w in re.sub(r"[^A-Z0-9 ]+", " ", str(s or "").upper()).split() if w not in STOP and len(w) > 1]


def name_sim(x, y):
    """Same company? Distinctive words only (NRB, WALTCOOL, CLOETE ...): generic ones (SYSTEMS, PIPING, MINING ...) never
    make two names alike. Shared distinctive words, or near-identical spelling of them (OCR)."""
    a = [w for w in norm_words(x) if w not in GENERIC]
    b = [w for w in norm_words(y) if w not in GENERIC]
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    j = len(sa & sb) / len(sa | sb)
    small, big = (a, b) if len(sa) <= len(sb) else (b, a)
    if (sa <= sb or sb <= sa) and (len(set(small)) >= 2 or small[0] == big[0]):   # NRB / NRB INLAND; not EXPRESS / BERCO EXPRESS
        j = max(j, 0.9 if max(len(w) for w in (sa & sb or {""})) >= 3 else 0.0)
    return max(j, difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() if min(len(" ".join(a)), len(" ".join(b))) >= 5 else 0.0)


def desc_sim(x, y):
    a, b = set(norm_words(x)), set(norm_words(y))
    return len(a & b) / max(1, len(a | b))


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_date(s):
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"):
        try:
            return datetime.strptime(str(s).strip(), f).date()
        except (TypeError, ValueError):
            pass
    return None


def text_of(pdf):
    try:
        t = subprocess.run(["pdftotext", "-layout", "-l", "2", str(pdf), "-"], capture_output=True, timeout=60).stdout.decode("utf8", "ignore")
        if len(re.sub(r"\s", "", t)) > 80:
            return t
    except Exception:
        pass
    try:
        return "\n".join(pop_reader.ocr_pages(pdf, 1, dpi=200, upright=True))
    except Exception:
        return ""


def model_read(pdf):
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-png", "-scale-to", "1600", "-f", "1", "-l", "1", "-singlefile", str(pdf), f"{td}/p"],
                       capture_output=True, timeout=120, check=True)
        body = {"model": MODEL, "stream": False, "format": "json", "keep_alive": CFG.get("keep_alive", "15m"),
                "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": 1500},
                "messages": [{"role": "user", "content": PROMPT, "images": [base64.b64encode(open(f"{td}/p.png", "rb").read()).decode()]}]}
        req = urllib.request.Request(OLLAMA + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
        raw = json.loads(urllib.request.urlopen(req, timeout=600).read())["message"]["content"]
    try:
        return json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {}


def safe(s, n=60):
    return re.sub(r'[\\/:*?"<>|#%]+', " ", str(s or "")).strip()[:n].strip() or "UNKNOWN"


# Sage: suppliers (VAT -> name) and our delivery notes (MPS company) with their lines
sup_by_vat, sup_names = {}, []
with open(ROOT / "suppliers.csv", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        if r.get("VatNo"):
            sup_by_vat[str(r["VatNo"]).strip()] = r
        if r.get("Name"):
            sup_names.append(r)
try:
    seen = {norm for norm in (" ".join(norm_words(r["Name"])) for r in sup_names)}
    with open(ROOT / "sage_ap.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            nm = r.get("VendorName") or r.get("SupplierName") or r.get("Name")
            k = " ".join(norm_words(nm))
            if nm and k and k not in seen and str(r.get("Company") or "MPS").upper() in ("MPS", "1", "MINING PRESSURE SYSTEMS"):
                seen.add(k)
                sup_names.append({"Name": nm, "VatNo": "", "SageAccount": r.get("VendorAccount") or r.get("Account") or ""})
except Exception as e:
    print("sage_ap.csv not read:", e)
ours = []
with open(ROOT / "sage_invoices.csv", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        if str(r.get("CompanyID")) != "1" or not str(r.get("DocNumber") or "").upper().startswith("IN"):
            continue
        d = parse_date(r.get("DocDate"))
        if not d:
            continue
        lines = []
        for part in str(r.get("Lines") or "").split(" | "):
            m = re.match(r"\s*([\d.,]+)\s*x\s*(.+)", part)
            if m:
                lines.append((num(m.group(1)), m.group(2)))
        ours.append({"no": r["DocNumber"], "date": d, "customer": r.get("CustomerName"), "account": r.get("CustomerAccount"),
                     "job": r.get("JobNumber"), "order": r.get("CustomerOrder"), "lines": lines})
by_no = {o["no"].upper(): o for o in ours}
cust_names = sorted({o["customer"] for o in ours if o["customer"]})
by_day = {}
for o in ours:
    by_day.setdefault(o["date"], []).append(o)
print(f"{len(sup_by_vat)} supplier VAT numbers, {len(ours)} MPS delivery notes with lines", flush=True)


def supplier_by_name(name, strict=0.9):
    """The Sage supplier a name (letterhead or scan folder) belongs to: best name similarity, or the name is the first
    word(s) of a supplier's name ("NRB" -> NRB PIPING SYSTEMS INLAND)."""
    w = norm_words(name)
    if not w:
        return None, 0.0
    best, bs = None, 0.0
    for r in sup_names:
        sw = norm_words(r["Name"])
        s = name_sim(name, r["Name"])
        if sw[:len(w)] == w and len(" ".join(w)) >= 3:
            s = max(s, 0.92)
        if s > bs:
            best, bs = r, s
    return (best, bs) if bs >= strict else (None, bs)


def customer_by_name(name, strict=0.85):
    best, bs = None, 0.0
    for c in cust_names:
        s_ = name_sim(name, c)
        if s_ > bs:
            best, bs = c, s_
    return (best, bs) if bs >= strict else (None, bs)


def match_ours(dn_date, frm, lines, customer=None):
    """Our delivery note this customer delivery note belongs to: (best, score, second score, why)."""
    if not dn_date or not lines:
        return None, 0, 0, "no date or no lines read"
    cands = []
    for k in range(-a.days, a.days + 1):
        cands += by_day.get(dn_date + timedelta(days=k), [])
    if customer:                                      # only our delivery notes made out to that customer
        cands = [o for o in cands if name_sim(o["customer"], customer) >= 0.85]
    scored = []
    for o in cands:
        if not o["lines"]:
            continue
        hit = 0.0
        for q, dsc in lines:
            best = 0.0
            for oq, od in o["lines"]:
                s = desc_sim(dsc, od)
                if q is not None and oq is not None and abs(q - oq) < 1e-6:
                    s += 0.35                        # the same quantity
                best = max(best, s)
            hit += min(best, 1.0)
        score = hit / max(len(lines), len(o["lines"]))
        cs = name_sim(frm, o["customer"])
        score = score * (0.6 + 0.4 * cs)             # made out to that customer counts
        scored.append((score, o, cs))
    if not scored:
        return None, 0, 0, f"no MPS delivery note within {a.days} days"
    scored.sort(key=lambda x: -x[0])
    s1, o1, cs1 = scored[0]
    s2 = scored[1][0] if len(scored) > 1 else 0.0
    return o1, round(s1, 3), round(s2, 3), f"customer name {cs1:.2f}"


checked = {}
if a.from_report:
    with open(a.from_report, encoding="utf-8-sig") as fh:
        checked = {r["file"]: r for r in csv.DictReader(fh)}
    print(f"{len(checked)} rows in the checked report {a.from_report}", flush=True)
elif a.apply:
    sys.exit("--apply needs --from-report (the dry-run report that was checked)")
rows = []
files = sorted(glob.glob(str(ROOT / "out" / "DeliveryNotes" / "hold" / "*.json")))
for js in files:
    if a.limit and len(rows) >= a.limit:
        break
    js = Path(js)
    try:
        sc = json.load(open(js, encoding="utf-8"))
    except Exception:
        continue
    if sc.get("hold_reason") != "not_a_delivery_note" and "not_a_delivery_note" not in (sc.get("flags") or []):
        continue                                          # MPS delivery notes waiting for Sage: not ours to touch
    pdf = js.with_name(str(sc.get("file") or ""))
    if not pdf.is_file():
        continue
    t0 = time.time()
    U = re.sub(r"\s+", " ", text_of(pdf)).upper()
    try:
        m = model_read(pdf)
    except Exception as e:
        m = {"error": str(e)[:150]}
    mps_issuer = bool(re.search(r"MINING\s*PRESSURE|^MPS\b", str(m.get("from_company") or ""), re.I))
    if mps_issuer:
        m["from_company_read"], m["from_company"] = m.get("from_company"), None   # not used to name the issuer
    mps = bool(re.search(r"MINING\s*PRESSURE", U)) or OWN_VAT in U or bool(re.search(r"MINING\s*PRESSURE|\bMPS\b", str(m.get("to_company") or ""), re.I))
    vats = [v for v in dict.fromkeys(re.findall(r"(?<!\d)(4\d{9})(?!\d)", U + " " + str(m.get("from_vat") or ""))) if v != OWN_VAT]
    sup = next((sup_by_vat[v] for v in vats if v in sup_by_vat), None)
    how_sup = "VAT number" if sup else ""
    if sup and m.get("from_company") and name_sim(m["from_company"], sup["Name"]) < 0.5:
        sup, how_sup = None, ""                       # a VAT number on the page that is not the letterhead's: not trusted
    if not sup and m.get("from_company"):
        sup, _ = supplier_by_name(m["from_company"])
        how_sup = "letterhead name" if sup else ""
    folder_sup, fs = supplier_by_name(Path(str(sc.get("source_folder") or "").replace("\\", "/")).name, 0.85)
    if not sup and folder_sup and (name_sim(m.get("from_company"), folder_sup["Name"]) >= 0.6
                                   or (not m.get("from_company") and (mps or " ".join(norm_words(folder_sup["Name"]))[:12] in " ".join(norm_words(U[:600]))))):
        sup, how_sup = folder_sup, "scan folder"
    d = parse_date(m.get("date_iso"))
    lines = [(num(l.get("qty")), str(l.get("description") or "")) for l in (m.get("lines") or []) if isinstance(l, dict) and l.get("description")]
    src_stem = Path(str(sc.get("source_file") or pdf.name)).stem
    base = re.sub(r" p\d+$", "", re.sub(r"^.*? - ", "", pdf.stem, count=1))
    orig = base if base.startswith(src_stem) else src_stem
    order = next(iter(re.findall(r"\bA\d{5}\b", U + " " + str(m.get("order_number") or "").upper() + " " + str(sc.get("source_file") or "").upper())), None)
    row = {"file": js.name, "source_file": sc.get("source_file"), "from": m.get("from_company"), "to": m.get("to_company"), "customer": "",
           "mps_on_it": mps, "supplier": sup["Name"] if sup else "", "supplier_how": how_sup, "date": str(d or ""),
           "their_no": m.get("dn_number"), "order": order or "", "lines": len(lines), "kind": "", "match": "", "score": "",
           "second": "", "why": "", "new_name": "", "secs": 0, "error": m.get("error", "")}
    upd, dest = None, None
    o = None
    folder = Path(str(sc.get("source_folder") or "").replace("\\", "/")).name
    cust, cs = customer_by_name(m.get("from_company"))
    if not cust:
        cust, cs = customer_by_name(folder)
        if cust and name_sim(m.get("from_company"), cust) < 0.5 and m.get("from_company"):
            cust = None                               # the scan folder names a customer, the letterhead someone else
    row["customer"] = cust or ""
    by_vat = bool(sup) and how_sup == "VAT number"
    supplier_first = bool(sup) and (by_vat or (mps and bool(order)))
    if supplier_first and cust:
        row["customer"] = f"{cust} (also a supplier; our order {order or 'VAT'})"
    if cust and not supplier_first:
        # 1. the customer's own delivery note for a delivery MPS made on their behalf: our IN number in its name, or its
        #    lines match one of our delivery notes to that customer within +-days
        named = [by_no[n] for n in re.findall(r"\bIN\s?0*(\d{5,6})\b", str(sc.get("source_file") or "").upper())
                 for n in ["IN" + n.zfill(6)] if n in by_no]
        if named:
            o, s1, s2, why = named[0], 1.0, 0.0, "our IN number in the file name"
        else:
            o, s1, s2, why = match_ours(d, m.get("from_company"), lines, customer=cust)
            if not (o and s1 >= 0.35 and s1 - s2 >= 0.1):
                o = None
        row.update(match=o["no"] if o else "", score=s1, second=s2, why=why)
        if not o:
            row["kind"] = f"customer's delivery note ({cust}), no MPS delivery note matched"
    if mps_issuer:
        oo, t1, t2, why2 = match_ours(d, None, lines)
        if oo and t1 >= 0.6:
            o = None                                  # our own delivery note (our letterhead, no barcode): for Dino
            row.update(kind=f"looks like our own delivery note {oo['no']} (no barcode)", match=oo["no"], score=t1, second=t2, why=why2)
    if o:
        row["kind"] = "on behalf of customer"
        frm = cust or m.get("from_company") or o["customer"]
        name = f"{safe(frm)}-{safe(orig, 120)}.pdf"
        row["new_name"] = name
        upd = {"fields": {"on_behalf_of_customer": True, "from_company": frm, "their_dn_number": m.get("dn_number"),
                          "our_delivery_note": o["no"], "dn_number": None, "date": str(d) if d else str(o["date"]),
                          "customer": o["customer"], "customer_account": o["account"], "job_number": o["job"],
                          "customer_order": o["order"], "deliver_to": m.get("deliver_to"),
                          "lines_text": "\n".join(f"{q:g} x {s}" if q is not None else s for q, s in lines) or None,
                          "content_hash": (sc.get("fields") or {}).get("content_hash"),
                          "review": f"delivery note made out by {frm}, matched to our {o['no']} ({row['why']}, score {row['score']})"}}
        dest = ROOT / "out" / "DeliveryNotes"
    elif row["kind"].startswith("looks like our own"):
        pass                                          # stays on hold, with the match noted
    elif sup and (not cust or supplier_first):
        # 2. a supplier's delivery note to MPS
        row["kind"] = "supplier delivery note"
        name = f"{safe(sup['Name'])}-{safe(orig, 120)}.pdf"
        row["new_name"] = name
        upd = {"doc_type": "supplier_dn", "folder": "SupplierDeliveryNotes",
               "fields": {"supplier": sup["Name"], "supplier_vat": sup.get("VatNo"), "sage_account": sup.get("SageAccount"),
                          "supplier_how": how_sup, "dn_number": m.get("dn_number"), "date": str(d) if d else None,
                          "order_number": order, "job_number": None, "deliver_to": m.get("deliver_to"),
                          "lines_text": "\n".join(f"{q:g} x {s}" if q is not None else s for q, s in lines) or None,
                          "content_hash": (sc.get("fields") or {}).get("content_hash"), "review": None if order else "no A-number found on it"}}
        dest = ROOT / "out" / "SupplierDeliveryNotes"
    elif row["kind"]:
        pass                                          # a customer's delivery note without a match: stays on hold
    elif not lines and not m.get("from_company"):
        row["kind"] = "unreadable"
    else:
        row["kind"] = "issuer neither a known supplier nor a customer"
    row["secs"] = round(time.time() - t0, 1)
    if a.from_report:
        was = checked.get(js.name)
        same = bool(was) and all(str(was.get(k) or "") == str(row.get(k) or "") for k in ("kind", "supplier", "match"))
        if not same:
            upd, dest = None, None
            row["kind_checked"] = (was or {}).get("kind", "not in the checked report")
            row["kind"] = "differs from the checked report: stays on hold (" + row["kind"] + ")"
    rows.append(row)
    print(f"{row['kind'][:28]:28s} {row['supplier'] or row['match'] or '':28s} {js.name}", flush=True)
    if not a.apply:
        continue
    sc = json.load(open(js, encoding="utf-8"))
    sc.setdefault("fields", {})["dn_sort"] = {k: row[k] for k in ("kind", "from", "to", "supplier", "supplier_how", "match", "score", "second", "why")}
    if upd and dest:
        dest.mkdir(parents=True, exist_ok=True)
        f = dict(sc.get("fields") or {})
        f.update(upd["fields"])
        sc["fields"] = f
        for k in ("doc_type", "folder"):
            if k in upd:
                sc[k] = upd[k]
        sc.pop("hold_reason", None)
        sc["flags"] = [x for x in (sc.get("flags") or []) if x != "not_a_delivery_note" and not str(x).startswith("held: ")]
        sc["needs_review"] = bool(sc["flags"])
        sc["sorted_from_hold"] = {"at": datetime.now().isoformat(timespec="seconds"), "kind": row["kind"], "was": js.name}
        new = dest / row["new_name"]
        k = 2
        while new.exists() or new.with_suffix(".json").exists():
            new = dest / f"{Path(row['new_name']).stem}_{k}.pdf"
            k += 1
        sc["file"] = new.name
        shutil.copy2(pdf, new.with_name(new.name + ".tmp"))
        os.replace(new.with_name(new.name + ".tmp"), new)
        tmp = new.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, new.with_suffix(".json"))
        gone = ROOT / "deleted" / "DeliveryNotes" / datetime.now().strftime("%Y-%m")
        gone.mkdir(parents=True, exist_ok=True)
        for q in (js, pdf):
            shutil.move(str(q), str(gone / q.name))
        (gone / (js.stem + ".rerouted.json")).write_text(json.dumps({"to": str(new.relative_to(ROOT)), "at": sc["sorted_from_hold"]["at"]}), encoding="utf-8")
    else:
        sc["hold_reason"] = sc.get("hold_reason") or "not_a_delivery_note"
        tmp = js.with_name(js.name + ".ds.tmp")
        tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, js)

out = ROOT / "reports" / f"dn hold sort {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}"
with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(dict.fromkeys(k for r in rows for k in r)) or ["file"], restval="")
    w.writeheader()
    w.writerows(rows)
from collections import Counter                      # noqa: E402
print(json.dumps({"documents": len(rows), "kinds": dict(Counter(r["kind"] for r in rows)), "applied": a.apply, "report": str(out) + ".csv"}, indent=1))

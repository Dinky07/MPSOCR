#!/usr/bin/env python3
"""
MPS delivery notes (CPU only): barcode -> invoice number -> Sage.

A scanned batch (one PDF, many pages, often upside down) is turned into one PDF per delivery note:
  1. ocrmypdf turns every page upright and adds a searchable text layer (Tesseract). Without ocrmypdf the pages are
     kept as scanned and only the barcode is used.
  2. Each page is checked for the delivery-note barcode (zbarimg). The barcode is the Sage invoice number (IN106905).
     A page with the barcode starts a new delivery note; every following page without one (packing list, release
     note, signed copy...) belongs to that delivery note. If the barcode can't be read, the printed
     "Document No IN......" in the delivery-note header is used instead (flagged).
  3. The number is looked up in sage_invoices.csv (MPSBusiness rpt.vw_SalesDocuments, D150, written to the pipeline
     share by the PC sync): date, customer, account, customer order, job, rep and the lines.
     A number not in Sage yet (the Sage copy is refreshed nightly) is held and checked again automatically.
  4. Pages before the first delivery note (a release note or anything else scanned on its own) become one document
     that goes to the hold queue: not a delivery note.

Needs: poppler-utils, zbar-tools (zbarimg), tesseract-ocr; ocrmypdf recommended.  Standard library only.
"""
import csv, hashlib, os, re, shutil, subprocess, tempfile
from datetime import date
from pathlib import Path

DEFAULTS = {
    "barcode_pattern": r"^IN\d{5,7}$",     # what a delivery-note barcode looks like
    "prefer_company": "MPS",               # Sage company to prefer when the same number exists in more than one
    "sage_csv": "sage_invoices.csv",       # relative to the pipeline root
    "ocr": True,                           # ocrmypdf: upright pages + text layer
    "ocr_jobs": 2,
    "ocr_timeout": 1800,
    "barcode_dpi": 150,
    "hold_if_not_in_sage": True,
}


def run(cmd, timeout=300, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def page_count(pdf):
    return int(re.search(r"Pages:\s+(\d+)", run(["pdfinfo", str(pdf)], 60).stdout).group(1))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- Sage copy
class SageInvoices:
    """sage_invoices.csv indexed by document number; reloaded when the file changes."""

    def __init__(self, path):
        self.path = Path(path)
        self.mtime = None
        self.idx = {}

    def refresh(self):
        """True when the file changed (or appeared) since the last call."""
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            changed = self.mtime is not None
            self.mtime, self.idx = None, {}
            return changed
        if m == self.mtime:
            return False
        idx = {}
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                k = norm_number(r.get("DocNumber"))
                if k:
                    idx.setdefault(k, []).append(r)
        self.idx, self.mtime = idx, m
        return True

    @property
    def loaded(self):
        return self.mtime is not None

    def lookup(self, number, prefer_company="MPS"):
        rows = self.idx.get(norm_number(number)) or []
        if not rows:
            return None, 0
        best = sorted(rows, key=lambda r: (r.get("Company") != prefer_company, not (r.get("LineCount") or "0").strip("0"),
                                           -(int(r.get("AutoIndex") or 0))))[0]
        return best, len(rows)


def norm_number(s):
    return re.sub(r"[\s\-_/.]", "", str(s or "")).upper()


# --------------------------------------------------------------------------- page reading
def make_upright(src, dst, cfg, log=None):
    """ocrmypdf: rotate pages upright and add a text layer. Returns how the pages were prepared."""
    if cfg.get("ocr", True) and shutil.which("ocrmypdf"):
        cmd = ["ocrmypdf", "-q", "--rotate-pages", "--rotate-pages-threshold", str(cfg.get("rotate_threshold", 2)),
               "--skip-text", "--output-type", "pdf", "--jobs", str(cfg.get("ocr_jobs", 2)), "-l", cfg.get("ocr_lang", "eng"),
               str(src), str(dst)]
        r = run(cmd, timeout=int(cfg.get("ocr_timeout", 1800)), check=False)
        if r.returncode == 0 and Path(dst).exists():
            return "ocrmypdf"
        if log:
            log.warning("ocrmypdf failed (%s); using the scan as it is: %s", r.returncode, (r.stderr or "").strip()[-300:])
    shutil.copy2(src, dst)
    return "as_scanned"


def page_texts(pdf, n):
    out = []
    for i in range(1, n + 1):
        r = run(["pdftotext", "-layout", "-f", str(i), "-l", str(i), str(pdf), "-"], 60, check=False)
        out.append(r.stdout if r.returncode == 0 else "")
    return out


def page_barcodes(pdf, n, dpi, td):
    """[[barcode, ...] per page]; zbarimg reads every page image in one call per page."""
    td = Path(td)
    run(["pdftoppm", "-r", str(dpi), "-gray", str(pdf), str(td / "bc")], timeout=600)
    imgs = sorted(td.glob("bc*.pgm"), key=lambda p: int(re.search(r"(\d+)\.pgm$", p.name).group(1)))
    res = []
    for img in imgs[:n]:
        r = run(["zbarimg", "-q", "--raw", str(img)], 60, check=False)     # exit 4 = nothing found
        res.append([x.strip() for x in r.stdout.splitlines() if x.strip()])
        img.unlink(missing_ok=True)
    while len(res) < n:
        res.append([])
    return res


def barcode_retry(pdf, page, dpi, td):
    td = Path(td)
    run(["pdftoppm", "-r", str(dpi), "-gray", "-f", str(page), "-l", str(page), str(pdf), str(td / "rt")], timeout=120)
    out = []
    for img in td.glob("rt*.pgm"):
        r = run(["zbarimg", "-q", "--raw", str(img)], 60, check=False)
        out += [x.strip() for x in r.stdout.splitlines() if x.strip()]
        img.unlink(missing_ok=True)
    return out


def words(*ws):
    """Regex for words as Tesseract may print them: stray spaces inside a word ("Packing Lis t"), any spacing between."""
    return r"\s+".join(r"\s?".join(re.escape(c) for c in w) for w in ws)


_DN_HEADER = re.compile(words("Delivery", "Note"), re.I)
_DOCNO = re.compile(words("Document", "No") + r"\.?[\s:|_.\-]*([I1l|][NM])\s?(\d{5,7})\b", re.I)
KINDS = [("packing_list", re.compile(words("Packing", "List"), re.I)),
         ("release_note", re.compile(words("RELEASE", "NOTE") + "|" + words("RELEASE", "NUMBER"), re.I)),
         ("delivery_note", _DN_HEADER),
         ("invoice", re.compile(words("TAX", "INVOICE"), re.I))]
REVIEW_TEXT = {"not_in_sage": "Not in Sage", "no_sage_copy": "No Sage copy", "barcode_unreadable": "Barcode unreadable: number from the print",
               "several_barcodes_on_page": "More than one delivery-note barcode on the page",
               "printed_date_differs_from_sage": "Printed date differs from Sage", "printed_account_differs_from_sage": "Printed account differs from Sage",
               "packing_list_number_differs": "Packing list number differs", "not_a_delivery_note": "Not a delivery note"}


def review_text(flags):
    return "; ".join(REVIEW_TEXT.get(x, x) for x in flags) or None


KIND_LABEL = {"packing_list": "Packing list", "release_note": "Release note", "delivery_note": "Delivery note copy",
              "invoice": "Invoice", "other": "Other page"}


def page_kind(text):
    for k, rx in KINDS:
        if rx.search(text or ""):
            return k
    return "other"


# --------------------------------------------------------------------------- printed fields (fallback / cross-check)
def text_fields(text):
    """Best-effort fields from the delivery note's own text layer."""
    f = {}
    m = re.search(r"\bDate\s+(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text or "")
    if m:
        d, mo, y = map(int, m.groups())
        try:
            f["date"] = date(y, mo, d).isoformat()
        except ValueError:
            pass
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if re.search(r"Customer\s+Order", ln) and re.search(r"Account|Job|Drawing", ln):
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip():
                    cells = [c for c in re.split(r"\s{2,}", nxt.strip()) if c]
                    if cells and re.fullmatch(r"[A-Z0-9]{3,10}", cells[0]):
                        f["account"] = cells[0]
                    if len(cells) > 1 and re.search(r"\w", cells[1]) and not re.search(r"\d{4,5}[A-Z]\b", cells[1]):
                        f["customer_order"] = cells[1]
                    jm = re.search(r"\b(\d{4,5}[A-Z])\b", nxt)
                    if jm:
                        f["job_number"] = jm.group(1)
                    break
            break
    # customer: the left column (left of "Deliver to:") of the line that carries "Deliver to:", else the first line
    # under the "Deep down, we care" strap
    for i, ln in enumerate(lines):
        x = ln.find("Deliver to")
        if x > 10:
            left = re.sub(r"\s+", " ", ln[:x]).strip(" |")
            if len(left) >= 4 and re.search(r"[A-Z]{3}", left):
                f["customer"] = left
            break
    if not f.get("customer"):
        for i, ln in enumerate(lines):
            if re.search(r"Deep\s+down", ln, re.I):
                for nxt in lines[i + 1:i + 6]:
                    left = re.split(r"\s{3,}", nxt.strip())[0] if nxt.strip() else ""
                    if len(left) >= 6 and " " in left and re.search(r"[A-Z]{3}", left) and not re.search(r"Deliver|NI0|IN1", left):
                        f["customer"] = left
                        break
                break
    # deliver to: the right-hand column under "Deliver to:"
    for i, ln in enumerate(lines):
        x = ln.find("Deliver to")
        if x >= 0:
            out = []
            for nxt in lines[i + 1:i + 8]:
                if re.search(r"DELIVERED\s+BY", nxt, re.I):
                    break
                seg = nxt[max(0, x - 2):].strip()
                if seg:
                    out.append(seg)
            if out:
                f["deliver_to"] = ", ".join(out)
            break
    items = []
    start = next((i for i, ln in enumerate(lines) if re.search(r"Quantity\s+Item\s+Description", ln)), None)
    if start is None:      # header row not read: start after the account / order / job values line
        hdr = next((i for i, ln in enumerate(lines) if re.search(r"Customer\s+Order", ln)), None)
        if hdr is not None:
            start = next((k for k in range(hdr + 1, min(hdr + 4, len(lines))) if lines[k].strip()), hdr)
    if start is not None:
        for ln in lines[start + 1:start + 40]:
            m = re.match(r"^\s{0,20}(\d+(?:[.,]\d+)?)\s{2,}(\S.*?)(?:\s{3,}(each|meter|metre|m|kg|set|length|lengths|pcs)\b.*)?$", ln, re.I)
            if m:
                items.append({"quantity": m.group(1), "description": re.sub(r"\s+", " ", m.group(2)).strip(), "unit": m.group(3)})
            elif re.search(r"RECEIVED\s+IN\s+GOOD", ln):
                break
    if items:
        f["items"] = items
    return f


# --------------------------------------------------------------------------- the reader
def tidy_qty(q):
    try:
        v = float(q)
        return str(int(v)) if v == int(v) else f"{v:g}"
    except (TypeError, ValueError):
        return str(q or "").strip()


def sage_fields(row):
    items = []
    for part in (row.get("Lines") or "").split(" | "):
        m = re.match(r"^\s*(-?[\d.]+)\s+x\s+(.*)$", part)
        if m:
            items.append({"quantity": tidy_qty(m.group(1)), "description": m.group(2).strip()})
        elif part.strip():
            items.append({"quantity": None, "description": part.strip()})

    def g(k):
        v = (row.get(k) or "").strip()
        return v or None

    def n(k):
        try:
            return float(row.get(k))
        except (TypeError, ValueError):
            return None
    return {
        "sage_company": g("Company"), "date": g("DocDate"), "customer": g("CustomerName"),
        "customer_account": g("CustomerAccount"), "customer_order": g("CustomerOrder"), "ext_order": g("ExtOrderNum"),
        "job_number": g("JobNumber"), "job_name": g("JobName"), "rep": g("Rep"), "contact": g("Contact"),
        "description": g("Description"), "quote_ref": g("QuoteNum"), "total_excl": n("TotalExcl"), "total_incl": n("TotalIncl"),
        "sage_doc_type": g("DocType"), "sage_doc_state": g("DocState"), "items": items,
    }


def lines_text(items, limit=4000):
    t = "\n".join(f"{(i.get('quantity') or '').strip() + ' x ' if i.get('quantity') else ''}{i.get('description') or ''}" for i in items)
    return t[:limit]


def read(pdf, root, cfg, sage, log=None, name=None):
    """Split one scanned batch into delivery notes. Returns (prep, docs); each doc:
       {"pages": [..], "pdf": Path (in a temp dir the caller owns via the returned 'tmp'), "fields": {}, "flags": [],
        "hold": reason or None, "stem": str}"""
    c = dict(DEFAULTS, **(cfg or {}))
    bc_rx = re.compile(c["barcode_pattern"], re.I)
    tmp = Path(tempfile.mkdtemp(prefix="dn-"))
    upright = tmp / "upright.pdf"
    prep = make_upright(pdf, upright, c, log)
    n = page_count(upright)
    texts = page_texts(upright, n)
    with tempfile.TemporaryDirectory() as td:
        codes = page_barcodes(upright, n, int(c["barcode_dpi"]), td)
        pages = []
        for i in range(n):
            dn = sorted({norm_number(x) for x in codes[i] if bc_rx.match(norm_number(x))})
            kind = page_kind(texts[i])
            if kind == "other" and pages and pages[-1]["kind"] == "packing_list":
                kind = "packing_list"          # page 2, 3... of a packing list
            how = "barcode" if dn else None
            if not dn and kind == "delivery_note":
                # looks like a delivery note but no barcode read: try harder, then the printed Document No
                dn = sorted({norm_number(x) for x in barcode_retry(upright, i + 1, 300, td) if bc_rx.match(norm_number(x))})
                how = "barcode" if dn else None
                m = _DOCNO.search(texts[i]) if not dn else None
                if m and re.search(r"Page\s+1\s+of", texts[i]):
                    dn, how = ["IN" + m.group(2)], "printed_number"
            pages.append({"no": i + 1, "dn": dn, "kind": kind, "how": how, "other_codes": [x for x in codes[i] if not bc_rx.match(norm_number(x))]})

    groups = []
    for p in pages:
        if p["dn"] or not groups:
            groups.append([p])
        else:
            groups[-1].append(p)

    sage.refresh()
    docs = []
    for gi, grp in enumerate(groups, 1):
        first = grp[0]
        part = tmp / f"doc{gi:03d}.pdf"
        nos = [p["no"] for p in grp]
        if len(nos) == n:
            shutil.copy2(upright, part)
        else:
            run(["pdfseparate", "-f", str(nos[0]), "-l", str(nos[-1]), str(upright), str(tmp / f"g{gi:03d}-%04d.pdf")], 120)
            singles = [str(tmp / f"g{gi:03d}-{k:04d}.pdf") for k in nos]
            if len(singles) == 1:
                shutil.move(singles[0], part)
            else:
                run(["pdfunite", *singles, str(part)], 120)
                for s in singles:
                    Path(s).unlink(missing_ok=True)
        extra = [p["kind"] for p in grp[1:]]
        extra_txt = "; ".join(f"{KIND_LABEL.get(k, k)} ({extra.count(k)} page{'s' if extra.count(k) > 1 else ''})"
                              for k in dict.fromkeys(extra))
        flags, hold = [], None
        if not first["dn"]:
            kinds = [p["kind"] for p in grp]
            main = next((k for k in kinds if k != "other"), "other")
            label = KIND_LABEL.get(main, "Other").replace(" copy", "")
            f = {"doc_kind": main, "doc_label": label, "pages": len(grp), "source_pages": f"{nos[0]}-{nos[-1]}" if len(nos) > 1 else str(nos[0]),
                 "page_kinds": "; ".join(f"{KIND_LABEL.get(k, k)} ({kinds.count(k)})" for k in dict.fromkeys(kinds))}
            m = re.search(r"RELEASE\s+NUMBER\s+NO[:.]?\s*([A-Z0-9 /.-]{4,40})", texts[first["no"] - 1], re.I)
            if m:
                f["release_number"] = re.sub(r"\s+", " ", m.group(1)).strip(" /")
            flags.append("not_a_delivery_note")
            hold = "not_a_delivery_note"
            stem = re.sub(r"[^A-Za-z0-9 _.-]+", "_", f"{label} - {Path(name or pdf).stem} p{f['source_pages']}")
        else:
            num = first["dn"][0]
            if len(first["dn"]) > 1:
                flags.append("several_barcodes_on_page")
            f = {"dn_number": num, "number_read_from": first["how"], "pages": len(grp),
                 "source_pages": f"{nos[0]}-{nos[-1]}" if len(nos) > 1 else str(nos[0]),
                 "extra_pages": extra_txt or None, "doc_label": "Delivery Note"}
            if first["how"] == "printed_number":
                flags.append("barcode_unreadable")
            txt = text_fields(texts[first["no"] - 1])
            row, nrows = sage.lookup(num, c["prefer_company"]) if sage.loaded else (None, 0)
            if row:
                f.update(sage_fields(row))
                f["sage_found"] = True
                if nrows > 1:
                    f["sage_matches"] = nrows
                if txt.get("date") and f.get("date") and txt["date"] != f["date"]:
                    flags.append("printed_date_differs_from_sage")
                if txt.get("account") and f.get("customer_account") and norm_number(txt["account"]) != norm_number(f["customer_account"]):
                    flags.append("printed_account_differs_from_sage")
                f["deliver_to"] = txt.get("deliver_to")
            else:
                f.update({k: v for k, v in txt.items()})
                f["customer_account"] = f.pop("account", None)
                f["sage_found"] = False
                flags.append("not_in_sage" if sage.loaded else "no_sage_copy")
                if c.get("hold_if_not_in_sage", True):
                    hold = "not_in_sage" if sage.loaded else "no_sage_copy"
            f["printed_date"] = txt.get("date")
            for k in ("customer_order", "job_number"):
                if txt.get(k) and not f.get(k):
                    f[k] = txt[k]
            # a packing list carries the same IN number: check it
            for p in grp[1:]:
                if p["kind"] == "packing_list":
                    m = re.search(r"Packing\s+List\s+([I1l][NM])?\s?(\d{5,7})", texts[p["no"] - 1], re.I)
                    if m and "IN" + m.group(2) != num:
                        flags.append("packing_list_number_differs")
            f["items"] = f.get("items") or []
            f["line_count"] = len(f["items"])
            f["lines_text"] = lines_text(f["items"]) or None
            stem = num
        f["content_hash"] = sha256(part).upper()
        f["review"] = review_text(flags)
        f["library_doc_type"] = f.get("doc_label")
        docs.append({"pages": nos, "pdf": part, "fields": f, "flags": flags, "hold": hold, "stem": stem})
    return prep, docs, tmp


def recheck(sidecar, sage, cfg=None):
    """A held 'not_in_sage' delivery note: look it up again. Returns the updated sidecar or None if still missing."""
    c = dict(DEFAULTS, **(cfg or {}))
    f = sidecar.get("fields") or {}
    if not f.get("dn_number") or not sage.loaded:
        return None
    row, nrows = sage.lookup(f["dn_number"], c["prefer_company"])
    if not row:
        return None
    printed = {"date": f.get("printed_date"), "account": f.get("customer_account")}
    f.update(sage_fields(row))
    f["sage_found"] = True
    if nrows > 1:
        f["sage_matches"] = nrows
    f["line_count"] = len(f["items"])
    f["lines_text"] = lines_text(f["items"]) or None
    flags = [x for x in sidecar.get("flags") or [] if x not in ("not_in_sage", "no_sage_copy")]
    if printed["date"] and f.get("date") and printed["date"] != f["date"]:
        flags.append("printed_date_differs_from_sage")
    f["review"] = review_text(flags)
    sidecar.update(fields=f, flags=flags, needs_review=bool(flags))
    sidecar.pop("hold_reason", None)
    return sidecar


if __name__ == "__main__":       # quick look at one batch:  python3 dn_reader.py scan.pdf [sage_invoices.csv]
    import json, sys
    s = SageInvoices(sys.argv[2] if len(sys.argv) > 2 else "/nonexistent")
    prep, docs, tmp = read(Path(sys.argv[1]), None, {}, s)
    print("pages prepared:", prep)
    for d in docs:
        print(json.dumps({"pages": d["pages"], "stem": d["stem"], "hold": d["hold"], "flags": d["flags"],
                          "fields": {k: v for k, v in d["fields"].items() if k not in ("content_hash",)}}, ensure_ascii=False))
    shutil.rmtree(tmp, ignore_errors=True)

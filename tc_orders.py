#!/usr/bin/env python3
"""Order numbers and the letter of credit on test certificates, read from the certificate's own text (Dino 26 Oct:
DOC010217-008 prints "SC No. DF0940/DF0941" and "Documentary Credit Number: IL16341ZA0704992", and the model read neither).

The model's reading of a certificate misses numbers printed in odd places (sales-contract boxes, footers). This reads the
PDF's text layer (or Tesseract OCR when the scan has none) and finds:
  MPS orders A#####, Ronin import orders DF####, contracts CNxxx####-#, and the documentary credit / L/C number,
then checks the orders against Sage (pipeline sage_po.csv: Ronin supplier invoices carry "DF0940+DF0941" as their order).

    fields: mps_po, ronin_po, contract (merged with the model's), lc_number, order_check (what Sage has for them)
    python3 tc_orders.py scan [--apply] [--limit N]   every test cert sidecar (out, hold, synced): report + update
"""
import csv, json, os, re, subprocess, sys, tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("MPS_PIPELINE_ROOT", "/srv/ocr/scans/pipeline"))
_TEXT_CACHE = ROOT / "work" / "tc_text"

LC_LABEL = re.compile(r"(?:DOCUMENTARY\s+CREDIT|LETTER\s+OF\s+CREDIT|L\s*/\s*C|\bLC|CREDIT)\s*(?:NUMBER|NO\b\.?|NR\b\.?|#)?\s*[:.\-]?\s*"
                      r"([A-Z0-9][A-Z0-9/\-]{5,30})", re.I)
DF_RUN = re.compile(r"(?<![A-Z0-9])DF\s?-?\s?(\d{3,4})((?:\s*[/&,+]\s*(?:DF\s?-?\s?)?\d{3,4})*)(?!\d)", re.I)
MPS_RE = re.compile(r"(?<![A-Z0-9])A-?(\d{5})(?![\d.])")
CN_RE = re.compile(r"(?<![A-Z0-9])(CN[A-Z]{2,4}\d{4,6}[A-Z]{0,3}(?:-\d+)?)")


def _run(cmd, timeout=600):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def pdf_text(pdf, max_pages=30, key=None):
    """The certificate's text: the PDF text layer, or Tesseract OCR (300 dpi) when the pages have none. Cached by key."""
    cache = _TEXT_CACHE / f"{key}.txt" if key else None
    if cache and cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")
    txt = ""
    try:
        txt = _run(["pdftotext", "-layout", "-l", str(max_pages), str(pdf), "-"], 300).stdout.decode("utf-8", "replace")
    except Exception:
        txt = ""
    if len(re.sub(r"\s", "", txt)) < 150:                       # a scan without a text layer: OCR it
        parts = []
        with tempfile.TemporaryDirectory() as td:
            try:
                _run(["pdftoppm", "-r", "300", "-gray", "-l", str(max_pages), "-png", str(pdf), f"{td}/p"], 900)
            except Exception:
                pass
            for png in sorted(Path(td).glob("p*.png")):
                try:
                    parts.append(_run(["tesseract", str(png), "-", "--psm", "3"], 300).stdout.decode("utf-8", "replace"))
                except Exception:
                    pass
        txt = "\n\f".join(parts)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(txt, encoding="utf-8")
    return txt


def find(text):
    """-> {"mps": [...], "ronin": [...], "contracts": [...], "lc": [...]} from a certificate's text."""
    up = (text or "").upper()
    ronin = []
    for m in DF_RUN.finditer(up):                                # DF0940/DF0941, DF0940/0941, DF940 & 941
        for n in [m.group(1)] + re.findall(r"(\d{3,4})", m.group(2) or ""):
            v = "DF" + n.zfill(4)
            if v not in ronin:
                ronin.append(v)
    mps = []
    for m in MPS_RE.finditer(up):
        v = "A" + m.group(1)
        if v not in mps:
            mps.append(v)
    contracts = []
    for m in CN_RE.finditer(up):
        if m.group(1) not in contracts:
            contracts.append(m.group(1))
    lc = []
    for m in LC_LABEL.finditer(up):
        v = m.group(1).strip("-/.")
        # a real credit number has letters and digits and isn't a date, a heat, an order or a contract already found
        if not (re.search(r"\d", v) and re.search(r"[A-Z]", v)) or len(re.sub(r"\D", "", v)) < 5:
            continue
        if v.startswith(("DF", "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9")) and len(v) <= 6:
            continue
        if v in contracts or v.startswith("NUMBER"):
            continue
        if v not in lc:
            lc.append(v)
    return {"mps": mps, "ronin": ronin, "contracts": contracts, "lc": lc}


_SAGE = None


def sage_orders():
    """order number -> [Sage documents] from sage_po.csv (all companies): DF#### and A##### found in OrderNum / DocNumber."""
    global _SAGE
    if _SAGE is not None:
        return _SAGE
    m = {}
    p = ROOT / "sage_po.csv"
    if p.exists():
        with open(p, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                blob = f"{r.get('OrderNum') or ''} {r.get('DocNumber') or ''} {r.get('ExtOrderNum') or ''}".upper()
                keys = set("DF" + n.zfill(4) for n in re.findall(r"(?<![A-Z0-9])DF\s?-?(\d{3,4})(?!\d)", blob))
                keys |= set("A" + n for n in re.findall(r"(?<![A-Z0-9])A(\d{5})(?!\d)", blob))
                for k in keys:
                    m.setdefault(k, []).append(r)
    _SAGE = m
    return m


def sage_text(orders):
    """'DF0940: RONIN Supplier invoice CNZAN16264BE-1 NIPPON STEEL 2018-01-15; ...' and the orders Sage doesn't have."""
    s = sage_orders()
    parts, missing = [], []
    for o in orders:
        rows = s.get(o) or []
        if not rows:
            missing.append(o)
            continue
        seen, docs = set(), []
        for r in sorted(rows, key=lambda r: r.get("DocDate") or ""):
            k = (r.get("Company"), r.get("Source"), r.get("DocNumber"))
            if k in seen:
                continue
            seen.add(k)
            docs.append(f"{r.get('Company')} {r.get('Source')} {r.get('DocNumber')} {r.get('VendorName')} {r.get('DocDate')}")
        parts.append(f"{o}: " + "; ".join(docs[:4]) + (f" +{len(docs) - 4} more" if len(docs) > 4 else ""))
    return parts, missing


def _merge(cur, new):
    have = [x.strip() for x in str(cur or "").split(";") if x.strip()]
    for v in new:
        if v not in have:
            have.append(v)
    return "; ".join(have) or None


def _orders(cur, new, kind):
    """DF / A order numbers from what is there (the model, an edit like "DF0940/DF0941") plus the cert text, one each."""
    f = find(str(cur or ""))
    out = []
    for v in f[kind] + list(new):
        if v not in out:
            out.append(v)
    return "; ".join(out) or None


_DIG = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "J": "1", "T": "1", "U": "1", "Z": "2", "S": "5", "G": "6", "B": "8"})


def norm_lc(v):
    """Standard Bank import credits are IL + 5 digits + ZA + 7 digits (IL16341ZA0704992): OCR slips in that shape are
    put right (ILL8214ZA0707216 -> IL18214ZA0707216, JU8134... -> IL18134...). Other banks' numbers are kept as read."""
    v = re.sub(r"[^A-Z0-9/]", "", str(v or "").upper())
    m = re.fullmatch(r"([A-Z0-9]{1,2})([A-Z0-9]{5})ZA([A-Z0-9/]{2,9})", v)
    if m and re.search(r"\d", m.group(2)):
        tail = re.sub(r"/.*$", "", m.group(3)).translate(_DIG)
        return "IL" + m.group(2).translate(_DIG) + "ZA" + tail
    return v


def _lcs(cur, new):
    vals = []
    for v in [x for x in str(cur or "").split(";")] + list(new):
        v = norm_lc(v)
        if v and v not in vals:
            vals.append(v)
    # a credit read cut short (IL20358ZA07) beside the whole one (IL20358ZA0712863) is the same credit
    vals = [v for v in vals if not any(o != v and o.startswith(v) for o in vals)]
    return "; ".join(vals) or None


def apply(f, flags, pdf, key=None, max_pages=30):
    """Add what the certificate's text shows to the reader's fields (never removes what the model read)."""
    found = find(pdf_text(pdf, max_pages, key))
    for o in f.get("order_numbers") or []:                        # the model may list the credit among the order numbers
        lab, val = str(o.get("label") or "").upper(), str(o.get("value") or "").upper().strip()
        if re.search(r"CREDIT|L\s*/\s*C|\bLC\b", lab) and val and val not in found["lc"]:
            found["lc"].append(val)
    added = []
    for fld, k in (("mps_po", "mps"), ("ronin_po", "ronin"), ("contract", "contracts")):
        before = f.get(fld)
        f[fld] = _merge(before, found[k]) if k == "contracts" else _orders(before, found[k], k)
        if f[fld] != before:
            added.append(f"{fld} {f[fld]}")
    if found["lc"] or f.get("lc_number"):
        f["lc_number"] = _lcs(f.get("lc_number"), found["lc"])
    if not f.get("purchaser"):
        if f.get("ronin_po") and not f.get("mps_po"):
            f["purchaser"] = "Ronin"
        elif f.get("mps_po") and not f.get("ronin_po"):
            f["purchaser"] = "MPS"
    orders = [x.strip() for x in f"{f.get('ronin_po') or ''};{f.get('mps_po') or ''}".split(";") if x.strip()]
    if orders:
        parts, missing = sage_text(orders)
        f["order_check"] = (" | ".join(parts) + (f" | not in Sage: {', '.join(missing)}" if missing else "")).strip(" |")[:2000]
    if f.get("lc_number") and ";" in f["lc_number"]:
        flags.append(f"check: more than one credit number on the cert: {f['lc_number']}")
    return found, added


# ------------------------------------------------------------------------------------------------ scan of every cert
def sidecars():
    for base, pat in ((ROOT / "out" / "TestCerts", "*.json"), (ROOT / "out" / "TestCerts" / "hold", "*.json"),
                      (ROOT / "synced" / "TestCerts", "*/*.json")):
        for js in sorted(base.glob(pat)):
            yield js


def scan(apply_changes, limit=0):
    rows, n = [], 0
    by_df = {}
    for js in sidecars():
        try:
            sc = json.load(open(js, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pdf = js.with_name(str(sc.get("file") or ""))
        if not pdf.is_file():
            continue
        n += 1
        if limit and n > limit:
            break
        f = sc.setdefault("fields", {})
        before = {k: f.get(k) for k in ("mps_po", "ronin_po", "contract", "lc_number", "order_check", "purchaser")}
        flags = []
        found, added = apply(f, flags, pdf, (f.get("content_hash") or js.stem)[:32])
        after = {k: f.get(k) for k in before}
        changed = after != before
        where = "synced" if "synced" in js.parts else "hold" if js.parent.name == "hold" else "out"
        for d in (f.get("ronin_po") or "").split(";"):
            d = d.strip()
            if d:
                by_df.setdefault(d, set()).update(x.strip() for x in (f.get("lc_number") or "").split(";") if x.strip())
        rows.append({"file": sc.get("file"), "where": where, "mps_po": after["mps_po"], "ronin_po": after["ronin_po"],
                     "contract": after["contract"], "lc_number": after["lc_number"], "order_check": after["order_check"],
                     "added": "; ".join(added), "model_read": json.dumps(f.get("order_numbers") or [])[:300]})
        if changed and apply_changes:
            if where == "synced":
                sc["resync_fields"] = datetime.now().isoformat(timespec="seconds")      # the PC sync fills the new columns
            tmp = js.with_name(js.name + ".tmp")
            tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, js)
        print(f"{n:4} {where:6} {str(sc.get('file'))[:44]:44} DF {after['ronin_po'] or '-':22} A {after['mps_po'] or '-':14} "
              f"LC {after['lc_number'] or '-':20} {'+' + '; '.join(added) if added else ''}", flush=True)
    # one credit per import order: different credit numbers on certs of the same DF order are worth a look
    for r in rows:
        conf = []
        for d in (r["ronin_po"] or "").split(";"):
            d = d.strip()
            vals = sorted(by_df.get(d, ()))
            vals = [v for v in vals if not any(o != v and o.startswith(v) for o in vals)]
            if d and len(vals) > 1:
                conf.append(f"{d}: {', '.join(vals)}")
        r["lc_conflict"] = " | ".join(conf)
    out = ROOT / "reports" / f"test cert orders {datetime.now():%Y-%m-%d %H%M}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)
    tot = lambda k: sum(1 for r in rows if r[k])
    print(f"\n{len(rows)} certs: DF orders on {tot('ronin_po')}, MPS orders on {tot('mps_po')}, contracts on {tot('contract')}, "
          f"credit numbers on {tot('lc_number')}; added from the cert text on {tot('added')}; "
          f"credit numbers differing within a DF order: {tot('lc_conflict')}\nreport: {out}"
          + ("" if apply_changes else "\nDRY RUN: sidecars not changed (add --apply)"))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "text"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if a.cmd == "text":
        t = pdf_text(a.path)
        print(json.dumps(find(t), indent=1))
    else:
        scan(a.apply, a.limit)

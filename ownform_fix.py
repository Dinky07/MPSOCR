#!/usr/bin/env python3
"""Own order forms the reader got wrong on the Purchase / Polymer Orders hold (Dino 30 Oct, from the sorter labels):
MPS / Polymer orders it read with their number but marked "not an order", and IGTs it recognised but read no IGT number
for. A document is corrected only when its page shows the printed form, so invoices / pro formas filed under an order's
number are left alone:
  MPS order      MPS letterhead in the title area + printed "ORDER A <n>" equal to the number read, no other title
  Polymer order  POLYMER LINERS in the title area + printed "ORDER PL <n>" equal to the number read, no other title
  IGT            "INTERNAL GROUP TRANSFER" / "I.G.T. NO" + the IGT number from the file name (KUSASA IGT-15571-...),
                 or from the page when the file name has none (never the prompt's example 16013)
Correction: form (+ po_doc_type / Document Type), the IGT number, the not_an_order / missing_order_number flags dropped,
the hold reason recounted; the reader's form kept as form_read and the change in edits. Stays on hold:
po_autocheck --hold releases what is then clear. Skipped: checked, uploaded, merged scans, locked sidecars.
DRY RUN unless --apply. -> reports/ownform fix <stamp>[ dry run].csv

    python3 ownform_fix.py [--apply]
"""
import argparse, csv, glob, json, os, re, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import dashboard as D                              # noqa: E402  _recompute()
import po_reader                                   # noqa: E402
import presort                                     # noqa: E402  page_text() (second OCR)
import route_sort                                 # noqa: E402  text_of() (OCR cache)
import sidecar_lock                                # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
HELD = re.compile(r"^held: ")
OTHER_TITLE = re.compile(r"\bTAX INVOICE\b|\bPRO ?-?FORMA\b|\bQUOTATION\b|\bSTATEMENT\b|\bCREDIT NOTE\b|\bDELIVERY NOTE\b|\bREMITTANCE\b|\bRECORD OF PAYMENT\b")
FORM_LABEL = next((v for v in vars(po_reader).values() if isinstance(v, dict) and v.get("not_an_order") == "Not an order"), {})


def digits(s):
    return re.sub(r"\D", "", s or "")


def title(text, n=10):
    lines = [l.strip() for l in (text or "").splitlines() if len(l.strip()) > 2]
    return re.sub(r"\s+", " ", " ".join(lines[:n])).upper()


def judge(sc, text, text2=""):
    """-> (new form, new order number, evidence) or None. text: the OCR cache; text2: the pre-sort's own OCR (the red
    printed number is often misread in one of them). The number itself is the reader's; it has to match the original
    file name (named by hand) or the printed number as OCR'd."""
    f = sc.get("fields") or {}
    form, on = f.get("form"), str(f.get("order_number") or "").upper().replace(" ", "")
    texts = [t for t in (text, text2) if t]
    Ts, Bs = [title(t) for t in texts], [re.sub(r"\s+", " ", t).upper() for t in texts]
    T, B = " | ".join(Ts), " | ".join(Bs)
    name = f"{sc.get('source_file') or ''} {sc.get('file') or ''}".upper()
    src = str(sc.get("source_file") or "").upper()
    for kind, head, pre, n in (("mps_order", r"MINING\s*PRESSURE\s*SYS|DEEP DOWN,? WE CARE|4320123039", r"A", 1),
                               ("polymer_order", r"POLYMER\s*LINERS", r"P\s*L", 2)):
        if form != "not_an_order" or not re.fullmatch(("A\\d{5}" if n == 1 else "PL\\d{4}"), on):
            continue
        if not any(re.search(head, t) for t in Ts) or any(OTHER_TITLE.search(t) for t in Ts):
            return None
        if not re.search(rf"\bORDER\s*:?\s*{pre}\s*[0-9/]", B):
            return None
        ocr = [digits(x) for x in re.findall(rf"\bORDER\s*:?\s*{pre}\s*([0-9][0-9 ]{{3,7}})", B)]
        if on[n:] in ocr:
            return kind, on, f"letterhead + printed ORDER {on[:n]} {on[n:]}"
        if re.search(rf"(?<![A-Z0-9]){on[:n]}\s?-?{on[n:]}(?!\d)", src):
            return kind, on, f"letterhead + printed ORDER {on[:n]}, {on} also in the original file name"
        return None
    if form in ("internal_group_transfer", "not_an_order") and not on:
        if not re.search(r"INTERNAL\s*GROUP\s*TRANSFER|\bI\s*\.?\s*G\s*\.?\s*T\s*\.?\s*NO\b", B) or OTHER_TITLE.search(T):
            return None
        fn = re.search(r"\bIGT[ -]?(\d{5})\b", name)
        if fn:
            return "internal_group_transfer", fn.group(1), f"IGT heading + IGT {fn.group(1)} in the file name"
        pg = [d for d in (digits(x) for x in re.findall(r"I\s*\.?\s*G\s*\.?\s*T\s*\.?\s*NO\s*\.?\s*:?\s*([0-9][0-9 ]{4,7})", B))
              if len(d) == 5 and d != "16013"]
        if len(set(pg)) == 1:
            return "internal_group_transfer", pg[0], f"IGT heading + printed I.G.T. NO {pg[0]}"
    return None


def fix(js, sc, new_form, new_on, why, stamp):
    f = sc["fields"]
    changes = []

    def put(k, v):
        if f.get(k) != v:
            changes.append({"field": k, "old": f.get(k), "new": v})
            f[k] = v
    f.setdefault("form_read", f.get("form"))
    put("form", new_form)
    if FORM_LABEL.get(new_form):
        put("form_label", FORM_LABEL[new_form])
    put("po_doc_type", "internal_transfer" if new_form == "internal_group_transfer" else "purchase_order")
    if new_form != "internal_group_transfer" and f.get("library_doc_type") not in ("Purchase Order",):
        put("library_doc_type", "Purchase Order")
    put("order_number", new_on)
    D._recompute("po", f)
    drop = {"not_an_order", "missing_order_number"}
    old_flags = list(sc.get("flags") or [])
    keep = [x for x in old_flags if x not in drop and not HELD.match(str(x))]
    reason = f"review checks ({len(keep)})"
    if sc.get("hold_reason") != reason:
        changes.append({"field": "hold_reason", "old": sc.get("hold_reason"), "new": reason})
    sc["hold_reason"] = reason
    sc["flags"] = keep + ["held: " + reason]
    sc["needs_review"] = bool(keep)
    sc.setdefault("edits", []).append({"at": stamp, "by": f"ownform_fix (Dino 30 Oct: {why})", "changes": changes})
    tmp = js.with_name(js.name + ".of.tmp")
    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, js)
    return [x for x in old_flags if x in drop]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    rows, cnt = [], Counter()
    for t in ("PurchaseOrders", "PolymerOrders"):
        for js in sorted(glob.glob(str(ROOT / "out" / t / "hold" / "*.json"))):
            js = Path(js)
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except Exception:
                continue
            f = sc.get("fields") or {}
            if sc.get("doc_type") != "po" or f.get("checked") or Path(str(js) + ".uploaded").exists():
                continue
            on = str(f.get("order_number") or "").upper().replace(" ", "")
            cand = ((f.get("form") == "not_an_order" and re.fullmatch(r"A\d{5}|PL\d{4}", on))
                    or (f.get("form") in ("internal_group_transfer", "not_an_order") and not on
                        and re.search(r"IGT|INTERNAL GROUP", f"{sc.get('source_file')} {js.name} {f.get('po_doc_type')}".upper())))
            if not cand:
                continue
            if sc.get("merged") or str(sc.get("hold_reason") or "").startswith("scan of"):
                cnt["skipped: merged scan"] += 1
                continue
            pdf = js.parent / str(sc.get("file") or "")
            if not pdf.is_file():
                continue
            try:
                text = route_sort.text_of(pdf)
            except Exception:
                text = ""
            try:
                text2 = presort.page_text(pdf)[0]
            except Exception:
                text2 = ""
            j = judge(sc, text, text2)
            res = "left (page does not show the form)" if not j else "would fix" if not a.apply else "fixed"
            if j and a.apply:
                try:
                    with sidecar_lock.locked(js, "ownform_fix"):
                        sc = json.load(open(js, encoding="utf-8"))
                        if (sc.get("fields") or {}).get("checked"):
                            res = "skipped (checked meanwhile)"
                        else:
                            fix(js, sc, *j, stamp)
                except sidecar_lock.Locked:
                    res = "locked (left)"
            cnt[res] += 1
            rows.append({"type": t, "file": js.name, "source_file": sc.get("source_file"), "form_read": f.get("form"),
                         "po_doc_type": f.get("po_doc_type"), "order_read": on, "new_form": j[0] if j else "",
                         "new_order": j[1] if j else "", "evidence": j[2] if j else "", "result": res,
                         "flags": "; ".join(str(x) for x in (sc.get("flags") or []))[:300],
                         "title": title(text)[:160]})
    p = ROOT / "reports" / f"ownform fix {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)
    print(dict(cnt))
    for r in rows:
        print(f"  {r['result'][:12]:12} {r['type'][:7]} {r['form_read']:>23} {r['order_read'] or '-':7} -> {r['new_form'] or '':22} {r['new_order']:7} {r['file'][:60]} | {r['evidence'] or r['title'][:90]}")
    print("report:", p)


if __name__ == "__main__":
    main()

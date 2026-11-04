#!/usr/bin/env python3
"""497 (Dino 4 Nov: supplier and date trained too): numbers_eval.py also scores the supplier and the order date, where the
test order carries them (verified_header / main_header from numbers_data_sheet.py): "main" = the reader, "numbers" = the
model; combined / combined_desc keep the reader's header (the pass applies lines only). Supplier counts when it is
85 % alike after keeping letters and digits (po_daily's rule); date only on orders whose date is verified, exact.
    numbers_eval_header_patch.py <numbers_eval.py>"""
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
R = [
    ('''def parse(txt):''', '''def parse_header(txt):
    m = re.search(r"\\{.*\\}", txt, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
        return {"supplier": d.get("supplier"), "date": str(d.get("date") or "")[:10] or None} if isinstance(d, dict) else {}
    except ValueError:
        return {}


def same_supplier(a_, b_):
    x, y = norm(a_), norm(b_)
    return bool(x and y) and (x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= 0.85)


def parse(txt):'''),
    ('''tot = {k: {"qty": [0, 0], "price": [0, 0], "description": [0, 0]} for k in KINDS}''',
     '''tot = {k: {"qty": [0, 0], "price": [0, 0], "description": [0, 0], "supplier": [0, 0], "date": [0, 0]} for k in KINDS}'''),
    ('''    docs.append({"file": r["file"], "secs": round(time.time() - s, 1), "raw": raw, "numbers": nums, "main": main,''',
     '''    vh = r.get("verified_header")
    if vh:                                               # 497: supplier and date
        mh, nh = r.get("main_header") or {}, parse_header(raw)
        for k, got in (("main", mh), ("numbers", nh), ("combined", mh), ("combined_desc", mh)):
            if vh.get("supplier"):
                tot[k]["supplier"][1] += 1
                tot[k]["supplier"][0] += same_supplier(got.get("supplier"), vh["supplier"])
            if vh.get("date") and vh.get("date_verified"):
                tot[k]["date"][1] += 1
                tot[k]["date"][0] += str(got.get("date") or "")[:10] == vh["date"]
    docs.append({"file": r["file"], "secs": round(time.time() - s, 1), "raw": raw, "numbers": nums, "main": main,'''),
    ('''print(f"{'':14}{'line qty':>12}{'line price':>12}{'description':>13}")
for k in KINDS:
    print(f"{k:14}" + "".join(f"{res[k][f]['acc'] * 100:11.1f}%" for f in ("qty", "price")) + f"{res[k]['description']['acc'] * 100:12.1f}%")''',
     '''print(f"{'':14}{'line qty':>12}{'line price':>12}{'description':>13}{'supplier':>10}{'date':>8}")
pc = lambda v, w: f"{v * 100:{w - 1}.1f}%" if v is not None else f"{'-':>{w}}"
for k in KINDS:
    print(f"{k:14}" + pc(res[k]["qty"]["acc"], 12) + pc(res[k]["price"]["acc"], 12) + pc(res[k]["description"]["acc"], 13)
          + pc(res[k]["supplier"]["acc"], 10) + pc(res[k]["date"]["acc"], 8))'''),
]
for old, new in R:
    if s.count(old) != 1:
        sys.exit(f"expected 1 match, found {s.count(old)}: {old[:70]!r}")
    s = s.replace(old, new)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched", p)

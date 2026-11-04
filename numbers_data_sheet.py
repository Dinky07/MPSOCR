#!/usr/bin/env python3
"""469 (Dino 4 Nov: train Qwen2.5 and Qwen3 on the corrected training sheet and A/B test them). The numbers dataset
from training/po_lines/PO lines training <date>.xlsx (the newest, as Dino left it) instead of the checked orders:
an order trains when every one of its lines has Use = Y; its answer is the FINAL qty / price / description / per.
Same split as training.py (1 in 5 by content hash is a TEST order, never trained), so the live numbers model never saw
the test orders either. Test rows carry "verified" (FINAL) and "main" (what the reader read, from the sheet) - no
Ollama reads. Page images are reused from the old dataset (same name: hash[:16].png) or rendered at 1600 px.

    python3 numbers_data_sheet.py --out /srv/ml/numbers/data-sheet [--test-max N, 0 = all] [--sheet FILE]
"""
import argparse, glob, json, os, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import training as T                                     # noqa: E402  is_test(), render_png()
import numbers_data as ND                                # noqa: E402  PROMPT, answer()

ROOT = Path("/srv/ocr/scans/pipeline")


def yes(v):
    return str(v or "").strip().upper() in ("Y", "YES", "X", "1", "TRUE", "OK")


def num(v):
    try:
        return None if v in (None, "") else round(float(str(v).replace(",", "").replace("R", "")), 2)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/srv/ml/numbers/data-sheet")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--old-img", default="/srv/ml/numbers/data/img")
    ap.add_argument("--px", type=int, default=1600)
    ap.add_argument("--test-max", type=int, default=0, help="0 = every test order")
    a = ap.parse_args()
    import openpyxl
    stable = ROOT / "training" / "po_lines" / "PO lines training.xlsx"          # rebuilt nightly with Dino's changes kept
    sheet = Path(a.sheet) if a.sheet else stable if stable.exists() else \
        max(Path(p) for p in glob.glob(str(ROOT / "training" / "po_lines" / "PO lines training *.xlsx")))
    print("sheet:", sheet, flush=True)
    ws = openpyxl.load_workbook(sheet, read_only=True, data_only=True)["Lines"]
    it = ws.iter_rows(values_only=True)
    head = list(next(it))
    I = {k: head.index(k) for k in head if k}
    orders = defaultdict(list)
    for r in it:
        if not r or not r[I["Hash"]]:
            continue
        orders[str(r[I["Hash"]])].append(r)
    out = Path(a.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    train, tests, skipped = [], [], 0
    for h, rs in orders.items():
        rs.sort(key=lambda r: int(r[I["Line"]] or 0))
        if not all(yes(r[I["Use"]]) for r in rs):
            skipped += 1
            continue
        items = [{"description": r[I["Description FINAL"]], "quantity": num(r[I["Qty FINAL"]]),
                  "unit_price": num(r[I["Price FINAL"]]), "price_unit": r[I["Per sheet"]] if r[I["Per sheet"]] not in (None, "total") else r[I["Per read"]]}
                 for r in rs]
        main = [{"qty": num(r[I["Qty read"]]), "unit_price": num(r[I["Price read"]]), "description": r[I["Description read"]]}
                for r in rs if r[I["Status"]] != "sheet only"]
        e = {"hash": h, "pdf": str(ROOT / str(rs[0][I["Ref"]]).replace("\\", "/")), "items": items, "main": main,
             "desc_ok": [yes(r[I["Description verified"]]) for r in rs]}
        (tests if T.is_test(h) else train).append(e)
    tests = sorted(tests, key=lambda e: e["hash"])[: a.test_max or None]          # 0 = every test order (Dino 4 Nov)
    print(f"{len(orders)} orders in the sheet: {len(train)} train, {len(tests)} test, {skipped} not all lines Use = Y", flush=True)
    (out / "prompt.txt").write_text(ND.PROMPT, encoding="utf-8")
    for name, rows in (("train", train), ("test", tests)):
        n = made = 0
        with open(out / f"{name}.jsonl.tmp", "w", encoding="utf-8") as fh:
            for e in rows:
                img = out / "img" / f"{e['hash'][:16]}.png"
                old = Path(a.old_img) / img.name
                if not img.exists():
                    if old.exists():
                        os.link(old, img) if old.stat().st_dev == out.stat().st_dev else __import__("shutil").copy2(old, img)
                    else:
                        try:
                            T.render_png(e["pdf"], img, a.px)
                            made += 1
                        except Exception as ex:
                            print(f"  {Path(e['pdf']).name}: not rendered ({ex})", flush=True)
                            continue
                rec = {"image": str(img.relative_to(out)), "prompt": ND.PROMPT, "answer": ND.answer(e["items"]), "hash": e["hash"],
                       "file": Path(e["pdf"]).name}
                if name == "test":
                    rec["verified"] = [{"qty": i["quantity"], "unit_price": i["unit_price"], "description": i["description"]} for i in e["items"]]
                    rec["main"] = e["main"]
                    rec["description_verified"] = e["desc_ok"]
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        (out / f"{name}.jsonl.tmp").replace(out / f"{name}.jsonl")
        print(f"{name}: {n} orders ({made} pages rendered now)", flush=True)
    (out / "source.json").write_text(json.dumps({"sheet": str(sheet), "train": len(train), "test": len(tests)}), encoding="utf-8")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Ronin JT numbers model, step 1 (Dino 5 Nov 2026: train qty + stock code on the RoninStock capture): the dataset from
the Ronin JT lines training sheet (training/rjt_lines/Ronin JT lines training.xlsx) - a JT trains when every line has
Use = Y; its answer is the captured stock code and quantity per line, with the description as read. Split as the other
training: 1 in 5 by content hash is a TEST JT, never trained. Test rows carry "verified" (the capture) and "main" (the
current reader's lines, from the sheet). Page 1 at 1600 px.
    python3 rjt_numbers_data.py --out /srv/ml/rjt/data [--sheet FILE] [--test-max N]
"""
import argparse, glob, json, os, re, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import training as T                                     # noqa: E402  is_test(), render_png()

ROOT = Path("/srv/ocr/scans/pipeline")
PROMPT = ("This is a RONIN FLANGES AND FITTINGS job transfer form, filled in by hand. Read ONLY the table of items. For every "
          "row with handwriting, top to bottom: the quantity (QUANTITY column), the Ronin stock code (ITEM NO. column, copied "
          "character by character with no spaces; the type at the end is letters: SOFF, SORF, RFST, FFST, RFWN, WN, BL, SCRD, "
          "COMWN, CONR, ECCR, STD, XS, D, JIS) and the description exactly as written. When the ITEM NO. column holds no code "
          "(SPF, RONIN, a size, or nothing), build the code from the description the way Ronin's stock codes are built: four "
          "digits for the nominal bore (0080 = 80NB, 0250 = 250NB), then the class (10003 = 1000/3, 16003 = 1600/3, 25003 = "
          "2500/3, 40003 = 4000/3, 150A = ASA 150#, 300A = ASA 300#, 600A = ASA 600#, 643 = 64/3, TD = table D, 903D = 90 "
          "degree 3D bend, 9015D = 90 degree 1.5D bend), sometimes a schedule (10, 20, 40, 80, STD, XS, 160) and then the "
          "type (SOFF = slip-on flat face, SORF, RFST, FFST, RFWN, BL = blank, CONR = concentric reducer, ECCR, RTEE, STEE). "
          "A row without a quantity continues the row above. Do not total anything.\n"
          'Answer with JSON only: {"lines": [{"qty": number or null, "item_no": code or null, "item": text}]}')


def yes(v):
    return str(v or "").strip().upper() in ("Y", "YES", "X", "1", "TRUE", "OK")


def num(v):
    try:
        return None if v in (None, "") else round(float(str(v).replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def answer(lines):
    return json.dumps({"lines": [{"qty": l["qty"], "item_no": l["code"], "item": " ".join(str(l.get("desc") or "").split())} for l in lines]}, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/srv/ml/rjt/data")
    ap.add_argument("--sheet", default=str(ROOT / "training" / "rjt_lines" / "Ronin JT lines training.xlsx"))
    ap.add_argument("--px", type=int, default=1600)
    ap.add_argument("--test-max", type=int, default=0, help="0 = every test JT")
    a = ap.parse_args()
    import openpyxl
    ws = openpyxl.load_workbook(a.sheet, read_only=True, data_only=True)["Lines"]
    it = ws.iter_rows(values_only=True)
    head = list(next(it))
    I = {k: head.index(k) for k in head if k}
    jts = defaultdict(list)
    for r in it:
        if r and r[I["Hash"]]:
            jts[str(r[I["Hash"]])].append(r)
    out = Path(a.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    train, tests, skipped = [], [], 0
    for h, rs in jts.items():
        rs.sort(key=lambda r: int(r[I["Line"]] or 0))
        if not all(yes(r[I["Use"]]) for r in rs):
            skipped += 1
            continue
        fd, cw = I.get("Description FINAL"), I.get("Code written")           # 583: lines read by eye (status checked)
        lines = [{"qty": num(r[I["Qty FINAL"]]), "code": r[I["Code FINAL"]],
                  "desc": (r[fd] if fd is not None else None) or r[I["Description read"]] or r[I["Description captured"]]} for r in rs]
        main = [{"qty": num(r[I["Qty read"]]), "item_no": r[I["Code read"]], "item": r[I["Description read"]]} for r in rs
                if r[I["Status"]] not in ("sheet only", "checked, not read")]
        e = {"hash": h, "jt": rs[0][I["JT"]], "pdf": str(ROOT / str(rs[0][I["Ref"]]).replace("\\", "/")), "lines": lines, "main": main,
             "code_written": [(yes(r[cw]) if cw is not None and str(r[I["Status"]] or "").startswith("checked") else r[I["Status"]] != "no code written") for r in rs]}
        (tests if T.is_test(h) else train).append(e)
    tests = sorted(tests, key=lambda e: e["hash"])[: a.test_max or None]
    print(f"{len(jts)} JTs in the sheet: {len(train)} train, {len(tests)} test, {skipped} not all lines Use = Y", flush=True)
    (out / "prompt.txt").write_text(PROMPT, encoding="utf-8")
    for name, rows in (("train", train), ("test", tests)):
        n = made = 0
        with open(out / f"{name}.jsonl.tmp", "w", encoding="utf-8") as fh:
            for e in rows:
                img = out / "img" / f"{e['hash'][:16]}.png"
                if not img.exists():
                    try:
                        T.render_png(e["pdf"], img, a.px)
                        made += 1
                    except Exception as ex:
                        print(f"  {Path(e['pdf']).name}: not rendered ({ex})", flush=True)
                        continue
                rec = {"image": str(img.relative_to(out)), "prompt": PROMPT, "answer": answer(e["lines"]), "hash": e["hash"], "file": Path(e["pdf"]).name, "jt": e["jt"]}
                if name == "test":
                    rec["verified"] = [{"qty": l["qty"], "item_no": l["code"], "description": l["desc"]} for l in e["lines"]]
                    rec["main"] = e["main"]
                    rec["code_written"] = e["code_written"]
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        (out / f"{name}.jsonl.tmp").replace(out / f"{name}.jsonl")
        print(f"{name}: {n} JTs ({made} pages rendered now)", flush=True)
    (out / "source.json").write_text(json.dumps({"sheet": a.sheet, "train": len(train), "test": len(tests)}), encoding="utf-8")


if __name__ == "__main__":
    main()

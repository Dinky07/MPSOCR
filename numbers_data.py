#!/usr/bin/env python3
"""Numbers pass, step 1: the dataset. From the checked purchase orders (training.examples; checked values = the ordering
spreadsheet), page 1 at 1600 px with the line quantities and unit prices as the answer:
  <out>/train.jsonl  the training orders (not test orders)       {"image","prompt","answer","hash"}
  <out>/test.jsonl   the test orders (first --test-max, 150)    + "verified" lines and "main": what the current reader
                     (po_reader v12, qwen2.5vl, worked examples) reads on the same order - the baseline to beat
Runs with the system python (po_reader, Ollama for the baseline reads).

    python3 numbers_data.py --out /srv/ml/numbers/data [--no-main]
"""
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import training as T
import po_reader as P

PROMPT = ("This is a Mining Pressure Systems purchase order, filled in by hand in block capitals. Read ONLY the order "
          "lines (the table of items). For every line, top to bottom: the first few words of the item, the quantity and "
          "the unit price exactly as written, and what the price is per. The R in front of a price is the Rand sign, not "
          "a digit (\"R86-96 ea\" is 86.96). Her 4 looks like an L with an upward flick; her 1 is a single plain stroke. "
          "A quantity or price that is not written is null. Lines starting with a dash (- QUOTE:, - DISC:) are not order "
          "lines. Do not total anything.\n"
          'Answer with JSON only: {"lines": [{"item": text, "qty": number or null, "unit_price": number or null, '
          '"price_unit": "each|kg|litre|length|metre|lot" or null}]}')


def answer(items):
    return json.dumps({"lines": [{"item": " ".join(str(i.get("description") or "").split()[:4]), "qty": i.get("quantity"),
                                  "unit_price": i.get("unit_price"), "price_unit": i.get("price_unit")} for i in items]},
                      ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/srv/ml/numbers/data")
    ap.add_argument("--px", type=int, default=1600)
    ap.add_argument("--no-main", action="store_true", help="skip the baseline reads of the test orders")
    ap.add_argument("--test-max", type=int, default=150, help="test orders scored (all test orders stay out of training)")
    a = ap.parse_args()
    out = Path(a.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    cfg, t = T.load_cfg()
    pos = [e for e in T.po_examples(cfg) if e["verified"].get("items")]
    tests = sorted([e for e in pos if e["test"]], key=lambda e: e["hash"])[: a.test_max]
    train = [e for e in pos if not e["test"]]
    s = T.po_settings(cfg)
    P.JOBS_DIR = cfg["root"]
    P.EXTRA_VOCAB = str(T.tdir(cfg) / "suppliers_verified.json")
    P.PRICE_MEMORY = str(T.tdir(cfg) / "price_memory_train.json")       # never the test orders' own prices
    for name, rows in (("train", train), ("test", tests)):
        n = 0
        with open(out / f"{name}.jsonl", "w", encoding="utf-8") as fh:
            for e in rows:
                img = out / "img" / f"{e['hash'][:16]}.png"
                if not img.exists():
                    try:
                        T.render_png(e["pdf"], img, a.px)
                    except Exception as ex:
                        print(f"  {Path(e['pdf']).name}: not rendered ({ex})", flush=True)
                        continue
                items = [i for i in e["verified"]["items"] if isinstance(i, dict)]
                rec = {"image": str(img.relative_to(out)), "prompt": PROMPT, "answer": answer(items), "hash": e["hash"],
                       "file": Path(e["pdf"]).name}
                if name == "test":
                    rec["verified"] = [{"qty": i.get("quantity"), "unit_price": i.get("unit_price")} for i in items]
                    if not a.no_main:
                        try:
                            d, _, _ = P.read(e["pdf"], cfg["ollama_url"], s["model"], s["fewshot_dir"], 2, s["px"],
                                             int(cfg.get("ollama_timeout", 1200)), cfg.get("keep_alive", "15m"), **(e.get("source") or {}))
                            f, _ = P.to_fields(d)
                            rec["main"] = [{"qty": i.get("quantity"), "unit_price": i.get("unit_price")} for i in (f.get("items") or [])]
                        except Exception as ex:
                            rec["main"] = None
                            print(f"  main read failed for {rec['file']}: {ex}", flush=True)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        print(f"{name}: {n} orders", flush=True)


if __name__ == "__main__":
    main()

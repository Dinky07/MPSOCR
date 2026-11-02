#!/usr/bin/env python3
"""429 (Dino 2 Nov "Move the 108"): the proofs of payment on the Purchase Orders hold (dry run reports/pop on po hold
2026-11-02 dry run.csv, rows "move: ...") go to in/ProofOfPayments to be read by its reader, the same way as the
dashboard's Move to... (dashboard.reroute_doc, under the sidecar lock). Not recorded as dashboard moves (record_move: those
are Dino's own corrections, the pre-sort's labels); the notes say "pop_move 2 Nov" instead of "dashboard".
    pop_move.py [--apply]"""
import csv, json, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import dashboard as D                                   # noqa: E402
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
BY = "pop_move 2 Nov (Dino: move the 108 proofs of payment)"
apply = "--apply" in sys.argv
cfg = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))
D.record_move = lambda *a, **k: None
# --flagged: the 11 Dino also approved (named PROOF OF PAYMENT, or read as one by the reader - 10 of them in his labels)
flagged = "--flagged" in sys.argv
rows = [r for r in csv.DictReader(open(ROOT / "reports" / "pop on po hold 2026-11-02 dry run.csv", encoding="utf-8-sig"))
        if (r["dry_run"].startswith(("check", "leave: no sign")) if flagged else r["dry_run"].startswith("move"))]
hold = ROOT / "out" / "PurchaseOrders" / "hold"
cnt, out = Counter(), []
for r in rows:
    js = hold / r["document"]
    if not js.is_file():
        cnt["gone meanwhile"] += 1; out.append((r["document"], "gone meanwhile")); continue
    if not apply:
        cnt["would move"] += 1; continue
    try:
        with sidecar_lock.locked(js, "pop_move", wait=15):
            side = json.load(open(js, encoding="utf-8"))
            if (side.get("fields") or {}).get("checked"):
                cnt["skipped: checked meanwhile"] += 1; continue
            res = D.reroute_doc(cfg, ROOT, "PurchaseOrders", {"dest": "ProofOfPayments", "held": True, "name": js.name})
        m = ROOT / "in" / "ProofOfPayments" / "_meta" / (res["name"] + ".json")
        if m.exists():
            mm = json.load(open(m)); mm["routed_by"] = BY; mm["rerouted_from"] = mm["rerouted_from"].replace("(dashboard)", "(pop_move)")
            m.write_text(json.dumps(mm, indent=1), encoding="utf-8")
        for n in (ROOT / "deleted" / "PurchaseOrders").glob(f"*/{js.stem}.rerouted.json"):
            nn = json.load(open(n))
            if nn.get("to", "").endswith(res["name"]):
                nn["by"] = BY; n.write_text(json.dumps(nn), encoding="utf-8")
        cnt["moved"] += 1; out.append((r["document"], "in/ProofOfPayments/" + res["name"]))
    except sidecar_lock.Locked:
        cnt["skipped: locked"] += 1; out.append((r["document"], "locked"))
    except Exception as e:
        cnt["failed"] += 1; out.append((r["document"], f"failed: {str(e)[:120]}"))
print(dict(cnt), "of", len(rows))
for d, w in out[:5] + [x for x in out if not x[1].startswith("in/")][:10]:
    print("  ", d[:60], "->", w)
if apply:
    with open(ROOT / "reports" / f"pop on po hold 2026-11-02 moved{' flagged' if flagged else ''}.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh); w.writerow(["document", "result"]); w.writerows(out)

#!/usr/bin/env python3
"""Proofs of payment the Purchase Orders reader typed "Other" (file name says PROOF OF PAYMENT) become Document Type
"Proof of Payment" (Dino 26 Oct: "yes just mark them as proofs of payment"). An unknown supplier is taken from the scan
folder (the payee, as the reader does for proofs of payment). One on hold with an A-number moves to the queue (the PC
sync's proof-of-payment rule uploads those when asked). Writes reports/retype pop <date>.csv. Dry run unless --apply.

    python3 retype_pop.py [--apply]
"""
import argparse, csv, json, os, re, shutil
from datetime import datetime
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
root = Path(a.root)
stamp = datetime.now().isoformat(timespec="seconds")
rows = []
for where, pat in (("queue", "out/PurchaseOrders/*.json"), ("hold", "out/PurchaseOrders/hold/*.json")):
    for js in sorted(root.glob(pat)):
        try:
            sc = json.loads(js.read_text(encoding="utf-8"))
        except Exception:
            continue
        f = sc.setdefault("fields", {})
        if f.get("library_doc_type") == "Proof of Payment" or "PROOF OF PAYMENT" not in str(sc.get("source_file") or "").upper():
            continue
        on = str(f.get("order_number") or "").strip().upper()
        payee = str(sc.get("source_folder") or "").replace("/", "\\").rstrip("\\").split("\\")[-1].strip()
        sup = str(f.get("supplier") or "")
        new_sup = payee if payee and (not sup or sup.upper().startswith("UNKNOWN")) else sup
        row = {"where": where, "file": js.name, "source": sc.get("source_file"), "was": f.get("library_doc_type"),
               "order_number": on, "supplier": sup, "supplier_now": new_sup, "result": "retyped"}
        if a.apply:
            f["retyped"] = {"from": f.get("library_doc_type"), "to": "Proof of Payment", "how": "file name says PROOF OF PAYMENT", "at": stamp}
            f["library_doc_type"] = "Proof of Payment"
            f["po_doc_type"] = "proof_of_payment"
            if new_sup != sup:
                f["supplier"] = new_sup
                f["review"] = "; ".join(x for x in (f.get("review"), f"payee taken from the scan folder: {new_sup}") if x)
            tmp = js.with_name(js.name + ".rt.tmp")
            tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, js)
            pdf = js.with_name(str(sc.get("file") or ""))
            if where == "hold" and re.fullmatch(r"A\d{5}", on) and pdf.is_file():
                q = root / "out" / "PurchaseOrders"
                if not (q / js.name).exists() and not (q / pdf.name).exists():
                    shutil.move(str(pdf), str(q / pdf.name))
                    shutil.move(str(js), str(q / js.name))
                    row["result"] += "; moved from hold to the queue"
        rows.append(row)
        print(f"{where:5s} {on:7s} {new_sup[:30]:30s} {js.name}")
rep = root / "reports" / f"retype pop {'applied' if a.apply else 'dry run'} {datetime.now():%Y-%m-%d %H%M}.csv"
with open(rep, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["file"])
    w.writeheader()
    w.writerows(rows)
withA = sum(1 for r in rows if re.fullmatch(r"A\d{5}", r["order_number"]))
print(f"{len(rows)} proof(s) of payment {'retyped' if a.apply else 'to retype (dry run)'}: queue {sum(1 for r in rows if r['where'] == 'queue')}, "
      f"hold {sum(1 for r in rows if r['where'] == 'hold')}; with an A-number {withA}; report: {rep}")

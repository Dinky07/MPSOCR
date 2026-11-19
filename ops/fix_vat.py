#!/usr/bin/env python3
r"""
A supplier VAT number the reader got wrong, corrected everywhere on the box (Dino 19 Nov 2026: DSV South Africa was
read as 4260213673, the real number is 4260213873).

  - every supplier invoice sidecar (out/ and its hold, synced/, failed/, sync_failed/) whose fields.supplier_vat_no
    is the wrong number: set to the right one, under the sidecar lock, recorded in fields.text_fixes and edits the
    same way vat_fill.py does, so the dashboard shows it and the PC sync's -ReingestFixed re-sends synced ones
  - state.db: the wrong number's 'learned' supplier row is removed (a Sage 'csv' row is never touched), and the
    right number is learned for this supplier, so the CPU tier stops matching the misread

    python3 ops/fix_vat.py --wrong 4260213673 --right 4260213873 [--name "DSV South Africa"]           # dry run
    python3 ops/fix_vat.py --wrong 4260213673 --right 4260213873 [--name "DSV South Africa"] --apply

--name (optional) limits the sidecar fix to invoices whose supplier name contains it (case-insensitive), for a wrong
number that might also be another supplier's real one.
"""
import argparse, json, re, sqlite3, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
STATE_DB = "/srv/ocr/pipeline/state.db"
TYPES = ("SupplierInvoices", "SupplierInvoicesArchive")


def digits(v):
    return re.sub(r"\D", "", str(v or ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wrong", required=True)
    ap.add_argument("--right", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--state-db", default=STATE_DB)
    a = ap.parse_args()
    wrong, right = digits(a.wrong), digits(a.right)
    if not (re.fullmatch(r"4\d{9}", wrong) and re.fullmatch(r"4\d{9}", right)) or wrong == right:
        sys.exit("both numbers must be 10-digit SA VAT numbers starting with 4, and differ")
    root, stamp = Path(a.root), datetime.now().isoformat(timespec="seconds")
    how = f"confirmed by Dino ({wrong} was misread)"
    names, n = set(), 0
    for sub in ("out", "synced", "failed", "sync_failed"):
        for t in TYPES:
            d = root / sub / t
            if not d.is_dir():
                continue
            for js in sorted(d.rglob("*.json")):
                try:
                    sc = json.loads(js.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(sc, dict) or sc.get("doc_type") != "supplier_invoice":
                    continue
                f = sc.get("fields") or {}
                if digits(f.get("supplier_vat_no")) != wrong:
                    continue
                name = (f.get("supplier_name") or "").strip()
                if a.name and a.name.upper() not in name.upper():
                    print(f"skip (other supplier) {js.relative_to(root)}  {name}")
                    continue
                names.add(name)
                n += 1
                if not a.apply:
                    print(f"would fix {js.relative_to(root)}  {name}  invoice {f.get('invoice_number')}")
                    continue
                try:
                    with sidecar_lock.locked(js, "fix_vat"):
                        sc2 = json.load(open(js, encoding="utf-8"))
                        f2 = sc2.setdefault("fields", {})
                        old = f2.get("supplier_vat_no")
                        if digits(old) != wrong:
                            print(f"changed meanwhile, left alone: {js.relative_to(root)}")
                            continue
                        f2["supplier_vat_no"] = right
                        f2.setdefault("text_fixes", []).append({"field": "supplier_vat_no", "value": right, "how": how,
                                                                "by": "fix_vat", "at": stamp})
                        sc2.setdefault("edits", []).append({"at": stamp, "by": f"fix_vat ({how})",
                                                            "changes": [{"field": "supplier_vat_no", "old": old, "new": right}]})
                        tmp = js.with_name(js.name + ".fv.tmp")
                        tmp.write_text(json.dumps(sc2, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                        tmp.replace(js)
                    print(f"fixed {js.relative_to(root)}  {name}  invoice {f.get('invoice_number')}")
                except sidecar_lock.Locked:
                    print(f"LOCKED (being synced or edited), run again: {js.relative_to(root)}")
    print(f"{n} sidecar(s) {'fixed' if a.apply else 'to fix'}")

    db = sqlite3.connect(a.state_db, timeout=30)
    rows = db.execute("SELECT vat_no, name, source, seen FROM suppliers WHERE vat_no IN (?, ?)", (wrong, right)).fetchall()
    for r in rows:
        print("state.db now:", r)
    bad = [r for r in rows if r[0] == wrong]
    has_right = any(r[0] == right for r in rows)
    learn_name = (bad[0][1] if bad and bad[0][1] else None) or (sorted(names)[0] if names else None)
    if bad and bad[0][2] != "learned":
        print(f"{wrong} is a '{bad[0][2]}' row (from the Sage list), not removed: fix it in Sage")
    elif bad:
        print(f"{'removing' if a.apply else 'would remove'} learned {wrong}")
        if a.apply:
            db.execute("DELETE FROM suppliers WHERE vat_no=? AND source='learned'", (wrong,))
    if not has_right and learn_name:
        print(f"{'learning' if a.apply else 'would learn'} {right} = {learn_name}")
        if a.apply:
            db.execute("INSERT INTO suppliers VALUES(?,?,'learned',1,datetime('now'))", (right, learn_name))
    if a.apply:
        db.commit()
    db.close()
    if not a.apply:
        print("dry run: nothing changed. Add --apply to make these changes.")


if __name__ == "__main__":
    main()

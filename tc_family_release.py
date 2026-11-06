#!/usr/bin/env python3
"""531 (Dino 6 Nov): test certs on the Test Certs hold as "family Other" get their family again from their stored item
lines with the 531 reader rules (tc_reader.family_of_product / family_of_spec), and a part split from a combined cert
takes the combined cert's old-library family for a line that names none (not for plate, coil, bar ...). One that is now
Pipe / Flanges / Fittings / Mixed goes to out/TestCerts for the PC sync (the PDF first, the JSON last), unless another
hold rule still holds it. Nothing is read again (no GPU). Skipped: checked or edited by a person.
    tc_family_release.py [--apply]"""
import json, os, re, shutil, sys
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                          # noqa: E402
import tc_reader as T                                        # noqa: E402

R = Path("/srv/ocr/scans/pipeline")
OUT = R / "out/TestCerts"
HOLD = OUT / "hold"
apply = "--apply" in sys.argv
PFF = ("Pipe", "Flanges", "Fittings")
cnt = {}


def hint_of(f):
    rep = f.get("replaces") or {}
    try:
        return (json.load(open(R / str(rep["sidecar"]), encoding="utf-8")).get("fields") or {}).get("family") if rep.get("sidecar") else None
    except (OSError, ValueError, KeyError):
        return None


def family(f, hint):
    items = [i for i in f.get("items") or [] if isinstance(i, dict)]
    for i in items:
        txt = f"{i.get('product') or ''} {i.get('description') or ''}".upper()
        i["family"] = T.family_of_product(i.get("product"), i.get("description")) or (
            hint if hint in PFF and not T._NOT_PFF.search(txt) else None)
    fams = {i["family"] for i in items if i.get("family")}
    return (fams.pop() if len(fams) == 1 else "Mixed" if len(fams) > 1 else
            (items and T.family_of_spec(f.get("standard"), f.get("grade"), items=items)) or "Other")


for js in sorted(HOLD.glob("*.json")):
    try:
        sc = json.load(open(js, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if sc.get("doc_type") != "test_cert" or not str(sc.get("hold_reason") or "").startswith("family Other"):
        continue
    f = sc.get("fields") or {}
    if f.get("checked") or [e for e in sc.get("edits") or [] if "by" not in e]:
        cnt["skipped: checked / edited"] = cnt.get("skipped: checked / edited", 0) + 1
        continue
    hint = hint_of(f)
    fam = family(json.loads(json.dumps(f)), hint)
    flags = [x for x in sc.get("flags") or [] if not str(x).startswith("held") and x != "family_other"]
    why = ("family Other: not pipe, flanges or fittings" if fam not in PFF + ("Mixed",) else
           "no items read" if not f.get("item_count") else
           "no heats read" if "no_heats" in flags and not f.get("heat_count") else None)
    key = "released" if not why else f"still held: {why}"
    cnt[key] = cnt.get(key, 0) + 1
    print(f"  {js.stem[:60]:60} hint {hint} -> {fam}: {why or 'released'}")
    if not apply or why == "family Other: not pipe, flanges or fittings":
        continue
    with sidecar_lock.locked(js, "tc_family_release", wait=10):
        sc = json.load(open(js, encoding="utf-8"))
        f = sc["fields"]
        f["family"] = family(f, hint)
        sc["flags"] = flags + (["family_other"] if f["family"] == "Other" else []) + ([f"held: {why}"] if why else [])
        sc["family_fixed"] = f"531, 6 Nov: was Other; family from the item lines" + (f" / combined cert ({hint})" if hint else "")
        if why:
            sc["hold_reason"] = why
        else:
            sc.pop("hold_reason", None)
        pdf = HOLD / str(sc.get("file") or "")
        if not why and ((OUT / js.name).exists() or (OUT / pdf.name).exists() or not pdf.is_file()):
            cnt["not moved (name taken / PDF missing)"] = cnt.get("not moved (name taken / PDF missing)", 0) + 1
            continue
        tmp = js.with_name(js.name + ".tmp")
        tmp.write_text(json.dumps(sc, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, js)
        if not why:
            shutil.move(str(pdf), str(OUT / pdf.name))
            for extra in (js.name + ".attempts",):
                if (HOLD / extra).is_file():
                    shutil.move(str(HOLD / extra), str(OUT / extra))
            shutil.move(str(js), str(OUT / js.name))
print(json.dumps({"applied": apply, **cnt}))

#!/usr/bin/env python3
"""Incomplete test certificates (Dino 2 Nov: "a hold queue for incomplete test certs so we can rejoin them by evaluating
this queue from time to time"). A certificate scanned in parts reads as an incomplete document:
  header only   the cert number read, but no items and no heats (the body pages are another scan)
  body only     items / heats read, but no cert number (the header page is another scan)
  page X of Y   the page text says "page X of Y" with Y more than the file's pages
1. Hold: an incomplete cert in the Test Certs queue goes on hold as "incomplete certificate (<kind>)"; one already on hold
   gets that reason. A cert a person releases from that hold (it was complete after all) is never held again
   (work/tc_incomplete/held.csv). Checked or uploaded certs are left alone.
2. Rejoin: incomplete certs on hold that belong together - the same order number, or the same mill and date - and
   between them have a cert number and items (the same mill and date only counts within one file or scanning session),
   are joined into one PDF (in scan order, else header first) and sent to
   in/TestCerts to be read again as one certificate; the parts go to deleted/TestCerts/<yyyy-mm>/ (recoverable).
   A part whose partner is already in SharePoint is only reported (replacing an uploaded file is Dino's call).
DRY RUN unless --apply. -> reports/tc incomplete <stamp>[ dry run].csv   Timer: mps-tc-incomplete (every 15 min).

    python3 tc_incomplete.py [--apply] [--no-rejoin]
"""
import argparse, csv, glob, json, os, re, shutil, subprocess, sys, tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import dashboard as D                                   # noqa: E402  move_doc()
import route_sort                                       # noqa: E402  remove()
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
T = "TestCerts"
Q, H = ROOT / "out" / T, ROOT / "out" / T / "hold"
WORK = ROOT / "work" / "tc_incomplete"
LEDGER = WORK / "held.csv"
REASON = "incomplete certificate"
HELD_FLAG = re.compile(r"^held: ")


def text_of(pdf):
    try:
        return subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, timeout=60).stdout.upper()
    except Exception:
        return ""


def real_cert_no(v):
    """The cert number, or None when the reader put the inspection standard there (EN 10204 3.1B, 3.1 ...)"""
    s = re.sub(r"\s+", " ", str(v or "")).strip().upper()
    if not s or re.fullmatch(r"(BS ?)?(EN|N|DIN|ISO)? ?(10204|10474)\b.*|3\.[12] ?[AB]?|2\.[12]", s) or len(re.sub(r"[^A-Z0-9]", "", s)) < 4:
        return None
    return s


def kind(sc, pdf):
    """'header only' / 'body only' / 'page X of Y' / None"""
    f = sc.get("fields") or {}
    if sc.get("doc_type") != "test_cert" or f.get("checked"):
        return None
    items, heats, cert = f.get("items") or [], f.get("heats") or [], real_cert_no(f.get("cert_no"))
    if cert and not items and not heats:
        return "header only"
    if (items or heats) and not cert:
        return "body only"
    for a, b in re.findall(r"PAGE\s*:?\s*(\d{1,2})\s*(?:OF|/)\s*(\d{1,2})\b", text_of(pdf)):
        if int(b) <= 20 and int(b) > int(f.get("pages") or 0) and int(a) <= int(b):
            return f"page {a} of {b}"
    return None


def keys(sc):
    f = sc.get("fields") or {}
    out = set()
    for o in f.get("order_numbers") or []:
        v = re.sub(r"[^A-Z0-9]", "", str(o.get("value") if isinstance(o, dict) else o).upper())
        if len(v) >= 5:
            out.add("order:" + v)
    for k in ("mps_po", "ronin_po"):
        v = re.sub(r"[^A-Z0-9]", "", str(f.get(k) or "").upper())
        if len(v) >= 5:
            out.add("order:" + v)
    mill = re.sub(r"[^A-Z]", "", str(f.get("mill") or "").upper())[:12]
    if len(mill) >= 6 and f.get("date"):
        out.add(f"mill+date:{mill}|{f['date']}|{family(sc)}")   # the same mill and day alone links unrelated certs
    return out


def family(sc):
    """The file a part came from: a PDFsam split (12_PDFsam_DOC200217-014) or a page copy (_p2) of the same file, or a
    scanning session (2615_030928185130_001: the same first 8 digits of the stamp)"""
    s = Path(str(sc.get("source_file") or "")).stem.upper()
    s = re.sub(r"^\d+_PDFSAM_", "", s)
    s = re.sub(r"_P\d+$", "", s)
    m = re.match(r"\d{4}_(\d{8})\d{4}_\d{3}$", s)
    return "scan:" + m.group(1) if m else s


def scan_order(sc):
    m = re.match(r"(\d{4})_(\d{12})", str(sc.get("source_file") or ""))
    return (0, m.group(2), int(m.group(1))) if m else (1, str(sc.get("source_file") or ""), 0)


def hold_it(js, sc, why, stamp):
    with sidecar_lock.locked(js, "tc_incomplete"):
        sc = json.load(open(js, encoding="utf-8"))
        reason = f"{REASON} ({why})"
        old = sc.get("hold_reason")
        sc["hold_reason"] = reason
        sc["flags"] = [x for x in (sc.get("flags") or []) if not HELD_FLAG.match(str(x)) and not str(x).startswith(REASON)] + \
                      [f"{REASON}: {why} - waiting for the rest of the certificate", "held: " + reason]
        sc["needs_review"] = True
        sc.setdefault("edits", []).append({"at": stamp, "by": "tc_incomplete (Dino 2 Nov: incomplete test certs on hold)",
                                           "changes": [{"field": "hold_reason", "old": old, "new": reason}]})
        tmp = js.with_name(js.name + ".tci.tmp")
        tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, js)
        if js.parent == Q:
            D.move_doc(Q, H, js.name, json_last=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-rejoin", action="store_true")
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    held_before = set()
    if LEDGER.exists():
        held_before = {r["content_hash"] for r in csv.DictReader(open(LEDGER, encoding="utf-8"))}
    rows, cnt = [], Counter()
    newly = []
    docs = []                                           # (where, js, sc, kind)
    for where, d in (("queue", Q), ("hold", H)):
        for js in sorted(d.glob("*.json")):
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if Path(str(js) + ".uploaded").exists():
                continue
            k = kind(sc, js.with_name(str(sc.get("file") or "")))
            docs.append((where, js, sc, k))
    # 1. hold
    for where, js, sc, k in docs:
        if not k:
            continue
        h = str((sc.get("fields") or {}).get("content_hash") or js.name)
        already = str(sc.get("hold_reason") or "").startswith(REASON)
        if where == "queue" and h in held_before:
            res = "left: released by a person after being held"
        elif already:
            res = "on the incomplete hold"
        else:
            res = ("held" if a.apply else "would hold") + (" (was on hold: " + str(sc.get("hold_reason") or "").split(" (")[0] + ")" if where == "hold" else "")
            if a.apply:
                try:
                    hold_it(js, sc, k, stamp)
                    newly.append(h)
                except sidecar_lock.Locked:
                    res = "skipped: locked"
        cnt[res.split(" (")[0]] += 1
        rows.append({"document": js.name, "source_file": sc.get("source_file"), "where": where, "kind": k,
                     "cert_no": (sc.get("fields") or {}).get("cert_no"), "keys": "; ".join(sorted(keys(sc))), "partners": "", "result": res})
    if newly:
        WORK.mkdir(parents=True, exist_ok=True)
        new = not LEDGER.exists()
        with open(LEDGER, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["at", "content_hash"])
            w.writerows([[stamp, h] for h in newly])
    # 2. rejoin: the incomplete certs on hold (after this run's holds), grouped by shared keys
    if a.apply:                                         # as they are now: the ones held above have moved to hold/
        inc = []
        for js in sorted(H.glob("*.json")):
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(sc.get("hold_reason") or "").startswith(REASON) and not Path(str(js) + ".uploaded").exists():
                inc.append((js, sc, str(sc["hold_reason"])[len(REASON) + 2:-1]))
    else:
        inc = [(js, sc, k) for where, js, sc, k in docs if k and not (where == "queue" and str((sc.get("fields") or {}).get("content_hash") or js.name) in held_before)]
    byname = {js.name: (js, sc, k) for js, sc, k in inc}
    parent = {n: n for n in byname}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    bykey = defaultdict(list)
    for n, (js, sc, k) in byname.items():
        for key in keys(sc):
            bykey[key].append(n)
    for key, ns in bykey.items():
        for n in ns[1:]:
            parent[find(n)] = find(ns[0])
    groups = defaultdict(list)
    for n in byname:
        groups[find(n)].append(n)
    synced = defaultdict(list)                          # key -> synced cert files (report only)
    for p in glob.glob(str(ROOT / "synced" / T / "*" / "*.json")):
        try:
            sc = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in keys(sc):
            synced[key].append(Path(p).name)
    row_of = {r["document"]: r for r in rows}
    for g in groups.values():
        parts = [byname[n] for n in g]
        has_hdr = any(real_cert_no((sc.get("fields") or {}).get("cert_no")) for _, sc, _ in parts)
        has_body = any((sc.get("fields") or {}).get("items") or (sc.get("fields") or {}).get("heats") for _, sc, _ in parts)
        sp = sorted({s for _, sc, _ in parts for key in keys(sc) for s in synced.get(key, [])})
        names = [js.name for js, _, _ in parts]
        if len(parts) >= 2 and has_hdr and has_body:
            parts.sort(key=lambda p: (scan_order(p[1]), 0 if real_cert_no((p[1].get("fields") or {}).get("cert_no")) else 1))
            res = "would rejoin" if not a.apply or a.no_rejoin else "rejoined"
            if a.apply and not a.no_rejoin:
                res = rejoin(parts, stamp)
            for n in names:
                if n in row_of:
                    row_of[n]["partners"] = "; ".join(x for x in names if x != n)
                    row_of[n]["result"] += f" | {res}"
            cnt[res.split(":")[0]] += 1
        else:
            for n in names:
                if n in row_of:
                    row_of[n]["partners"] = ("in SharePoint: " + "; ".join(sp[:3])) if sp else ("; ".join(x for x in names if x != n) or "none yet")
            cnt["partner in SharePoint (report only)" if sp else "no partner yet"] += 1
    out = ROOT / "reports" / f"tc incomplete {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["document", "source_file", "where", "kind", "cert_no", "keys", "partners", "result"])
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"applied": a.apply, "results": dict(cnt.most_common()), "report": str(out)}, indent=1))


def rejoin(parts, stamp):
    """The parts' PDFs joined (pdfunite) into in/TestCerts/<first part>-rejoined.pdf with a _meta note; the parts to deleted/."""
    indir, md = ROOT / "in" / T, ROOT / "in" / T / "_meta"
    md.mkdir(parents=True, exist_ok=True)
    first = parts[0][1]
    stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(first.get("source_file") or parts[0][0].stem)).stem)[:100]
    name, k = f"{stem}-rejoined.pdf", 2
    while (indir / name).exists() or (md / (name + ".json")).exists():
        name, k = f"{stem}-rejoined_{k}.pdf", k + 1
    locks = []
    try:
        for js, _, _ in parts:
            lk = sidecar_lock.locked(js, "tc_incomplete rejoin")
            lk.__enter__()
            locks.append(lk)
        pdfs = [js.with_name(str(json.load(open(js, encoding="utf-8")).get("file") or "")) for js, _, _ in parts]
        if not all(p.is_file() for p in pdfs):
            return "skipped: a part's PDF is missing"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "joined.pdf"
            r = subprocess.run(["pdfunite"] + [str(p) for p in pdfs] + [str(tmp)], capture_output=True, text=True, timeout=300)
            if r.returncode or not tmp.exists():
                return f"failed: pdfunite {r.stderr[:100]}"
            note = {"original_name": name, "reread_of": [f"out/{T}/hold/{js.name}" for js, _, _ in parts], "reread_at": stamp,
                    "rejoined_from": [str(sc.get("source_file")) for _, sc, _ in parts],
                    "why": "incomplete test certificate parts joined into one (tc_incomplete, Dino 2 Nov)"}
            if first.get("source_folder"):
                note["source_folder"] = first["source_folder"]
            (md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
            shutil.copy2(tmp, indir / (name + ".part"))
        for js, sc, _ in parts:
            route_sort.remove(js, json.load(open(js, encoding="utf-8")), T,
                              f"incomplete test certificate rejoined with {len(parts) - 1} other part(s) as in/{T}/{name}", stamp)
        os.replace(indir / (name + ".part"), indir / name)
        return f"rejoined: in/{T}/{name}"
    except sidecar_lock.Locked:
        return "skipped: locked"
    finally:
        for lk in locks:
            lk.__exit__(None, None, None)


if __name__ == "__main__":
    main()

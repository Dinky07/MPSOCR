#!/usr/bin/env python3
"""Copies of one test certificate (Dino 5 Nov). The same mill cert is scanned once for every order it was bought on:
each copy carries that order's number written by hand at the top (Ronin "DF3009", sometimes MPS "A49797") and an
asterisk / tick / highlight on the line(s) bought on it. Only the most readable copy goes to SharePoint; the others
  - give their order number: on the cert (Ronin PO "DF3009; DF3010") and on the line they mark (Test Cert Items,
    MPSBusiness docs.TestCertItem.RoninPO); a copy whose marked line can't be told puts its order on the cert with a
    review check "DF3027: line not identified" (Dino 5 Nov "Yes, flag it"),
  - reinforce the reading: a line quantity two or more copies agree on, where the kept copy reads something else, is
    corrected (and noted); other disagreements become review checks,
  - leave the queue for copies/TestCerts/<yyyy-mm>/ (never synced), noted as copies of the kept one.

Copies are the same mill, date and heats with mostly the same line sizes. Handwriting is read by the vision model:
the order number on the page's top strip, the marked line on the whole page (fields.copy_marks, read once per copy).
Checked, uploaded or locked documents are left alone. A kept copy already in SharePoint is updated in synced/ and
listed in work/tc_copies/sp_refresh.txt for the PC sync.
    tc_copies.py [--apply] [--no-read]        (dry run unless --apply; --no-read: only copies whose marks are read)"""
import argparse, base64, csv, glob, json, os, re, shutil, sys, tempfile, time, urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                     # noqa: E402
import tc_reader as T                                   # noqa: E402

R = Path("/srv/ocr/scans/pipeline")
Q, H = R / "out" / "TestCerts", R / "out" / "TestCerts" / "hold"
COPIES, WORK = R / "copies" / "TestCerts", R / "work" / "tc_copies"
CFG = json.load(open("/srv/ocr/pipeline/config.json"))
MODEL = (CFG["doc_types"]["TestCerts"].get("tc_reader") or {}).get("model", "qwen3-vl:8b-instruct")

ASK_TOP = ('This is the top strip of a steel test certificate. Is there anything written BY HAND (pen) on it - usually an '
           'order number such as "DF3009" or "A49797" near the top right corner, outside the printed frame? Return JSON '
           '{"handwritten": [each handwritten number exactly as written]}; empty list if none. Ignore printed text.')
ASK_MARK = ('This is a steel test certificate. The buyer marks by hand, in pen, the item line(s) the copy is for: a small '
            'asterisk (*), tick or star written just left of the line, between the printed line number and the product '
            'description, or a highlighter stroke. Usually exactly one line is marked; unmarked lines have nothing there. '
            'Return JSON {"marked_lines": [{"line_no": the printed line number at the start of that line, "size": the size '
            'printed on that line (e.g. "100 NB")}]}; empty list if no line is marked.')
_FIX = str.maketrans({"C": "0", "O": "0", "Q": "0", "D": "0", "U": "0", "Z": "2", "I": "1", "L": "1", "T": "7", "S": "5", "B": "8", "G": "6"})


def ask(png, q):
    body = {"model": MODEL, "stream": False, "format": "json", "keep_alive": "15m",
            "options": {"temperature": 0, "num_ctx": 49152, "num_predict": 400},
            "messages": [{"role": "user", "content": q, "images": [base64.b64encode(open(png, "rb").read()).decode()]}]}
    req = urllib.request.Request(CFG["ollama_url"].rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return T.parse_answer(json.loads(urllib.request.urlopen(req, timeout=600).read())["message"]["content"]) or {}


def order_no(v):
    """A handwritten order number -> ("ronin", "DF3009") / ("mps", "A49797") / None; pen letters read as digits fixed."""
    s = re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
    m = re.fullmatch(r"[DUO]?F(\w{4,5})", s)
    if m and re.fullmatch(r"\d{4,5}", m.group(1).translate(_FIX)):
        return "ronin", "DF" + m.group(1).translate(_FIX)
    m = re.fullmatch(r"A(\w{5})", s)
    if m and re.fullmatch(r"\d{5}", m.group(1).translate(_FIX)):
        return "mps", "A" + m.group(1).translate(_FIX)
    return None


def read_marks(pdf, pages):
    """The copy's handwritten order numbers and marked lines (pages: the cert's own page numbers in pdf)."""
    from PIL import Image
    out = {"handwritten": [], "lines": [], "read_at": datetime.now().isoformat(timespec="seconds"), "model": MODEL}
    for p in pages:
        with tempfile.TemporaryDirectory() as tmp:
            png = T.render(str(pdf), p, 3000, os.path.join(tmp, "p.png"))
            T.upright(png)
            with Image.open(png) as im:
                im = im.convert("RGB")
                im.crop((0, 0, im.width, int(im.height * 0.16))).save(os.path.join(tmp, "top.png"))
                f = 2400 / max(im.size)
                im.resize((round(im.width * f), round(im.height * f)), Image.LANCZOS).save(os.path.join(tmp, "page.png"))
            for v in ask(os.path.join(tmp, "top.png"), ASK_TOP).get("handwritten") or []:
                if str(v).strip() and str(v).strip() not in out["handwritten"]:
                    out["handwritten"].append(str(v).strip())
            for m in ask(os.path.join(tmp, "page.png"), ASK_MARK).get("marked_lines") or []:
                if isinstance(m, dict):
                    out["lines"].append({"page": p, "line_no": m.get("line_no"), "size": m.get("size")})
    return out


def cert_pages(sc):
    pg = [q["page"] for q in sc.get("tc_pages") or [] if q.get("page") and (q.get("answer") or {}).get("page_type") in T.CERT_TYPES]
    return pg or [1]


def mill_key(f):
    s = re.sub(r"\b(CO|COMPANY|LTD|LIMITED|LLC|INC|GROUP|PTY)\b", " ", str(f.get("mill") or "").upper())
    return re.sub(r"[^A-Z]", "", s)[:14]


def heats_of(f):
    hs = [T.heat_key(x) for x in re.split(r";\s*", str(f.get("heats_text") or ""))]
    hs += [T.heat_key(i.get("heat_no")) for i in f.get("items") or [] if isinstance(i, dict)]
    return frozenset(h for h in hs if h)


def size_key(i):
    nb = T.norm_nb(i.get("size_nb") or i.get("size_text"))
    return f"{nb}" if nb else re.sub(r"[^A-Z0-9/]", "", str(i.get("size_text") or "").upper())[:20]


def sizes_of(f):
    return Counter(size_key(i) for i in f.get("items") or [] if isinstance(i, dict) and size_key(i))


def same_cert(a, b):
    if not a["mill"] or a["mill"] != b["mill"] or not a["date"] or a["date"] != b["date"] or not a["heats"] or a["heats"] != b["heats"]:
        return False
    ca, cb = a.get("cert"), b.get("cert")
    if ca and cb and ca != cb:
        return False
    sa, sb = a["sizes"], b["sizes"]                     # line sizes counted: one heat often makes several certs
    both, either = sum((sa & sb).values()), sum((sa | sb).values())
    return bool(either) and both / either >= 0.6


def score(f):
    items = [i for i in f.get("items") or [] if isinstance(i, dict)]
    chem = sum(1 for h in f.get("heats") or [] if isinstance(h, dict) and (h.get("chem") or {}))
    return (3 * chem + sum(1 for i in items if i.get("qty") not in (None, "") and size_key(i))
            - 2 * len(f.get("checks") or []) + (5 if str(f.get("legibility") or "").lower() == "good" else 0))


def docs():
    out = []
    for where, pat in (("queue", Q / "*.json"), ("hold", H / "*.json"), ("synced", R / "synced" / "TestCerts" / "*" / "*.json")):
        for p in glob.glob(str(pat)):
            js = Path(p)
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if sc.get("doc_type") != "test_cert":
                continue
            f = sc.get("fields") or {}
            out.append({"where": where, "js": js, "sc": sc, "mill": mill_key(f), "date": f.get("date"), "heats": heats_of(f),
                        "sizes": sizes_of(f), "cert": T._cert_key(f.get("cert_no")) if hasattr(T, "_cert_key") else None,
                        "uploaded": Path(str(js) + ".uploaded").exists(), "checked": bool(f.get("checked"))})
    return out


def groups(ds):
    by = defaultdict(list)
    for i, d in enumerate(ds):
        if d["mill"] and d["date"] and d["heats"]:
            by[(d["mill"], d["date"], d["heats"])].append(i)
    parent = list(range(len(ds)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for idx in by.values():
        for a in idx:
            for b in idx:
                if a < b and same_cert(ds[a], ds[b]):
                    parent[find(a)] = find(b)
    g = defaultdict(list)
    for i in range(len(ds)):
        g[find(i)].append(ds[i])
    return [m for m in g.values() if len(m) > 1 and any(d["where"] != "synced" for d in m)]


def link_lines(kept_items, marks):
    """Marked lines -> indexes of the kept copy's items: by size, then by the printed line number."""
    hits = []
    for m in marks.get("lines") or []:
        nb = T.norm_nb(m.get("size"))
        same = [k for k, i in enumerate(kept_items) if nb and size_key(i) == str(nb)]
        try:
            ln = int(str(m.get("line_no")).strip())
        except (TypeError, ValueError):
            ln = None
        if len(same) == 1:
            hits.append(same[0])
        elif same and ln and ln - 1 in same:
            hits.append(ln - 1)
        elif not same and ln and 0 < ln <= len(kept_items) and not nb:
            hits.append(ln - 1)
    return sorted(set(hits))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-read", action="store_true")
    a = ap.parse_args()
    stamp = datetime.now().isoformat(timespec="seconds")
    ds = docs()
    gs = groups(ds)
    rows, cnt, refresh = [], Counter(), []
    for g in gs:
        g = [d for d in g if not d["checked"] or d["where"] == "synced"]
        live = [d for d in g if d["where"] != "synced"]
        if len(g) < 2 or not live:
            continue
        for d in g:                                             # handwriting, read once per copy
            f = d["sc"].setdefault("fields", {})
            cache = WORK / "marks" / f"{str(f.get('content_hash') or d['js'].stem)[:64]}.json"
            if "copy_marks" not in f and cache.exists():
                f["copy_marks"] = json.load(open(cache, encoding="utf-8"))
            if "copy_marks" not in f and d["where"] != "synced" and not a.no_read:
                try:
                    f["copy_marks"] = read_marks(d["js"].with_name(str(d["sc"]["file"])), cert_pages(d["sc"]))
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(f["copy_marks"]), encoding="utf-8")   # a dry run's reading is kept too
                except Exception as e:
                    print(f"  marks not read for {d['js'].name}: {e}", flush=True)
        synced = [d for d in g if d["where"] == "synced"]
        kept = synced[0] if synced else max(live, key=lambda d: (score(d["sc"]["fields"]), -len(str(d["js"]))))
        kf = kept["sc"]["fields"]
        items = [i for i in kf.get("items") or [] if isinstance(i, dict)]
        checks = [c for c in (kf.get("checks") or []) if not str(c).startswith("copies:")]
        orders = defaultdict(set)                               # item index -> order numbers
        ronin, mps, copies = [], [], []
        for d in [kept] + [x for x in g if x is not kept]:
            f = d["sc"]["fields"]
            marks = f.get("copy_marks") or {}
            nos = [n for n in (order_no(v) for v in marks.get("handwritten") or []) if n]
            for kind, n in nos:
                (ronin if kind == "ronin" else mps).append(n)
            lines = link_lines(items, marks)
            for kind, n in nos:
                for k in lines:
                    orders[k].add(n)
            if nos and not lines and len(g) > 1:
                checks.append(f"copies: {', '.join(n for _, n in nos)}: line not identified")
            if (marks.get("lines") or []) and not nos:
                checks.append(f"copies: a copy ({d['sc'].get('file')}) marks a line but its order number was not read")
            copies.append({"file": d["sc"].get("file"), "content_hash": f.get("content_hash"), "source_file": d["sc"].get("source_file"),
                           "source_page": d["sc"].get("source_page"), "where": d["where"], "kept": d is kept,
                           "orders": [n for _, n in nos], "handwritten": marks.get("handwritten") or [],
                           "marked_lines": [items[k].get("#") or k + 1 for k in lines], "score": score(f)})
        # quantities: two or more copies agreeing against the kept one win
        for k, it in enumerate(items):
            votes = Counter()
            for d in g:
                if d is kept:
                    continue
                for j in d["sc"]["fields"].get("items") or []:
                    if isinstance(j, dict) and size_key(j) == size_key(it) and j.get("qty") not in (None, ""):
                        votes[str(j.get("qty"))] += 1
            if votes:
                v, n = votes.most_common(1)[0]
                if n >= 2 and str(it.get("qty")) != v:
                    checks.append(f"copies: line {k + 1} quantity {it.get('qty')} read as {v} on {n} other copies - corrected")
                    it["qty_read"], it["qty"] = it.get("qty"), T._num(v) if T._num(v) is not None else v
                elif n >= 1 and str(it.get("qty")) != v and not any(str(j.get("qty")) == str(it.get("qty")) for d in g if d is not kept
                                                                  for j in d["sc"]["fields"].get("items") or [] if isinstance(j, dict) and size_key(j) == size_key(it)):
                    checks.append(f"copies: line {k + 1} quantity {it.get('qty')}, another copy reads {v}")
        for k, it in enumerate(items):
            if orders.get(k):
                it["ronin_po"] = "; ".join(sorted(n for n in orders[k] if n.startswith("DF"))) or None
                it["mps_po"] = "; ".join(sorted(n for n in orders[k] if n.startswith("A"))) or None
        res = f"kept {kept['js'].name} ({kept['where']}), {len(g) - 1} cop{'y' if len(g) == 2 else 'ies'}"
        if a.apply:
            try:
                with sidecar_lock.locked(kept["js"], "tc_copies", wait=10):
                    sc = json.load(open(kept["js"], encoding="utf-8"))
                    f = sc["fields"]
                    old = {k: f.get(k) for k in ("ronin_po", "mps_po", "review")}
                    f["items"] = items
                    T.recompute(f)
                    f["ronin_po"] = "; ".join(dict.fromkeys(ronin + [x for x in re.split(r"[;,\s]+", str(old["ronin_po"] or "")) if x])) or None
                    if mps:
                        f["mps_po"] = "; ".join(dict.fromkeys([x for x in re.split(r"[;,\s]+", str(old["mps_po"] or "")) if x] + mps)) or None
                    f["copies"] = copies
                    f["copy_marks"] = kf.get("copy_marks")
                    f["checks"] = list(dict.fromkeys(checks))
                    f["review"] = "; ".join(f["checks"]) or None
                    sc["flags"] = [x for x in (sc.get("flags") or []) if not str(x).startswith("check: copies:")] + \
                                  ["check: " + c for c in f["checks"] if c.startswith("copies:")]
                    sc["needs_review"] = bool(sc["flags"])
                    sc.setdefault("edits", []).append({"at": stamp, "by": "tc_copies (Dino 5 Nov: copies of one test cert)",
                                                       "changes": [{"field": k, "old": old[k], "new": f.get(k)} for k in old if old[k] != f.get(k)]
                                                       + [{"field": "copies", "old": None, "new": len(copies) - 1}]})
                    tmp = kept["js"].with_name(kept["js"].name + ".tcc.tmp")
                    tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                    os.replace(tmp, kept["js"])
                if kept["where"] == "synced":
                    refresh.append(str(kept["js"].relative_to(R)))
                for d in g:
                    if d is kept or d["where"] == "synced" or d["uploaded"]:
                        continue
                    with sidecar_lock.locked(d["js"], "tc_copies", wait=10):
                        sc = json.load(open(d["js"], encoding="utf-8"))
                        if (sc.get("fields") or {}).get("checked"):
                            continue
                        sc["fields"]["copy_marks"] = d["sc"]["fields"].get("copy_marks")
                        sc["copy_of"] = {"file": kept["sc"].get("file"), "content_hash": kf.get("content_hash"), "where": kept["where"], "at": stamp}
                        dest = COPIES / datetime.now().strftime("%Y-%m")
                        dest.mkdir(parents=True, exist_ok=True)
                        pdf = d["js"].with_name(str(sc["file"]))
                        if pdf.is_file():
                            shutil.move(str(pdf), str(dest / pdf.name))
                        (dest / d["js"].name).write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                        d["js"].unlink()
                cnt["kept"] += 1
                cnt["copies moved"] += sum(1 for d in g if d is not kept and d["where"] != "synced" and not d["uploaded"])
            except sidecar_lock.Locked:
                res = "skipped: locked"
        else:
            cnt["would keep"] += 1
            cnt["copies"] += len(g) - 1
        rows.append({"kept": kept["js"].name, "where": kept["where"], "copies": " | ".join(d["js"].name for d in g if d is not kept),
                     "ronin_po": "; ".join(dict.fromkeys(ronin)), "mps_po": "; ".join(dict.fromkeys(mps)),
                     "lines": "; ".join(f"{items[k].get('#') or k + 1} {size_key(items[k])}: {', '.join(sorted(v))}" for k, v in sorted(orders.items())),
                     "checks": "; ".join(c for c in dict.fromkeys(checks) if c.startswith("copies:")), "result": res})
    if a.apply and refresh:
        WORK.mkdir(parents=True, exist_ok=True)
        with open(WORK / "sp_refresh.txt", "a", encoding="utf-8") as fh:
            fh.write("".join(x + "\n" for x in refresh))
    out = R / "reports" / f"tc copies {datetime.now():%Y-%m-%d %H%M}{'' if a.apply else ' dry run'}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["kept", "where", "copies", "ronin_po", "mps_po", "lines", "checks", "result"])
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"applied": a.apply, "groups": len(rows), "results": dict(cnt), "report": str(out)}, indent=1))
    for r in rows:
        print(f"  {r['kept'][:46]:46} +{len(r['copies'].split(' | '))} | {r['ronin_po'] or '-':24} | {r['lines'][:60]} | {r['checks'][:90]}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
r"""518 (Dino 5 Nov: "take another pass at the files in the test cert hold queue ... all files there are test certs but
they show 'not a test cert' or 'no items'"). 37 held: 14 no items, 10 header only, 7 no heats, 2 no test cert page,
4 other. Looked at page by page:
  - sideways scans Tesseract's orientation check was unsure of, read sideways as an empty "inspection_only" page
    (0261, 4101, 2208, 4581, DF2741, Sterling ...);
  - an item in a "Dimensions" block, not a table row (AMSA tubes: DOC041022-0002 -006 -009), read with no items;
  - "INSPECTION CERTIFICATE" titles with chemistry answered as inspection_only with the header only (MTC 0319 ...);
  - a bank's "DOCUMENTARY CREDIT NUMBER" stamp on a cert made the text rule file it as a letter of credit
    (DOC090720-003, DOC010818, DOC210518).
489 fixed these by changing how every page is read (crop, size, orientation) and lowered cert no. / date on the
controls. This one leaves every read as it is and adds a second look for a file that came back with no item lines or
no heats: its empty pages (and, when no page is a certificate, its letter-of-credit pages) are read once more -
turned the way Tesseract reads clearly more words, at 2400 px, with a note on where items and heats hide. The second
answer is kept only when it has more items + heats, keeping the first answer's header fields it lacks. A file that
read fine is never read again, so the other certs' readings cannot change.
    tc_rescue_patch.py <tc_reader.py>"""
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
R = []

R.append(('''def read_page(path, page, ollama_url, model, fewshot_dir, family=None, px=1600, hint=None, timeout=1200,
              keep_alive="15m", num_ctx=49152, earlier_heats=None, extra=""):   # 49152 = the pipeline readers' value: no model reload
    with tempfile.TemporaryDirectory() as tmp:
        if str(path).lower().endswith(".pdf"):
            png = render(path, page, px, os.path.join(tmp, "p.png"))
        else:
            png = shutil.copy(str(path), os.path.join(tmp, "p" + os.path.splitext(str(path))[1]))
        turned = upright(png)                                 # 453: sideways scans read upright
''', '''def _good_words(png, deg):
    """518: words Tesseract reads with confidence >= 70 (3+ letters) on the page image turned by deg."""
    from PIL import Image
    q = png[:-4] + f"_o{deg}.png"
    with Image.open(png) as im:
        g = im.convert("L")
        g.thumbnail((1400, 1400))
        (g.rotate(-deg, expand=True) if deg else g).save(q)
    out = run(["tesseract", q, "stdout", "--psm", "6", "tsv"], 120).stdout
    out = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")
    n = 0
    for line in out.splitlines()[1:]:
        c = line.split("\\t")
        try:
            if len(c) >= 12 and float(c[10]) >= 70 and re.fullmatch(r"[A-Za-z]{3,}", c[11].strip()):
                n += 1
        except ValueError:
            pass
    return n


def best_turn(path, page):
    """518: the way up with clearly the most well-read words: (degrees, scores). degrees is None when no way is
    clearly best (the reader then keeps its own orientation check)."""
    with tempfile.TemporaryDirectory() as tmp:
        png = render(path, page, 1400, os.path.join(tmp, "w.png"))
        score = {d: _good_words(png, d) for d in (0, 90, 270, 180)}
    best = max(score, key=score.get)
    rest = max(v for d, v in score.items() if d != best)
    return (best if score[best] >= max(12, 1.5 * rest) else None), score


def read_page(path, page, ollama_url, model, fewshot_dir, family=None, px=1600, hint=None, timeout=1200,
              keep_alive="15m", num_ctx=49152, earlier_heats=None, extra="", turn=None):   # 49152 = the pipeline readers' value: no model reload
    with tempfile.TemporaryDirectory() as tmp:
        if str(path).lower().endswith(".pdf"):
            png = render(path, page, px, os.path.join(tmp, "p.png"))
        else:
            png = shutil.copy(str(path), os.path.join(tmp, "p" + os.path.splitext(str(path))[1]))
        if turn is None:
            turned = upright(png)                             # 453: sideways scans read upright
        else:                                                 # 518: the way best_turn found
            turned = turn
            if turn:
                from PIL import Image
                with Image.open(png) as im:
                    im.rotate(-turn, expand=True).save(png)
'''))

R.append(('''def _has_content(a):''', '''RESCUE_ASK = ('This page is a test certificate, and your first answer for it had no item lines or no heat numbers. Read '
              'it again carefully. The items may be in a product or dimensions block rather than a table (outside '
              'diameter x wall thickness, length, quantity / pieces, weight), or in a table that has the chemistry '
              'columns (C, Si, Mn, P, S ...) on the same row. A page titled "INSPECTION CERTIFICATE" or "CERTIFICATE OF '
              'INSPECTION" that has chemistry is an mtc. Return every item line with its size, quantity and heat number '
              '("Heat No.", "Cast No.", "Charge No."), and every heat with its chemistry and mechanical results.')
RESCUE_PX = 2400
_HEADER = ("cert_no", "material_no", "cert_type", "date_as_written", "date_iso", "mill", "supplier", "customer",
           "order_numbers", "standard")


def _found(a):
    a = a if isinstance(a, dict) else {}
    return len(a.get("items") or []) + len(a.get("heats") or [])


def rescue(path, pages, ollama_url, model, family=None, timeout=1200, keep_alive="15m", log=None):
    """518: a second look at a file that read with no item lines or no heats (see tc_rescue_patch.py). Returns the
    number of pages read again."""
    if not str(path).lower().endswith(".pdf"):
        return 0
    got = [q for q in pages if q.get("page") and isinstance(q.get("answer"), dict)]
    cert = [q["answer"] for q in got if q["answer"].get("page_type") in CERT_TYPES]
    if cert and sum(len(a.get("items") or []) for a in cert) and sum(len(a.get("heats") or []) for a in cert):
        return 0
    n = 0
    for q in got:
        a = q["answer"]
        if a.get("page_type") in CERT_TYPES:
            if a.get("items") and a.get("heats"):
                continue
        elif not (q.get("how") == "text_rules" and a.get("page_type") == "letter_of_credit" and not cert):
            continue
        n += 1
        try:
            deg, score = best_turn(path, q["page"])
            text = page_text(path, q["page"])
            hint = text if len(text.strip()) > 80 else None
            a2 = read_page(path, q["page"], ollama_url, model, "", family, RESCUE_PX, hint, timeout, keep_alive,
                           extra=RESCUE_ASK, turn=deg)
            way = "osd" if deg is None else deg
            if isinstance(a2, dict) and a2.get("page_type") in CERT_TYPES:
                if a.get("page_type") in CERT_TYPES and (a.get("items") or a.get("heats")):
                    # 528: the first reading had items or heats: keep it, take only what it lacked (518's test: a
                    # whole second reading found the heats but changed a right pipe size)
                    a2 = dict(a, items=a.get("items") or a2.get("items") or [], heats=a.get("heats") or a2.get("heats") or [])
                else:
                    for k in _HEADER:
                        if not a2.get(k) and a.get(k):
                            a2[k] = a[k]
            if isinstance(a2, dict) and a2.get("page_type") in CERT_TYPES and _found(a2) > _found(a):
                q["answer"], q["how"] = a2, q["how"] + f"+rescue (turn {way}, {RESCUE_PX} px)"
            else:
                q["how"] += f"+rescue tried (turn {way}: no more found)"
        except Exception as e:
            q["how"] += f"+rescue failed ({str(e)[:120]})"
        if log:
            log(f"  p{q['page']}: {q['how']} {q['answer'].get('page_type')}")
    return n


def _has_content(a):'''))

R.append(('''    if n > max_pages:
        pages.append({"page": None, "how": "skipped", "answer": {"page_type": "not_read"}, "error": f"pages {max_pages + 1}-{n} not read"})
    return pages, n''', '''    rescue(path, pages, ollama_url, model, family, timeout, keep_alive, log)    # 518: a file with no items / heats
    if n > max_pages:
        pages.append({"page": None, "how": "skipped", "answer": {"page_type": "not_read"}, "error": f"pages {max_pages + 1}-{n} not read"})
    return pages, n'''))

for old, new in R:
    k = s.count(old)
    if k != 1:
        sys.exit(f"expected 1 match, found {k}: {old[:80]!r}")
    s = s.replace(old, new)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched", p)

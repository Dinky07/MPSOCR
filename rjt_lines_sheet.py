#!/usr/bin/env python3
"""Ronin JT training sheet (Dino 5 Nov 2026): every Ronin job transfer the reader has read (RoninJobTransfers: queue,
hold, synced) against its capture in RoninStock - the sales order whose OrderNum is the DF number (export
ronin-jt-export.psv, or later the MPSBusiness mirror). One row per line: stock code and quantity read vs captured,
FINAL = the captured values where the line is the same item. Read-only on the documents.
  training/rjt_lines/Ronin JT lines training.xlsx   Use / FINAL columns, the PDF one click away
  training/rjt_lines/rjt_lines_<date>.json          for the training jobs (Use rows only)
Status per line: match | corrected (the capture differs the way a misreading does: O/0, S/5, I/1, B/8, Z/2, G/6, one
or two characters; a qty digit) | check (differs more) | read only / sheet only (a line on one side only).
    python rjt_lines_sheet.py --root O:/pipeline --export FILE.psv [--out DIR]
"""
import argparse, csv, difflib, glob, json, os, re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

LOOK = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6", "Q": "0", "D": "0"})


def norm_code(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def looks(v):
    """The code with look-alike letters turned into the digits they are confused with."""
    return norm_code(v).translate(LOOK)


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def num(v):
    try:
        return None if v in (None, "") else round(float(re.sub(r"[^\d.]", "", str(v)) or "x"), 2)
    except ValueError:
        return None


def code_score(r, c):
    a, b = norm_code(r), norm_code(c)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if looks(a) == looks(b):
        return 0.95
    d = lev(looks(a), looks(b))
    return max(0.0, 1 - d / max(len(a), len(b)))


def align(ri, gi):
    n, m = len(ri), len(gi)
    S = [[code_score(ri[i].get("item_no"), gi[j]["code"]) + (0.1 if num(ri[i].get("quantity")) == gi[j]["qty"] else 0) for j in range(m)] for i in range(n)]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            take = best[i + 1][j + 1] + (S[i][j] if S[i][j] >= 0.5 else -1)
            best[i][j] = max(take, best[i + 1][j], best[i][j + 1])
    out, i, j = [], 0, 0
    while i < n and j < m:
        if S[i][j] >= 0.5 and best[i][j] == best[i + 1][j + 1] + S[i][j]:
            out.append((ri[i], gi[j])); i += 1; j += 1
        elif best[i][j] == best[i + 1][j]:
            out.append((ri[i], None)); i += 1
        else:
            out.append((None, gi[j])); j += 1
    out += [(ri[k], None) for k in range(i, n)] + [(None, gi[k]) for k in range(j, m)]
    if n == m and sum(1 for x, y in out if x and y) < n / 2:
        return list(zip(ri, gi))
    return out


def judge(x, y):
    """-> status, differs, notes, final code, final qty"""
    if x is None:
        return "sheet only", "line not read", "", y["code"], y["qty"]
    if y is None:
        return "read only", "not captured", "", None, None
    cr, cc = norm_code(x.get("item_no")), y["code"]
    qr, qc = num(x.get("quantity")), y["qty"]
    diffs, notes = [], []
    if len(re.sub(r"\D", "", cr)) < 7 or len(cr) < 9:
        # the Item No. column holds "SPF", "RONIN", "11", "200N3" or nothing (the May-July 2026 forms): the capturer built the
        # code from the description, so only the quantity can be checked against the page
        st = "no code written" if qr == qc else "check"
        return st, "code" + (", qty" if qr != qc else ""), f"Item No. column reads '{x.get('item_no') or ''}': the code comes from the description", cc, qc
    if cr != cc:
        diffs.append("code")
    if qr != qc:
        diffs.append("qty")
    if not diffs:
        return "match", "", "", cc, qc
    if "code" in diffs:
        lr, lc = looks(cr), looks(cc)
        if not cr:
            notes.append("code not read")
        elif lr == lc:
            notes.append("look-alike letters/digits")
        elif lc.startswith(lr) or (len(lr) >= 8 and lc.startswith(lr[:-1])):
            notes.append("written without its type (" + cc[len(lr) - (0 if lc.startswith(lr) else 1):] + "): the capture completes it")
        elif lev(lr, lc) > 3:
            notes.append(f"code {lev(lr, lc)} characters off")
    if "qty" in diffs:
        if qr is None:
            notes.append("qty not read")
        elif qc and (qr / qc > 20 or qc / qr > 20) or lev(str(int(qr)), str(int(qc))) > 2:
            notes.append(f"qty {qr:g} read, {qc:g} captured")
    st = "check" if any(n.startswith(("code ", "qty ")) and "characters off" in n or "captured" in n for n in notes) else "corrected"
    return st, ", ".join(diffs), "; ".join(notes), cc, qc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/srv/ocr/scans/pipeline")
    ap.add_argument("--export", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--link-root", default="O:\\pipeline")
    a = ap.parse_args()
    root = Path(a.root)
    out = Path(a.out) if a.out else root / "training" / "rjt_lines"
    out.mkdir(parents=True, exist_ok=True)
    cap = defaultdict(list)
    with open(a.export, encoding="utf-8", errors="replace") as fh:
        for r in csv.reader(fh, delimiter="|"):
            if len(r) < 14 or r[0] == "AutoIndex" or not r[2].upper().startswith("DF"):
                continue
            cap[r[2].upper().replace(" ", "")].append({"doc": r[0], "state": r[1], "date": r[6], "line": int(r[10] or 0), "code": norm_code(r[11]),
                                                        "qty": num(r[12]), "desc": r[13], "project": r[9]})
    for k in cap:
        cap[k].sort(key=lambda l: l["line"])
    print(f"{len(cap)} captured JTs, {sum(len(v) for v in cap.values())} lines", flush=True)
    rows, orders, cnt, seen = [], [], Counter(), set()
    for where, pat in (("queue", "out/RoninJobTransfers/*.json"), ("hold", "out/RoninJobTransfers/hold/*.json"), ("synced", "synced/RoninJobTransfers/*/*.json")):
        for js in sorted(glob.glob(str(root / pat))):
            if js.endswith((".deleted.json", ".rerouted.json")):
                continue
            try:
                sc = json.load(open(js, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            f = sc.get("fields") or {}
            jt = str(f.get("jt_number") or "").upper().replace(" ", "")
            if not re.fullmatch(r"DF\d{5}", jt):
                cnt["no DF number read"] += 1
                continue
            h = f.get("content_hash") or sc.get("source_sha256") or js
            if h in seen:
                continue
            seen.add(h)
            cnt["JTs read"] += 1
            gi = cap.get(jt)
            if not gi:
                cnt["not captured (yet)"] += 1
                continue
            rd = sc.get("fields_read") or f
            ri = [i for i in (rd.get("items") or []) if isinstance(i, dict)]
            cnt["JTs compared"] += 1
            pdf = Path(js).with_name(str(sc.get("file") or ""))
            rel = str(pdf.relative_to(root)).replace("/", "\\")
            o_rows = []
            for k, (x, y) in enumerate(align(ri, gi), 1):
                st, what, note, fc, fq = judge(x, y)
                use = "Y" if st in ("match", "corrected", "no code written") else ""   # Dino 5 Nov: the code is built from the description too
                x, y = x or {}, y or {}
                row = {"use": use, "status": st, "differs": what, "notes": note, "jt": jt, "date": f.get("date"), "captured_date": gi[0]["date"],
                       "job": f.get("job_number"), "line": k, "lines_read": len(ri), "lines_captured": len(gi),
                       "code_read": x.get("item_no"), "code_captured": y.get("code"), "code_final": fc,
                       "qty_read": num(x.get("quantity")), "qty_captured": y.get("qty"), "qty_final": fq,
                       "desc_read": x.get("description"), "desc_captured": y.get("desc"), "where": where, "doc_state": y.get("state"),
                       "pdf": rel, "hash": h}
                rows.append(row); o_rows.append(row)
                cnt["line: " + st] += 1
            orders.append({"jt": jt, "hash": h, "pdf": rel, "sidecar": str(Path(js).relative_to(root)).replace("\\", "/"),
                           "all_lines_usable": all(r["use"] == "Y" for r in o_rows), "date": gi[0]["date"],
                           "lines": [{"item_no": r["code_final"], "quantity": r["qty_final"], "description": r["desc_read"], "use": r["use"] == "Y", "status": r["status"]} for r in o_rows]})
            cnt["JTs all lines usable"] += orders[-1]["all_lines_usable"]
    stamp = datetime.now().strftime("%Y-%m-%d")
    (out / f"rjt_lines_{stamp}.json").write_text(json.dumps({"made": datetime.now().isoformat(timespec="seconds"), "counts": cnt, "orders": orders}, ensure_ascii=False, default=str), encoding="utf-8")
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Lines"
    cols = [("Use", "use", 6), ("Status", "status", 11), ("What differs", "differs", 12), ("Notes", "notes", 30), ("JT", "jt", 9), ("PDF", None, 6),
            ("Date read", "date", 11), ("Date captured", "captured_date", 11), ("Job", "job", 9), ("Line", "line", 5),
            ("Code read", "code_read", 17), ("Code captured", "code_captured", 17), ("Code FINAL", "code_final", 17),
            ("Qty read", "qty_read", 8), ("Qty captured", "qty_captured", 8), ("Qty FINAL", "qty_final", 8),
            ("Description read", "desc_read", 36), ("Description captured", "desc_captured", 36),
            ("Lines read", "lines_read", 6), ("Lines captured", "lines_captured", 6), ("Where", "where", 7), ("Sage state", "doc_state", 6), ("Ref", "pdf", 50), ("Hash", "hash", 12)]
    ws.append([c[0] for c in cols])
    fill = {"corrected": PatternFill("solid", fgColor="E8F4E8"), "check": PatternFill("solid", fgColor="FFF2CC"),
            "read only": PatternFill("solid", fgColor="EEEEEE"), "sheet only": PatternFill("solid", fgColor="EEEEEE"),
            "no code written": PatternFill("solid", fgColor="E4DFEC")}
    final = PatternFill("solid", fgColor="DDEBF7")
    for c in ws[1]:
        c.font = Font(bold=True); c.alignment = Alignment(wrap_text=True, vertical="top")
    rank = {"check": 0, "corrected": 1, "sheet only": 2, "read only": 3, "no code written": 4, "match": 5}
    rows.sort(key=lambda r: (rank[r["status"]], r["jt"], r["line"]))
    for r in rows:
        ws.append([("PDF" if k is None else r[k]) for _, k, _ in cols])
        rr = ws.max_row
        link = ws.cell(rr, 6); link.hyperlink = a.link_root.rstrip("\\") + "\\" + r["pdf"]; link.font = Font(color="0563C1", underline="single")
        if r["status"] in fill:
            ws.cell(rr, 2).fill = fill[r["status"]]
        for ci in (13, 16):
            ws.cell(rr, ci).fill = final
    for i, (_, _, w) in enumerate(cols, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = "F2"; ws.auto_filter.ref = ws.dimensions
    dv = DataValidation(type="list", formula1='"Y,N"', allow_blank=True); ws.add_data_validation(dv); dv.add(f"A2:A{ws.max_row}")
    s2 = wb.create_sheet("Summary"); s2.append(["What", "Count"])
    for k, v in cnt.items():
        s2.append([k, v])
    s2.column_dimensions["A"].width = 40
    s3 = wb.create_sheet("How")
    for line in ["One row per JT line: the stock code and quantity the reader read, what was captured in RoninStock for that DF number, and FINAL (blue).",
                 "FINAL = the captured values. Use = Y goes into training. corrected = differs the way a misreading does (O/0, S/5, I/1 and the like); check = differs more: set Y or N.",
                 "read only / sheet only: a line on one side only. The description stays as read (Sage's is the stock item's name, not what is written).",
                 "no code written: the Item No. column holds no stock code (SPF, RONIN, a size), so the captured code was built from the description; the quantity agrees. Use = Y teaches the reader to build the code the same way."]:
        s3.append([line])
    s3.column_dimensions["A"].width = 140
    xp = out / "Ronin JT lines training.xlsx"; tmp = out / (".~" + xp.name)
    wb.save(tmp); os.replace(tmp, xp)
    print(json.dumps(cnt, indent=1)); print("sheet:", xp)


if __name__ == "__main__":
    main()

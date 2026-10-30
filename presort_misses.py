#!/usr/bin/env python3
"""Weekly pre-sort misses report (Dino 30 Oct, 342): the documents Dino moved on the dashboard (Move to...,
work/sorter/moves/moves.jsonl, 341) grouped by from -> to queue, each page put through today's presort.verdict() again.
A group the pre-sort still does not send to the queue Dino chose, with >= --min such pages, is marked WORTH TUNING:
enough repeats for a rule. Rules only change through a tested runner, never from this report.

    python3 presort_misses.py [--min 5]

Words: what the group's pages have in common (>= 60 % of them) and the test set mostly lacks (<= 15 % of its pages), so
the shared letterhead / form wording a rule could key on. Page texts are cached in work/sorter/moves/text_cache.json.
-> reports/presort misses <yyyy-mm-dd>.xlsx (Summary, Pages) and .json
"""
import argparse, json, re, subprocess, sys, tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import presort                                          # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
MV = ROOT / "work" / "sorter" / "moves"
TS = ROOT / "work" / "sorter" / "testset" / "pdf"
CACHE = MV / "text_cache.json"
PC_ROOT = "O:\\pipeline"
STOP = {"THE", "AND", "FOR", "WITH", "FROM", "THIS", "THAT", "ARE", "YOUR", "PAGE", "DATE", "TOTAL", "PLEASE", "WILL",
        "HAVE", "BEEN", "NOT", "ANY", "ALL", "OUR", "YOU", "SHALL", "SUCH", "WHICH", "OTHER", "ONLY"}


def page1(pdf, fn):
    with tempfile.TemporaryDirectory() as td:
        p1 = Path(td) / "p1.pdf"
        subprocess.run(["pdfseparate", "-f", "1", "-l", "1", str(pdf), str(p1)], capture_output=True)
        return fn(p1 if p1.exists() else pdf)


def words(text):
    return {w for w in re.findall(r"\b[A-Z][A-Z&'-]{3,}\b", (text or "").upper()) if w not in STOP}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=5, help="pages the pre-sort still misses before a group is worth tuning")
    a = ap.parse_args()
    last = {}
    mj = MV / "moves.jsonl"
    if mj.exists():
        for line in open(mj, encoding="utf-8"):
            try:
                m = json.loads(line)
                last[m["id"]] = m
            except (ValueError, KeyError):
                continue
    moves = [m for m in last.values() if (MV / m["pdf"]).exists()]
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        cache = {}

    def key(p):
        s = p.stat()
        return f"{p.name}|{s.st_size}|{int(s.st_mtime)}"

    def text_of(p):
        k = key(p)
        if k not in cache:
            cache[k] = page1(p, lambda q: presort.page_text(q)[0])
        return cache[k]

    def judge(m):
        pdf = MV / m["pdf"]
        v = page1(pdf, lambda q: presort.verdict(q, m.get("original_file"), m.get("from")))
        text = v.pop("_page_text", "") or ""
        cache[key(pdf)] = text
        return m, v, text

    with ThreadPoolExecutor(max_workers=4) as ex:
        judged = list(ex.map(judge, moves))
        bg_pdfs = sorted(TS.glob("*.pdf"))
        bg = Counter()
        for t in ex.map(text_of, bg_pdfs):
            bg.update(words(t))
    json.dump(cache, open(CACHE, "w", encoding="utf-8"))
    n_bg = max(len(bg_pdfs), 1)

    week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    groups = defaultdict(list)
    for m, v, text in judged:
        groups[(m.get("from") or "?", m.get("to") or "?")].append((m, v, text))
    can_reach = set(presort.KINDS.values())
    summary, pages = [], []
    for (frm, to), items in groups.items():
        right = [x for x in items if x[1].get("dest") == to and x[1].get("strength") in ("sure", "likely")]
        missed = [x for x in items if x not in right]
        said = Counter(f"{v['kind']}:{v['strength']}" for _, v, _ in missed)
        wc = Counter()
        for _, _, t in missed:
            wc.update(words(t))
        need = max(2, -(-len(missed) * 6 // 10))
        common = [w for w, c in wc.most_common() if c >= need and bg[w] / n_bg <= 0.15][:12]
        flags = Counter(f for m, _, _ in missed for f in (m.get("flags") or []) if not f.startswith("check:"))
        cats = Counter(m.get("category") for m, _, _ in missed if m.get("category"))
        if to not in can_reach:
            note = f"the pre-sort has no kind for {to} yet: a rule here means a new kind"
        else:
            note = ""
        worth = len(missed) >= a.min
        summary.append({
            "From": frm, "To": to, "Moves": len(items), "New this week": sum(1 for m, _, _ in items if (m.get("at") or "") >= week_ago),
            "Pre-sort now right": len(right), "Still missed": len(missed),
            "Status": ("WORTH TUNING" if worth else f"{len(missed)} of {a.min}") if missed else "handled",
            "Pre-sort says": ", ".join(f"{k} x{c}" for k, c in said.most_common(4)),
            "Common words": ", ".join(common), "Reader flags": ", ".join(f"{k} x{c}" for k, c in flags.most_common(3)),
            "Categories": ", ".join(f"{k} x{c}" for k, c in cats.most_common(4)), "Note": note,
            "Samples": "; ".join(m.get("original_file") or m["id"] for m, _, _ in missed[:5]),
        })
        for m, v, _ in sorted(items, key=lambda x: x[0].get("at") or "", reverse=True):
            pages.append({
                "From": frm, "To": to, "Category": m.get("category") or "", "Original file": m.get("original_file") or "",
                "Source folder": m.get("source_folder") or "", "Moved": (m.get("at") or "").replace("T", " "),
                "Pre-sort now": f"{v['kind']}:{v['strength']}", "Missed": "no" if v.get("dest") == to and v.get("strength") != "unsure" else "yes",
                "Signals": " | ".join(v.get("signals") or []), "Read as": m.get("read_as") or "",
                "Copy": PC_ROOT + "\\work\\sorter\\moves\\" + m["pdf"].replace("/", "\\"),
            })
    summary.sort(key=lambda s: (s["Status"] != "WORTH TUNING", -s["Still missed"], -s["Moves"]))
    stamp = f"{datetime.now():%Y-%m-%d}"
    out = {"at": datetime.now().isoformat(timespec="seconds"), "moves": len(moves), "test_set_pages": len(bg_pdfs),
           "min": a.min, "worth_tuning": [f"{s['From']} -> {s['To']}" for s in summary if s["Status"] == "WORTH TUNING"],
           "groups": summary, "pages": pages}
    json.dump(out, open(ROOT / "reports" / f"presort misses {stamp}.json", "w"), indent=1)

    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    head = (f"{len(moves)} dashboard moves; a group is worth tuning when the pre-sort still misses {a.min} or more of its pages. "
            + (f"Worth tuning: {', '.join(out['worth_tuning'])}." if out["worth_tuning"] else "Nothing worth tuning yet."))
    ws.append([head])
    ws["A1"].font = Font(bold=True)
    for name, rows, sheet in (("Summary", summary, ws), ("Pages", pages, wb.create_sheet("Pages"))):
        cols = list(rows[0].keys()) if rows else ["From", "To"]
        sheet.append(cols)
        hr = sheet.max_row
        for c in sheet[hr]:
            c.font = Font(bold=True)
        for r in rows:
            sheet.append([r[c] for c in cols])
            if name == "Summary" and r["Status"] == "WORTH TUNING":
                for c in sheet[sheet.max_row]:
                    c.fill = PatternFill("solid", fgColor="FFE699")
            if name == "Pages":
                cell = sheet.cell(sheet.max_row, cols.index("Copy") + 1)
                cell.hyperlink = "file:///" + r["Copy"].replace("\\", "/")
                cell.font = Font(color="0563C1", underline="single")
        for i, c in enumerate(cols, 1):
            w = max([len(str(c))] + [len(str(r[c])) for r in rows[:200]])
            sheet.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(max(w + 2, 8), 60)
        sheet.freeze_panes = sheet.cell(hr + 1, 1)
    p = ROOT / "reports" / f"presort misses {stamp}.xlsx"
    wb.save(p)
    print(head)
    for s in summary:
        print(f"  {s['From']} -> {s['To']}: {s['Moves']} moves, {s['Still missed']} still missed [{s['Status']}] {s['Common words']}")
    print("report:", p)


if __name__ == "__main__":
    main()

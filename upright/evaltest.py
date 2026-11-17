#!/usr/bin/env python3
"""mps-upright accuracy test on real MPS pages (17 Nov 2026). Runs inside the container; changes no scan.

Input  /work/eval/sample.json : [{"type", "pdf" (/scans/...), "page", "osd_rotate", "osd_conf"}]  (Tesseract's check, done on the host)
Output /work/eval/results.json, /work/eval/summary.txt, /work/eval/sheet-*.jpg (pages as they would be saved, to check by eye)
"""
import collections, json, os, sys, time
import pypdfium2 as pdfium
from PIL import Image, ImageDraw
import server as S

W = "/work/eval"
S.ENGINE = S.load()
print("device", S.STATE.get("device"), "providers", S.STATE.get("providers_available"), "gpu_error", S.STATE.get("gpu_error"), flush=True)
sample = json.load(open(f"{W}/sample.json"))
TURNS = (0, 90, 180, 270)
rows, t0 = [], time.time()
for i, x in enumerate(sample):
    try:
        pdf = pdfium.PdfDocument(x["pdf"])
        im = S.render(pdf, x["page"], 768)
        pdf.close()
        got = S.ask([im if t == 0 else im.rotate(-t, expand=True) for t in TURNS])
        x["labels"], x["scores"] = [g[0] for g in got], [round(g[1], 4) for g in got]
        rows.append(x)
    except Exception as e:
        x["error"] = repr(e)[:160]
        rows.append(x)
    if i % 100 == 0:
        print(i, len(sample), round(time.time() - t0, 1), flush=True)
secs = time.time() - t0
ok = [r for r in rows if "labels" in r]

def vote(r, sign):
    implied = [(lab - sign * t) % 360 for lab, t in zip(r["labels"], TURNS)]
    best, n = collections.Counter(implied).most_common(1)[0]
    return best, n

# which way round do the labels count? the right sign makes the four answers agree
cons = {s: sum(vote(r, s)[1] >= 3 for r in ok) for s in (1, -1)}
sign = 1 if cons[1] >= cons[-1] else -1
for r in ok:
    best, n = vote(r, sign)
    r["label"], r["votes"], r["confident"] = best, n, n >= 3
    r["turn"] = (-sign * best) % 360                       # degrees clockwise to stand upright
    r["single_turn"] = (-sign * r["labels"][0]) % 360      # what one look at the page as it is would say
    sure = r.get("osd_conf") is not None and r["osd_conf"] >= 5
    r["osd_sure"] = bool(sure)
    r["agrees_osd"] = (r["turn"] == r["osd_rotate"]) if sure else None

L = []
L.append(f"pages {len(rows)}, read {len(ok)}, errors {len(rows) - len(ok)}; {secs:.1f} s = {1000 * secs / max(1, len(ok)):.0f} ms a page (render + 4 looks); device {S.STATE.get('device')}")
L.append(f"label direction: sign {sign:+d} (four-way agreement with +1: {cons[1]}, with -1: {cons[-1]} of {len(ok)})")
L.append("")
L.append(f"{'type':22}{'pages':>6}{'confident':>10}{'4 of 4':>8}{'turn 0':>8}{'90':>5}{'180':>5}{'270':>5}{'OSD sure':>10}{'agree':>7}{'differ':>7}{'one-look = vote':>17}")
tot = collections.Counter()
for t in sorted({r["type"] for r in ok}):
    rs = [r for r in ok if r["type"] == t]
    c = collections.Counter(r["turn"] for r in rs if r["confident"])
    sure = [r for r in rs if r["osd_sure"]]
    L.append(f"{t:22}{len(rs):>6}{sum(r['confident'] for r in rs):>10}{sum(r['votes'] == 4 for r in rs):>8}{c[0]:>8}{c[90]:>5}{c[180]:>5}{c[270]:>5}"
             f"{len(sure):>10}{sum(r['agrees_osd'] is True for r in sure):>7}{sum(r['agrees_osd'] is False for r in sure):>7}{sum(r['single_turn'] == r['turn'] for r in rs):>17}")
L.append("")
L.append("turned pages are tested too: every page was looked at in all four directions, so '4 of 4' = right in every direction, if the vote is right")
single = sum(((-sign * lab) % 360) == ((r["turn"] + 0) % 360 if t == 0 else ((r["turn"] - t) % 360)) for r in ok for lab, t in zip(r["labels"], TURNS))
L.append(f"single looks agreeing with the vote: {single} of {4 * len(ok)} = {100 * single / max(1, 4 * len(ok)):.2f} %")
L.append("")
L.append("differs from a sure Tesseract check:")
for r in ok:
    if r["agrees_osd"] is False:
        L.append(f"  {r['type']:18} p{r['page']:<3} ours {r['turn']:>3} ({r['votes']}/4) OSD {r['osd_rotate']:>3} conf {r['osd_conf']:<5} {os.path.basename(r['pdf'])[:70]}")
L.append("not confident (fewer than 3 of 4 agree):")
for r in ok:
    if not r["confident"]:
        L.append(f"  {r['type']:18} p{r['page']:<3} labels {r['labels']} scores {r['scores']} OSD {r.get('osd_rotate')} conf {r.get('osd_conf')} {os.path.basename(r['pdf'])[:60]}")
open(f"{W}/summary.txt", "w").write("\n".join(L) + "\n")
json.dump({"sign": sign, "seconds": secs, "rows": rows}, open(f"{W}/results.json", "w"), indent=0)
print("\n".join(L), flush=True)

# contact sheets: each page as it would be saved (turned by our answer). Order: differs from OSD, not confident, turned, then the rest.
def key(r):
    return (0 if r["agrees_osd"] is False else 1 if not r["confident"] else 2 if r["turn"] else 3 if not r["osd_sure"] else 4, r["type"])
order = sorted(ok, key=key)
pick = [r for r in order if key(r)[0] <= 2][:160]
rest = [r for r in order if key(r)[0] == 3]
step = max(1, len(rest) // 80)
pick += rest[::step][:80]
rest4 = [r for r in order if key(r)[0] == 4]
pick += rest4[::max(1, len(rest4) // 40)][:40]
TW, TH, COLS, ROWS = 300, 400, 6, 4
for s in range(0, len(pick), COLS * ROWS):
    sheet = Image.new("RGB", (COLS * TW, ROWS * (TH + 34)), "white")
    d = ImageDraw.Draw(sheet)
    for k, r in enumerate(pick[s:s + COLS * ROWS]):
        try:
            pdf = pdfium.PdfDocument(r["pdf"]); im = S.render(pdf, r["page"], 400); pdf.close()
            if r["turn"]:
                im = im.rotate(-r["turn"], expand=True)
            im.thumbnail((TW - 8, TH - 4))
            x, y = (k % COLS) * TW, (k // COLS) * (TH + 34)
            sheet.paste(im, (x + 4, y + 34))
            tag = "DIFFERS" if r["agrees_osd"] is False else "UNSURE" if not r["confident"] else ""
            d.text((x + 4, y + 2), f"#{s + k} {r['type'][:16]} p{r['page']}", fill="black")
            d.text((x + 4, y + 16), f"turned {r['turn']} ({r['votes']}/4) osd {r.get('osd_rotate')}/{r.get('osd_conf')} {tag}", fill="red" if tag else "blue")
            r["sheet"] = s + k
        except Exception as e:
            pass
    sheet.save(f"{W}/sheet-{s // (COLS * ROWS):02d}.jpg", quality=80)
json.dump([{k: r.get(k) for k in ("sheet", "type", "pdf", "page", "turn", "votes", "osd_rotate", "osd_conf")} for r in pick], open(f"{W}/sheet-index.json", "w"), indent=0)
print("sheets", (len(pick) + COLS * ROWS - 1) // (COLS * ROWS), "pages on sheets", len(pick), flush=True)

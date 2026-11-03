#!/usr/bin/env python3
"""489 test: the fixed test-cert reader (/tmp/tc489/tc_reader.py) on
  1. the 25 test certs (controls), against the live reader's score (453 test, 3 Nov 10:37; temperature 0), and
  2. every test cert on the Test Certs hold, against its stored reading (the 35 with no heats are what 489 is for).
PASS: overall not lower, no control field lower by more than 0.01, and the held certs not worse on heats / items /
mill / old-site heats."""
import importlib.util, json, sys, time
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import tc_training as TT                                # noqa: E402

BASE = {"overall": 0.9947, "cert_no": 1.0, "date": 1.0, "family": 1.0, "grade": 1.0, "heat_old_site": 0.9412, "line_heat": 1.0,
        "line_qty": 1.0, "line_size_nb": 1.0, "lines_count": 1.0, "mill": 1.0, "mps_po": 1.0}     # 453, 3 Nov 10:37
spec = importlib.util.spec_from_file_location("tc_reader_new", "/tmp/tc489/tc_reader.py")
NEW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NEW)
TT.tc_reader = NEW
cfg, t = TT.load_cfg()
s = TT.settings(cfg)
few = s["fewshot_dir"] or None
allc = TT.certs(cfg)
tests = TT.test_certs(t, allc)
print(f"{len(tests)} test certs; model {s['model']}", flush=True)


def run(es):
    pairs = []
    for e in es:
        t0 = time.time()
        try:
            f = TT.read_cert(cfg, e, s, few)
        except Exception as ex:
            print(f"  read failed {e['file']}: {ex}", flush=True)
            f = {}
        pairs.append((f, e))
        print(f"  read {Path(str(e['pdf'])).name[:50]} in {time.time() - t0:.0f} s", flush=True)
    return pairs


sn = TT.score(run(tests))
print("BASE", BASE, flush=True)
print("NEW ", sn["overall"], {k: v["acc"] for k, v in sorted(sn["fields"].items())}, flush=True)
drops = [k for k, v in BASE.items() if k != "overall" and (sn["fields"].get(k) or {}).get("acc") is not None and sn["fields"][k]["acc"] < v - 0.01]
if sn["overall"] is None or sn["overall"] < BASE["overall"] or drops:
    print("VERDICT FAIL", f"(controls: overall {BASE['overall']} -> {sn['overall']}; drops {drops}) - hold set not read", flush=True)
    sys.exit(0)

H = Path("/srv/ocr/scans/pipeline/out/TestCerts/hold")
byp = {str(e["pdf"]): e for e in allc if e.get("pdf")}
held = []
for js in sorted(H.glob("*.json")):
    sc = json.load(open(js))
    pdf = H / str(sc.get("file") or "")
    if pdf.is_file():
        held.append(byp.get(str(pdf)) or {"pdf": str(pdf), "file": sc.get("source_file") or pdf.name, "fields": sc.get("fields") or {},
                                          "gold_heats": [], "checked": False})
held.sort(key=lambda e: bool(e["fields"].get("heats")))             # the no-heats ones first
print(f"{len(held)} held certs ({sum(1 for e in held if not e['fields'].get('heats'))} with no heats)", flush=True)
ho = [(e["fields"], e) for e in held]
hn = run(held)
cnt = lambda pairs, k: sum(1 for f, _ in pairs if f.get(k))
tot = lambda pairs, k: sum(len(f.get(k) or []) for f, _ in pairs)
for k in ("heats", "items", "mill", "cert_no", "mps_po"):
    print(f"  {k:8} filled: stored {cnt(ho, k):2} -> new {cnt(hn, k):2}", flush=True)
print(f"  heats: stored {tot(ho, 'heats')} -> new {tot(hn, 'heats')}; item lines: stored {tot(ho, 'items')} -> new {tot(hn, 'items')}", flush=True)
lab = [i for i, e in enumerate(held) if e.get("gold_heats")]
so = sw = None
if lab:
    so = TT.score([ho[i] for i in lab])["fields"].get("heat_old_site")
    sw = TT.score([hn[i] for i in lab])["fields"].get("heat_old_site")
    print("  old-site heats on the labelled ones: stored", so, "new", sw, flush=True)
lost = []
for (fo, e), (fw, _) in zip(ho, hn):
    no, nw = len(fo.get("heats") or []), len(fw.get("heats") or [])
    io, iw = len(fo.get("items") or []), len(fw.get("items") or [])
    if nw < no or (iw < io and nw <= no):
        lost.append(Path(str(e["pdf"])).name)
    print(f"  {Path(str(e['pdf'])).name[:46]:46} heats {no}->{nw} items {io}->{iw} | {str(fw.get('heats_text') or '')[:60]}", flush=True)
worse = [k for k in ("heats", "items", "mill") if cnt(hn, k) < cnt(ho, k)]
if so and sw and (sw.get("acc") or 0) < (so.get("acc") or 0):
    worse.append("heat_old_site")
print("  worse on:", lost or "none", flush=True)
ok = not worse
print("VERDICT", "PASS" if ok else "FAIL", f"(overall {BASE['overall']} -> {sn['overall']}; drops {drops}; hold worse {worse})", flush=True)

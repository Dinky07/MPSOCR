#!/usr/bin/env python3
"""453 test: the upright test-cert reader (/tmp/tc453/tc_reader.py) on
  1. the 25 test certs (controls), against the live reader's score (445 night test, 3 Nov 01:41; temperature 0), and
  2. the queued / held test certs whose first page Tesseract finds sideways, against their stored reading.
PASS: overall not lower, no control field lower by more than 0.01, and the sideways certs not worse on items / heats /
mill / old-site heats."""
import importlib.util, json, os, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import tc_training as TT                                # noqa: E402

BASE = {"overall": 0.9947, "cert_no": 1.0, "date": 1.0, "family": 1.0, "grade": 1.0, "heat_old_site": 0.9412, "line_heat": 1.0,
        "line_qty": 1.0, "line_size_nb": 1.0, "lines_count": 1.0, "mill": 1.0, "mps_po": 1.0}     # 445, 3 Nov 01:41
MAX_SIDEWAYS = 40
spec = importlib.util.spec_from_file_location("tc_reader_new", "/tmp/tc453/tc_reader.py")
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
        try:
            f = TT.read_cert(cfg, e, s, few)
        except Exception as ex:
            print(f"  read failed {e['file']}: {ex}", flush=True)
            f = {}
        pairs.append((f, e))
    return pairs


def sideways(pdf):
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return NEW.upright(NEW.render(pdf, 1, 1600, os.path.join(tmp, "p.png")))
        except Exception:
            return 0


R = Path("/srv/ocr/scans/pipeline/out/TestCerts")
byp = {str(e["pdf"]): e for e in allc if e.get("pdf")}
t0, side = time.time(), []
for js in sorted(list(R.glob("*.json")) + list(R.glob("hold/*.json"))):
    sc = json.load(open(js))
    pdf = js.with_name(str(sc.get("file") or ""))
    if not pdf.is_file():
        continue
    deg = sideways(str(pdf))
    if deg:
        side.append((deg, byp.get(str(pdf)) or {"pdf": str(pdf), "file": sc.get("source_file") or pdf.name, "fields": sc.get("fields") or {},
                                                  "gold_heats": [], "checked": False}))
print(f"{len(side)} sideways certs in the queue / hold ({time.time() - t0:.0f} s); "
      f"turned: {dict((d, sum(1 for x, _ in side if x == d)) for d in (90, 180, 270))}", flush=True)
for need in ("2270_041226144347_001", "0059_040606184140_001"):
    if not [1 for _, e in side if need in str(e["pdf"])]:
        print(f"  {need}: not found sideways", flush=True)
side = sorted(side, key=lambda x: not any(n in str(x[1]["pdf"]) for n in ("2270_0412", "0059_0406")))[:MAX_SIDEWAYS]

sn = TT.score(run(tests))
print("BASE", BASE, flush=True)
print("NEW ", sn["overall"], {k: v["acc"] for k, v in sorted(sn["fields"].items())}, flush=True)

so = [(e["fields"], e) for _, e in side]
sw = run([e for _, e in side])
cnt = lambda pairs, k: sum(1 for f, _ in pairs if f.get(k))
tot = lambda pairs, k: sum(len(f.get(k) or []) for f, _ in pairs)
print(f"{len(side)} sideways certs read upright", flush=True)
for k in ("items", "heats", "mill", "cert_no", "mps_po"):
    print(f"  {k:8} filled: stored {cnt(so, k):2} -> new {cnt(sw, k):2}", flush=True)
print(f"  item lines: stored {tot(so, 'items')} -> new {tot(sw, 'items')}; heats: stored {tot(so, 'heats')} -> new {tot(sw, 'heats')}", flush=True)
lab = [(i, e) for i, (_, e) in enumerate(side) if e.get("gold_heats")]
ho = hn = None
if lab:
    ho = TT.score([so[i] for i, _ in lab])["fields"].get("heat_old_site")
    hn = TT.score([sw[i] for i, _ in lab])["fields"].get("heat_old_site")
    print("  old-site heats on the labelled ones: stored", ho, "new", hn, flush=True)
for (fo, e), (fn, _) in zip(so, sw):
    name = Path(str(e["pdf"])).name
    print(f"  {name[:50]:50} items {len(fo.get('items') or [])}->{len(fn.get('items') or [])} heats "
          f"{len(fo.get('heats') or [])}->{len(fn.get('heats') or [])} | {str(fn.get('heats_text') or '')[:70]}", flush=True)
drops = [k for k, v in BASE.items() if k != "overall" and (sn["fields"].get(k) or {}).get("acc") is not None and sn["fields"][k]["acc"] < v - 0.01]
worse = [k for k in ("items", "heats", "mill") if cnt(sw, k) < cnt(so, k)]
if ho and hn and (hn.get("acc") or 0) < (ho.get("acc") or 0):
    worse.append("heat_old_site")
ok = sn["overall"] is not None and sn["overall"] >= BASE["overall"] and not drops and not worse
print("VERDICT", "PASS" if ok else "FAIL", f"(overall {BASE['overall']} -> {sn['overall']}; drops {drops}; sideways worse {worse})", flush=True)

#!/usr/bin/env python3
"""518 test: the test-cert reader with the second look (/tmp/tc518/tc_reader.py) on
  1. the 25 test certs (controls): a control that reads with items and heats is never looked at again, so its reading
     is the live reader's. PASS when no control was read again, or (if some were) the score is not lower than the 453
     baseline and no field drops by more than 0.01;
  2. every test cert on the Test Certs hold, against its stored reading. PASS when no count (certs with heats / items /
     mill, total heats / item lines) is lower than stored."""
import importlib.util, json, sys, time
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import tc_training as TT                                # noqa: E402

BASE = {"overall": 0.9947, "cert_no": 1.0, "date": 1.0, "family": 1.0, "grade": 1.0, "heat_old_site": 0.9412, "line_heat": 1.0,
        "line_qty": 1.0, "line_size_nb": 1.0, "lines_count": 1.0, "mill": 1.0, "mps_po": 1.0}     # 453, 3 Nov 10:37
spec = importlib.util.spec_from_file_location("tc_reader_new", "/tmp/tc518/tc_reader.py")
NEW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NEW)
TT.tc_reader = NEW
LOOKED = []
_rescue = NEW.rescue


def rescue(path, pages, *a, **k):
    n = _rescue(path, pages, *a, **k)
    if n:
        LOOKED.append((Path(str(path)).name, [q.get("how") for q in pages if q.get("page")]))
    return n


NEW.rescue = rescue
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
        k = len(LOOKED)
        try:
            f = TT.read_cert(cfg, e, s, few)
        except Exception as ex:
            print(f"  read failed {e['file']}: {ex}", flush=True)
            f = {}
        pairs.append((f, e))
        print(f"  read {Path(str(e['pdf'])).name[:50]} in {time.time() - t0:.0f} s"
              + (f" | second look: {LOOKED[-1][1]}" if len(LOOKED) > k else ""), flush=True)
    return pairs


sn = TT.score(run(tests))
ctrl_looked = len(LOOKED)
print("BASE", BASE, flush=True)
print("NEW ", sn["overall"], {k: v["acc"] for k, v in sorted(sn["fields"].items())}, flush=True)
drops = [k for k, v in BASE.items() if k != "overall" and (sn["fields"].get(k) or {}).get("acc") is not None and sn["fields"][k]["acc"] < v - 0.01]
print(f"controls read again: {ctrl_looked}", flush=True)
if ctrl_looked and (sn["overall"] is None or sn["overall"] < BASE["overall"] or drops):
    print("VERDICT FAIL", f"(controls read again {ctrl_looked}: overall {BASE['overall']} -> {sn['overall']}; drops {drops}) - hold set not read", flush=True)
    sys.exit(0)

H = Path("/srv/ocr/scans/pipeline/out/TestCerts/hold")
byp = {str(e["pdf"]): e for e in allc if e.get("pdf")}
held = []
for js in sorted(H.glob("*.json")):
    sc = json.load(open(js))
    pdf = H / str(sc.get("file") or "")
    if pdf.is_file():
        held.append(byp.get(str(pdf)) or {"pdf": str(pdf), "file": sc.get("source_file") or pdf.name, "fields": sc.get("fields") or {},
                                          "gold_heats": [], "checked": False, "hold_reason": sc.get("hold_reason")})
print(f"{len(held)} held certs", flush=True)
ho = [(e["fields"], e) for e in held]
hn = run(held)
cnt = lambda pairs, k: sum(1 for f, _ in pairs if f.get(k))
tot = lambda pairs, k: sum(len(f.get(k) or []) for f, _ in pairs)
for k in ("heats", "items", "mill", "cert_no", "mps_po"):
    print(f"  {k:8} filled: stored {cnt(ho, k):2} -> new {cnt(hn, k):2}", flush=True)
print(f"  heats: stored {tot(ho, 'heats')} -> new {tot(hn, 'heats')}; item lines: stored {tot(ho, 'items')} -> new {tot(hn, 'items')}", flush=True)
lost = []
for (fo, e), (fw, _) in zip(ho, hn):
    no, nw = len(fo.get("heats") or []), len(fw.get("heats") or [])
    io, iw = len(fo.get("items") or []), len(fw.get("items") or [])
    if nw < no or iw < io:
        lost.append(Path(str(e["pdf"])).name)
    print(f"  {Path(str(e['pdf'])).name[:46]:46} heats {no}->{nw} items {io}->{iw} | {str(e.get('hold_reason') or '')[:40]}", flush=True)
worse = [k for k in ("heats", "items", "mill") if cnt(hn, k) < cnt(ho, k)]
worse += [f"total {k}" for k in ("heats", "items") if tot(hn, k) < tot(ho, k)]
print("  fewer than stored:", lost or "none", flush=True)
ok = not worse
print("VERDICT", "PASS" if ok else "FAIL", f"(controls read again {ctrl_looked}, overall {BASE['overall']} -> {sn['overall']}; drops {drops}; hold worse {worse})", flush=True)

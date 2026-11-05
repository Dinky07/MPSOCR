#!/usr/bin/env python3
"""528 test: the test-cert reader with the second look (/tmp/tc528/tc_reader.py) on
  1. the 25 test certs (controls). A control that reads with items and heats is never looked at again, so its reading
     is the live reader's. For a control that is looked at again, the first reading of the same run (exactly what the
     live reader returns) is scored next to the second look's. PASS when no control scores lower on any field and the
     overall score is not lower. (518 compared with the 3 Nov score, taken on a different set of certs.)
  2. every test cert on the Test Certs hold, against its stored reading. PASS when no count (certs with heats / items /
     mill, total heats / item lines) is lower than stored."""
import copy, importlib.util, json, sys, time
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import tc_training as TT                                # noqa: E402

spec = importlib.util.spec_from_file_location("tc_reader_new", "/tmp/tc528/tc_reader.py")
NEW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NEW)
TT.tc_reader = NEW
LOOKED = []
_rescue = NEW.rescue


def rescue(path, pages, *a, **k):
    before = copy.deepcopy(pages)
    n = _rescue(path, pages, *a, **k)
    if n:
        LOOKED.append((Path(str(path)).name, [q.get("how") for q in pages if q.get("page")], before))
    return n


NEW.rescue = rescue
cfg, t = TT.load_cfg()
s = TT.settings(cfg)
few = s["fewshot_dir"] or None
allc = TT.certs(cfg)
tests = TT.test_certs(t, allc)
print(f"{len(tests)} test certs; model {s['model']}", flush=True)


def fields_of(pages, e):
    fam = e["fields"].get("family") if e["fields"].get("family") in TT.FAMS else None
    f, _ = NEW.to_fields(NEW.merge(pages, e["file"] or "", fam), e["file"] or "", fam)
    return f


def run(es):
    """-> [(new fields, e)], [(first-reading fields, e)] (the same unless the file was looked at again)"""
    pairs, firsts = [], []
    for e in es:
        t0 = time.time()
        k = len(LOOKED)
        try:
            f = TT.read_cert(cfg, e, s, few)
        except Exception as ex:
            print(f"  read failed {e['file']}: {ex}", flush=True)
            f = {}
        f0 = f
        if len(LOOKED) > k:
            try:
                f0 = fields_of(LOOKED[-1][2], e)
            except Exception as ex:
                print(f"  first reading not rebuilt {e['file']}: {ex}", flush=True)
                f0 = {}
        pairs.append((f, e))
        firsts.append((f0, e))
        print(f"  read {Path(str(e['pdf'])).name[:50]} in {time.time() - t0:.0f} s"
              + (f" | second look: {LOOKED[-1][1]}" if len(LOOKED) > k else ""), flush=True)
    return pairs, firsts


cn, c0 = run(tests)
ctrl_looked = len(LOOKED)
sn, s0 = TT.score(cn), TT.score(c0)
print("FIRST", s0["overall"], {k: v["acc"] for k, v in sorted(s0["fields"].items())}, flush=True)
print("NEW  ", sn["overall"], {k: v["acc"] for k, v in sorted(sn["fields"].items())}, flush=True)
worse_c = []
for (fw, e), (fo, _) in zip(cn, c0):
    if fw is fo:
        continue
    rw, ro = TT.compare(fw, e), TT.compare(fo, e)
    d = {k: f"{ro.get(k, (0, 0))[0]}->{rw.get(k, (0, 0))[0]}/{max(rw.get(k, (0, 0))[1], ro.get(k, (0, 0))[1])}"
         for k in sorted(set(rw) | set(ro))}
    print(f"  looked again {Path(str(e['pdf'])).name[:46]}: {d}", flush=True)
    worse_c += [f"{Path(str(e['pdf'])).name}:{k}" for k in ro if rw.get(k, (0, 0))[0] < ro[k][0]]
print(f"controls read again: {ctrl_looked}; worse: {worse_c or 'none'}", flush=True)
if worse_c or (sn["overall"] or 0) < (s0["overall"] or 0):
    print("VERDICT FAIL", f"(controls read again {ctrl_looked}: overall {s0['overall']} -> {sn['overall']}; worse {worse_c}) - hold set not read", flush=True)
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
hn, _ = run(held)
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
print("VERDICT", "PASS" if ok else "FAIL", f"(controls read again {ctrl_looked}, overall {s0['overall']} -> {sn['overall']}; hold worse {worse})", flush=True)

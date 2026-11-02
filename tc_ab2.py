#!/usr/bin/env python3
"""445 test: the patched test-cert reader (/tmp/tc445/tc_reader.py) on the quick job's test certs, against the live
reader's score on the same certs this afternoon (444, deterministic: temperature 0), and on the held / queued certs with
an empty "inspection_only" page against their stored reading. PASS: overall not lower, no field lower by more than 0.01,
and the empty-page certs not worse on items / heats / mill."""
import importlib.util, json, sys
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import tc_training as TT                                # noqa: E402

BASE = {"overall": 0.9947, "cert_no": 1.0, "date": 1.0, "family": 1.0, "grade": 1.0, "heat_old_site": 0.9412, "line_heat": 1.0,
        "line_qty": 1.0, "line_size_nb": 1.0, "lines_count": 1.0, "mill": 1.0, "mps_po": 1.0}     # 444, 2 Nov 16:26
spec = importlib.util.spec_from_file_location("tc_reader_new", "/tmp/tc445/tc_reader.py")
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


sn = TT.score(run(tests))
print("BASE", BASE, flush=True)
print("NEW ", sn["overall"], {k: v["acc"] for k, v in sorted(sn["fields"].items())}, flush=True)
R = Path("/srv/ocr/scans/pipeline/out/TestCerts")
byp = {str(e["pdf"]): e for e in allc if e.get("pdf")}
fails = []
for js in sorted(list(R.glob("*.json")) + list(R.glob("hold/*.json"))):
    sc = json.load(open(js))
    if not [q for q in sc.get("tc_pages") or [] if (q.get("answer") or {}).get("page_type") == "inspection_only"
            and not any(v not in (None, "", [], {}) for k, v in (q.get("answer") or {}).items() if k not in ("page_type", "legibility"))]:
        continue
    pdf = js.with_name(str(sc.get("file") or ""))
    fails.append(byp.get(str(pdf)) or {"pdf": pdf, "file": sc.get("source_file") or pdf.name, "fields": sc.get("fields") or {},
                                       "gold_heats": [], "checked": False})
print(f"{len(fails)} certs with an empty inspection_only page", flush=True)
fo = [(e["fields"], e) for e in fails]
fn = run(fails)
cnt = lambda pairs, k: sum(1 for f, _ in pairs if f.get(k))
for k in ("items", "heats", "mill", "cert_no", "mps_po", "mps_job"):
    print(f"  {k:8} filled: stored {cnt(fo, k):2} -> new {cnt(fn, k):2}", flush=True)
lab_o = [(f, e) for f, e in fo if e.get("gold_heats")]
if lab_o:
    print("  old-site heats on the labelled ones: stored", TT.score(lab_o)["fields"].get("heat_old_site"),
          "new", TT.score([(f, e) for f, e in fn if e.get("gold_heats")])["fields"].get("heat_old_site"), flush=True)
for f, e in fn:
    if "DOC280117-007" in str(e["pdf"]):
        print("  DOC280117-007 new:", json.dumps({k: f.get(k) for k in ("mill", "cert_no", "mps_po", "mps_job", "heats_text", "item_count", "date")}, default=str), flush=True)
drops = [k for k, v in BASE.items() if k != "overall" and (sn["fields"].get(k) or {}).get("acc") is not None and sn["fields"][k]["acc"] < v - 0.01]
ok = sn["overall"] is not None and sn["overall"] >= BASE["overall"] and not drops and all(cnt(fn, k) >= cnt(fo, k) for k in ("items", "heats", "mill"))
print("VERDICT", "PASS" if ok else "FAIL", f"(overall {BASE['overall']} -> {sn['overall']}; drops {drops})", flush=True)

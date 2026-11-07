#!/usr/bin/env python3
"""591 test (Dino 7 Nov "apply the rules ... and prepare the test cert training"): the test-cert reader with the reading
rules learnt from the certs checked by eye (tc_reader.591.py, tc_rules_patch.py) against the live reader, both read in
this run, on the test certs (1 in 5 by hash, never a worked example), checked certs first, up to TEST_MAX of them
(384 certs are checked now: 299 by Claude 6-7 Nov, 85 before).
PASS needs: no control lower on any field, and overall not lower (the mps-frigate rule: deploy only if nothing that was
right gets worse). Caveat for the report: the rules were drawn from certs read by eye, and some of those are test certs.
    tc_rules_test.py [TEST_MAX]"""
import importlib.util, json, sys, time
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import tc_reader as OLD                                 # noqa: E402
import tc_training as TT                                # noqa: E402

spec = importlib.util.spec_from_file_location("tc_reader_new", "/srv/ocr/pipeline/tc_reader.591.py")
NEW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NEW)
TEST_MAX = int(sys.argv[1]) if len(sys.argv) > 1 else 100
cfg, t = TT.load_cfg()
t["tc_test_max"] = TEST_MAX
s = TT.settings(cfg)
allc = TT.certs(cfg)
tests = TT.test_certs(t, allc)
print(f"model {s['model']}; {len(allc)} certs, {sum(1 for e in allc if e['checked'])} checked; "
      f"{len(tests)} test certs ({sum(1 for e in tests if e['checked'])} checked, the rest scored on old-site heats)", flush=True)
few = s["fewshot_dir"] or None
pairs = {}
for tag, M in (("old", OLD), ("new", NEW)):
    TT.tc_reader = M
    pairs[tag] = []
    t0 = time.time()
    for i, e in enumerate(tests, 1):
        t1 = time.time()
        try:
            f = TT.read_cert(cfg, e, s, few)
        except Exception as ex:
            print(f"  {tag} read failed {e['file']}: {ex}", flush=True)
            f = {}
        pairs[tag].append((f, e))
        print(f"  {tag} {i}/{len(tests)} {Path(str(e['pdf'])).name[:56]} {time.time() - t1:.0f} s", flush=True)
    print(f"{tag}: {len(tests)} certs in {(time.time() - t0) / 60:.0f} min", flush=True)
so, sn = TT.score(pairs["old"]), TT.score(pairs["new"])
print("OLD", so["overall"], {k: v["acc"] for k, v in sorted(so["fields"].items())}, flush=True)
print("NEW", sn["overall"], {k: v["acc"] for k, v in sorted(sn["fields"].items())}, flush=True)
worse, better = [], 0
for (fo, e), (fw, _) in zip(pairs["old"], pairs["new"]):
    ro, rw = TT.compare(fo, e), TT.compare(fw, e)
    w = [k for k in ro if rw.get(k, (0, 0))[0] < ro[k][0]]
    better += any(rw.get(k, (0, 0))[0] > ro[k][0] for k in ro)
    if w:
        worse += [f"{Path(str(e['pdf'])).name}:{k} {ro[k][0]}->{rw.get(k, (0, 0))[0]}/{ro[k][1]}" for k in w]
print(f"controls better on some field: {better}; controls worse: {len(worse)}", flush=True)
for x in worse[:40]:
    print("  worse:", x, flush=True)
ok = not worse and (sn["overall"] or 0) >= (so["overall"] or 0)
print("VERDICT", "PASS" if ok else "FAIL", f"(controls overall {so['overall']} -> {sn['overall']}, {better} better, {len(worse)} field(s) worse)", flush=True)
out = Path(cfg["root"]) / "training" / "reports" / f"tc-rules-{time.strftime('%Y%m%d-%H%M')}.json"
json.dump({"kind": "tc-rules", "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tests": len(tests), "checked_tests": sum(1 for e in tests if e["checked"]),
           "old": so, "new": sn, "controls_better": better, "worse": worse, "verdict": "PASS" if ok else "FAIL",
           "caveat": "the rules were drawn from certs read by eye, some of them test certs"}, open(out, "w"), indent=1, default=str)
print("report:", out, flush=True)

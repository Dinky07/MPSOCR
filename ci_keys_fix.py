#!/usr/bin/env python3
"""Sidecars waiting to sync (out/<Type>/ and hold/) with keys that differ only in case are rewritten with them merged
(ci_keys), and a failed attempt count they caused is reset, so the next sync takes them. Backed up first by the runner.
    python3 ci_keys_fix.py [--apply]"""
import argparse, glob, json, os, sys
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import ci_keys                                          # noqa: E402
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
n = 0
for p in sorted(glob.glob(str(ROOT / "out/*/*.json")) + glob.glob(str(ROOT / "out/*/hold/*.json"))):
    if p.endswith((".tmp", ".lock")):
        continue
    try:
        if not ci_keys.has_dupes_text(p):
            continue
    except (OSError, ValueError):
        continue
    n += 1
    print(("fixing " if a.apply else "would fix ") + p)
    if not a.apply:
        continue
    with sidecar_lock.locked(Path(p), "ci_keys_fix"):
        sc = json.load(open(p, encoding="utf-8"), object_pairs_hook=ci_keys.pairs_hook)
        tmp = p + ".ci.tmp"
        open(tmp, "w", encoding="utf-8").write(json.dumps(sc, indent=2, ensure_ascii=False))
        os.replace(tmp, p)
    at = Path(p + ".attempts")
    if at.exists():
        at.unlink()
print(f"{n} sidecar(s) with keys differing only in case")

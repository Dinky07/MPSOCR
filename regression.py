#!/usr/bin/env python3
"""Regression set: a fixed set of checked documents per document type, re-read by the current code and scored by the
shared scorer (scorer.py), so a reader or prompt change can be checked before it goes live.

Only TEST documents go in (1 in 5 checked documents by content hash, the same split the training uses), so nothing in
the set is ever a worked example or training data. Copies only: the pipeline's own folders are never changed.

    python3 regression.py build [--per-type 30] [--types po,jt]   # freeze the set (adds documents, never replaces)
    python3 regression.py list                                     # what is in the set
    python3 regression.py baseline                                 # score what the pipeline read when each was checked (no GPU)
    python3 regression.py run [--types po,jt,ws,tc] [--label X] [--pause-worker]   # re-read with the current code (GPU)
    python3 regression.py compare OLD.json NEW.json [--max-drop 0.02]              # exit 1 if a field got worse

Re-reading works for orders (po), job transfers (jt), workshop sheets (ws) and test certificates (tc), the same way their
training scores a setup. Other types (delivery notes, release notes, customer orders, proofs of payment, supplier
invoices) are in the set and in `baseline`, but `run` skips them for now: their readers run inside the worker.

A run uses Ollama like the worker does. With --pause-worker the worker is paused (the PAUSE file the training uses)
for the run and resumed after; without it the two share the GPU. Don't run it while a training job is running.

Lives in <pipeline root>/regression/: <type>/<id>.pdf + <id>.json (the checked answer), reports/<stamp>-<label>.json.
"""
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scorer  # noqa: E402

CONFIG = Path(os.environ.get("MPS_OCR_CONFIG", str(HERE / "config.json")))
KIND = {"test_cert": "tc"}                      # sidecar doc_type -> scorer kind (the rest are the same)
REREAD = ("po", "jt", "ws", "tc")


def load_cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def rdir(cfg):
    return Path(cfg["root"]) / "regression"


def kind_of(doc_type):
    return KIND.get(doc_type, doc_type)


def write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, p)


def cases(cfg, types=None):
    out = []
    for js in sorted(rdir(cfg).glob("*/*.json")):
        if js.parent.name == "reports":
            continue
        c = json.loads(js.read_text(encoding="utf-8"))
        if types and c["kind"] not in types:
            continue
        c["pdf"] = str(js.with_suffix(".pdf"))
        out.append(c)
    return out


# ------------------------------------------------------------------------------------------------ build
def candidates(cfg):
    """Checked test documents of every type: (kind, case) with the checked answer and the reading at the time."""
    import training as T
    for e in T.examples(cfg):
        if not e["test"] or not e["pdf"] or str(e["doc_type"]) == "test_cert":
            continue
        yield kind_of(e["doc_type"]), {"id": str(e["hash"])[:16].upper(), "doc_type": e["doc_type"], "folder": e["folder"],
                                        "truth": e["verified"], "read_then": e["read"], "source": e["source"],
                                        "from": e["json"], "pdf_from": e["pdf"]}
    import tc_training as TC                     # certs also count when only the old site's heat numbers are known
    for e in TC.certs(cfg):
        if not e["test"] or not e["pdf"] or not TC.labelled(e):
            continue
        yield "tc", {"id": e["id"][:16], "doc_type": "test_cert", "folder": "TestCerts",
                     "truth": e["fields"] if e["checked"] else None, "read_then": e["read"] or e["fields"], "gold_heats": e["gold_heats"],
                     "from": e["json"], "pdf_from": e["pdf"], "file": e["file"], "pages": e["pages"]}


def cmd_build(cfg, per_type, types):
    have = {}
    for c in cases(cfg):
        have.setdefault(c["kind"], set()).add(c["id"])
    added = {}
    for kind, c in sorted(candidates(cfg), key=lambda kc: (kc[0], kc[1]["id"])):     # lowest hashes first: stable
        if types and kind not in types:
            continue
        ids = have.setdefault(kind, set())
        if c["id"] in ids or len(ids) >= per_type:
            continue
        d = rdir(cfg) / kind
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(c.pop("pdf_from"), d / f"{c['id']}.pdf")
        c.update(kind=kind, frozen_at=datetime.now().isoformat(timespec="seconds"))
        write_json(d / f"{c['id']}.json", c)
        ids.add(c["id"])
        added[kind] = added.get(kind, 0) + 1
    print(json.dumps({"added": added, "in_set": {k: len(v) for k, v in sorted(have.items())}}, indent=1))


def cmd_list(cfg):
    by = {}
    for c in cases(cfg):
        by.setdefault(c["kind"], []).append(c["id"])
    for k, ids in sorted(by.items()):
        print(f"{k:20} {len(ids):4}  {'re-read' if k in REREAD else 'baseline only'}")


# ------------------------------------------------------------------------------------------------ reading
def truth_of(c):
    if c["kind"] == "jt" and c.get("truth") is not None:
        import jt_training
        return jt_training.from_worker(c["truth"])
    return c.get("truth")


class Reader:
    """Re-reads one case with the current code and settings, the way the type's training scores a setup."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.timeout = int(cfg.get("ollama_timeout", 1200))
        self.keep = cfg.get("keep_alive", "15m")

    def settings(self, kind):
        if kind == "po":
            import training as T
            return T.po_settings(self.cfg)
        if kind == "jt":
            import jt_training
            return jt_training.settings(self.cfg)
        if kind == "ws":
            import ws_training
            return ws_training.settings(self.cfg)
        if kind == "tc":
            import tc_training
            return tc_training.settings(self.cfg)
        return {}

    def read(self, c, s):
        k, url = c["kind"], self.cfg["ollama_url"]
        if k == "po":
            import po_reader
            import training as T
            po_reader.JOBS_DIR = self.cfg["root"]
            po_reader.PRICE_MEMORY = str(T.tdir(self.cfg) / "price_memory_train.json")   # never the test orders' own prices
            try:
                data, _, _ = po_reader.read(c["pdf"], url, s["model"], s["fewshot_dir"], int(s.get("pages", 2)), int(s["px"]),
                                            self.timeout, self.keep, **(c.get("source") or {}))
            finally:
                po_reader.PRICE_MEMORY = None
            return po_reader.to_fields(data)[0]
        if k == "jt":
            import jt_reader
            return jt_reader.read(c["pdf"], url, s["model"], s["fewshot_dir"], int(s["px"]), self.timeout, self.keep)[0]
        if k == "ws":
            import ws_reader
            ans = ws_reader.read_model(c["pdf"], url, s["model"], s["fewshot_dir"], int(s["px"]), self.timeout, self.keep)
            return ws_reader.to_fields(ans, Path(c["pdf"]).name, self.cfg["root"])[0]
        if k == "tc":
            import tc_training
            e = {"pdf": c["pdf"], "fields": c.get("truth") or {}, "file": c.get("file") or Path(c["pdf"]).name}
            return tc_training.read_cert(self.cfg, e, s, s.get("fewshot_dir") or None)
        raise NotImplementedError(k)


def report(kind_docs, label, extra=None):
    out = {"label": label, "at": datetime.now().isoformat(timespec="seconds"), "types": {}}
    out.update(extra or {})
    for kind, docs in sorted(kind_docs.items()):
        s = scorer.total([d["metrics"] for d in docs])
        s["failed_reads"] = sum(1 for d in docs if d.get("error"))
        s["docs_detail"] = [{"id": d["id"], "wrong": sorted(m for m, (r, n) in d["metrics"].items() if r < n),
                             **({"error": d["error"]} if d.get("error") else {})} for d in docs]
        out["types"][kind] = s
    return out


def save_report(cfg, rep):
    p = rdir(cfg) / "reports" / f"{datetime.now():%Y%m%d-%H%M%S}-{rep['label']}.json"
    write_json(p, rep)
    for kind, s in rep["types"].items():
        print(f"{kind:18} docs {s['docs']:3}  overall {s['overall']}  failed {s['failed_reads']}")
    print(p)
    return p


def metrics(c, read):
    return scorer.compare(read, truth_of(c), c["kind"], gold_heats=c.get("gold_heats"))


def cmd_baseline(cfg, types):
    docs = {}
    for c in cases(cfg, types):
        rt = c.get("read_then") or {}
        if c["kind"] == "jt":
            import jt_training
            rt = jt_training.from_worker(rt)
        docs.setdefault(c["kind"], []).append({"id": c["id"], "metrics": metrics(c, rt)})
    return save_report(cfg, report(docs, "baseline", {"what": "the pipeline's reading when each document was checked"}))


def cmd_run(cfg, types, label, pause_worker):
    rd = Reader(cfg)
    pause = Path(cfg["root"]) / "PAUSE"
    made_pause = False
    if pause_worker and not pause.exists():
        pause.write_text("regression run: re-reading the regression set\n")
        made_pause = True
    docs, used = {}, {}
    try:
        for c in cases(cfg, types or REREAD):
            if c["kind"] not in REREAD:
                continue
            s = used.setdefault(c["kind"], rd.settings(c["kind"]))
            t0 = time.time()
            try:
                got, err = rd.read(c, s), None
            except Exception as ex:                  # a failed read scores as an empty reading, as in training
                got, err = {}, f"{type(ex).__name__}: {ex}"[:300]
            docs.setdefault(c["kind"], []).append({"id": c["id"], "metrics": metrics(c, got), "error": err,
                                                   "secs": round(time.time() - t0, 1)})
            print(f"{c['kind']} {c['id']} {round(time.time() - t0)} s{' FAILED ' + err if err else ''}", flush=True)
    finally:
        if made_pause:
            pause.unlink(missing_ok=True)
    return save_report(cfg, report(docs, label, {"settings": used}))


def cmd_compare(old_p, new_p, max_drop, min_counted):
    old, new = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (old_p, new_p))
    worse = []
    for kind in sorted(set(old["types"]) | set(new["types"])):
        a, b = old["types"].get(kind), new["types"].get(kind)
        if not a or not b:
            print(f"{kind:18} only in {'the new' if b else 'the old'} report")
            continue
        print(f"{kind:18} overall {a['overall']} -> {b['overall']}   failed reads {a['failed_reads']} -> {b['failed_reads']}")
        for m, x, y in scorer.regressions(a, b, max_drop, min_counted):
            worse.append((kind, m, x, y))
            print(f"    WORSE {m}: {x} -> {y}")
        if b["failed_reads"] > a["failed_reads"]:
            worse.append((kind, "failed_reads", a["failed_reads"], b["failed_reads"]))
            print(f"    WORSE failed reads: {a['failed_reads']} -> {b['failed_reads']}")
    print("REGRESSION" if worse else "OK: nothing got worse by more than", "" if worse else max_drop)
    return 1 if worse else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--per-type", type=int, default=30)
    b.add_argument("--types")
    sub.add_parser("list")
    bl = sub.add_parser("baseline")
    bl.add_argument("--types")
    r = sub.add_parser("run")
    r.add_argument("--types")
    r.add_argument("--label", default="run")
    r.add_argument("--pause-worker", action="store_true")
    c = sub.add_parser("compare")
    c.add_argument("old")
    c.add_argument("new")
    c.add_argument("--max-drop", type=float, default=0.02)
    c.add_argument("--min-counted", type=int, default=5)
    a = ap.parse_args()
    if a.cmd == "compare":
        sys.exit(cmd_compare(a.old, a.new, a.max_drop, a.min_counted))
    cfg = load_cfg()
    types = set(a.types.split(",")) if getattr(a, "types", None) else None
    if a.cmd == "build":
        cmd_build(cfg, a.per_type, types)
    elif a.cmd == "list":
        cmd_list(cfg)
    elif a.cmd == "baseline":
        cmd_baseline(cfg, types)
    elif a.cmd == "run":
        cmd_run(cfg, types, a.label, a.pause_worker)


if __name__ == "__main__":
    main()

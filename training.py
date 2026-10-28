#!/usr/bin/env python3
"""Training for the purchase-order reader, from the documents you have checked on the dashboard.

A document counts as verified once "Checked" is ticked (on the dashboard or before it synced). Its sidecar then holds
the corrected answer ("fields") and, if you edited it, the model's own reading ("fields_read"). Verified orders are
split once and for all by content hash: 1 in 5 is a TEST document (never used to train, only to score), the rest
TRAIN documents.

    python3 training.py status              # counts + how accurate the current reader is, per field
    python3 training.py quick  [--force]    # new worked examples + verified suppliers; installed only if it scores better
    python3 training.py finetune [--force]  # fine-tune the reader model (hours; needs setup_training.sh); same rule
    python3 training.py revert              # back to the previous setup

Everything lives in <pipeline root>/training/:
  active.json                 what the worker uses now (model, worked-examples folder, image size)
  fewshot_active/, history/   the installed worked examples, and earlier setups
  suppliers_verified.json     supplier names you confirmed (the reader snaps misreadings to these first)
  job.json, logs/, reports/   the running / last job, its log, and the A/B score of every job
The dashboard's Training tab drives all of this.
"""
import argparse, difflib, hashlib, json, os, re, shutil, signal, subprocess, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import po_reader  # noqa: E402

CONFIG = Path(os.environ.get("MPS_OCR_CONFIG", str(HERE / "config.json")))
FIELDS = ["order_number", "polymer_po", "supplier", "date", "order_method", "att_of", "allocation", "job_number",
          "other_po_ref", "quote_ref", "payment_terms", "signed_by", "cancelled"]
LINE_FIELDS = ["lines_count", "line_description", "line_qty", "line_price"]
DEFAULTS = {"quick_min_corrected": 20, "quick_min_scored": 8, "finetune_min_verified": 300, "test_max": 60, "add_examples": 1, "max_examples": 5,
            "min_gain": 0.005, "focus": ["line_qty", "line_price"], "venv": "/srv/ocr/train-venv", "base_model": "unsloth/Qwen2.5-VL-7B-Instruct-bnb-4bit",
            "ft_px": 1288, "ft_epochs": 2, "ft_rank": 16}


# ------------------------------------------------------------------------------------------------ setup
def load_cfg():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    t = dict(DEFAULTS)
    t.update(cfg.get("training") or {})
    return cfg, t


def tdir(cfg):
    d = Path(cfg["root"]) / "training"
    for sub in ("logs", "reports", "history"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p, obj):
    p = Path(p)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, p)


def po_settings(cfg):
    """What the worker uses for orders right now: config.json, overridden by training/active.json."""
    tc = (cfg.get("doc_types") or {}).get("PurchaseOrders") or {}
    pc = tc.get("po_reader") if isinstance(tc.get("po_reader"), dict) else {}
    s = {"model": pc.get("model", "qwen2.5vl:7b"), "fewshot_dir": pc.get("fewshot_dir", str(HERE / "po_fewshot")),
         "px": int(pc.get("px", 1600)), "pages": int(pc.get("pages", 2))}
    act = read_json(Path(cfg["root"]) / "training" / "active.json", {}) or {}
    for k in ("model", "fewshot_dir", "px"):
        if act.get(k):
            s[k] = act[k]
    return s


# ------------------------------------------------------------------------------------------------ examples
def is_test(h):
    return int(hashlib.sha256(str(h).encode()).hexdigest()[:8], 16) % 5 == 0


def examples(cfg):
    """Every verified document (Checked ticked): synced ones first, then the out queue and hold queue."""
    root = Path(cfg["root"])
    seen, out = set(), []
    for base, pattern in ((root / "synced", "*/*/*.json"), (root / "out", "*/*.json"), (root / "out", "*/hold/*.json")):
        for js in sorted(base.glob(pattern)):
            sc = read_json(js)
            if not isinstance(sc, dict):
                continue
            f = sc.get("fields") or {}
            if not f.get("checked"):
                continue
            h = f.get("content_hash") or sc.get("source_sha256") or js.name
            if h in seen:
                continue
            seen.add(h)
            pdf = js.with_name(str(sc.get("file") or ""))
            out.append({"json": str(js), "pdf": str(pdf) if pdf.is_file() else None, "doc_type": sc.get("doc_type"),
                        "folder": sc.get("folder"), "verified": f, "read": sc.get("fields_read") or f,
                        "edits": sum(len(e.get("changes") or []) for e in (sc.get("edits") or [])),
                        "hash": h, "test": is_test(h), "synced": base.name == "synced",
                        "form": f.get("form") or "", "supplier": f.get("supplier") or f.get("supplier_name"),
                        # what the worker knows about the scan: the reader's date check needs it (scan-date year anchor)
                        "source": {"source_folder": sc.get("source_folder"), "source_modified": sc.get("source_modified"),
                                   "source_sha256": sc.get("source_sha256")}})
    return out


def po_examples(cfg):
    return [e for e in examples(cfg) if e["doc_type"] == "po" and e["pdf"] and str(e["pdf"]).lower().endswith(".pdf")]


# ------------------------------------------------------------------------------------------------ scoring
def _n(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def _num(v):
    try:
        return None if v in (None, "") else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def compare(read, ver):
    """Per-field (right, counted) for one document: read = what a reader produced, ver = the checked answer."""
    res = {}
    for k in FIELDS:
        a, b = read.get(k), ver.get(k)
        if (a in (None, "")) and (b in (None, "")):
            continue                                   # blank on the form and blank in the reading: not scored
        res[k] = (1 if _n(a) == _n(b) else 0, 1)
    ri, vi = read.get("items") or [], ver.get("items") or []
    if ri or vi:
        res["lines_count"] = (1 if len(ri) == len(vi) else 0, 1)
        d = q = p = 0
        for x, y in zip(ri, vi):
            d += 1 if difflib.SequenceMatcher(None, _n(x.get("description")), _n(y.get("description"))).ratio() >= 0.9 else 0
            q += 1 if _num(x.get("quantity")) == _num(y.get("quantity")) else 0
            p += 1 if _num(x.get("unit_price")) == _num(y.get("unit_price")) else 0
        n = max(len(vi), 1)
        res["line_description"], res["line_qty"], res["line_price"] = (d, n), (q, n), (p, n)
    return res


def score(pairs):
    """pairs: [(read, verified)] -> {"fields": {k: {"right","counted","acc"}}, "overall": acc, "docs": n}."""
    tot = {}
    for read, ver in pairs:
        for k, (r, c) in compare(read, ver).items():
            t = tot.setdefault(k, [0, 0])
            t[0] += r
            t[1] += c
    fields = {k: {"right": r, "counted": c, "acc": round(r / c, 4) if c else None} for k, (r, c) in tot.items()}
    accs = [v["acc"] for v in fields.values() if v["acc"] is not None]
    return {"fields": fields, "overall": round(sum(accs) / len(accs), 4) if accs else None, "docs": len(pairs)}


# ------------------------------------------------------------------------------------------------ answer format
def to_answer(f):
    """A checked sidecar -> the JSON answer the trained reader is taught to give (po_ocr_v2 format)."""
    d = f.get("date")
    dw = f.get("date_as_written")
    if d and re.fullmatch(r"\d{4}-\d\d-\d\d", str(d)):
        y, m, dd = str(d).split("-")
        mw = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s*", str(dw or ""))
        if not mw or (int(mw.group(1)), int(mw.group(2))) != (int(dd), int(m)):
            dw = f"{int(dd)}.{m}.{y}"                  # keep what's "written" consistent with the corrected date
    job = f.get("job_number")
    jr = str(f.get("job_number_read") or "")
    bare, full = re.findall(r"\d{3,5}[A-Z]?", jr.upper()), re.findall(r"(\d{3,5})[A-Z]", str(job or "").upper())
    if job and bare and [int(re.sub(r"\D", "", b)) for b in bare] == [int(x) for x in full]:
        job = jr                # written without its letter: teach the reader what is on the page; tidy adds the letter
    other = f.get("other_po_ref")
    if not other and f.get("polymer_po"):
        other = "PO " + str(f["polymer_po"])[2:]
    return {
        "doc_type": f.get("po_doc_type") or "purchase_order", "form": f.get("form") or "mps_order",
        "order_number": f.get("order_number"), "other_po_ref": other, "supplier": f.get("supplier"),
        "att_of": f.get("att_of"), "order_method": f.get("order_method"),
        "transfer_from": f.get("transfer_from"), "transfer_to": f.get("transfer_to"),
        "date_as_written": dw, "date_iso": d, "allocation": f.get("allocation"), "job_number": job,
        "lines": [{"qty": i.get("quantity"), "open_quantity": bool(i.get("open_quantity")), "description": i.get("description"),
                   "unit_price": i.get("unit_price"), "price_unit": i.get("price_unit")} for i in (f.get("items") or [])],
        "quote_ref": f.get("quote_ref"), "payment_terms": f.get("payment_terms"), "notes": f.get("notes"),
        "delivery_address": f.get("delivery_address"), "writer": f.get("writer"), "signed_by": f.get("signed_by"),
        "cancelled": bool(f.get("cancelled")), "legibility": f.get("legibility") or "good",
    }


def render_png(pdf, dst, px):
    dst = Path(dst)
    subprocess.run(["pdftoppm", "-png", "-scale-to", str(px), "-f", "1", "-l", "1", "-singlefile", str(pdf), str(dst.with_suffix(""))],
                   check=True, capture_output=True, timeout=300)
    return dst


def _png_size(p):
    import struct
    with open(p, "rb") as fh:
        head = fh.read(24)
    return struct.unpack(">II", head[16:24]) if head[:8] == b"\x89PNG\r\n\x1a\n" else (1131, 1600)


def est_tokens(fewshot_dir, prompt, px, pages):
    """Rough size of one request to the reader: every worked example (prompt + image + answer) plus the document's own
    pages. Qwen2.5-VL uses one token per 28 x 28 pixels of image; ~3.5 characters of text per token."""
    tot = 0
    for j in sorted(Path(fewshot_dir).glob("*.json")) if fewshot_dir and Path(fewshot_dir).is_dir() else []:
        png = j.with_suffix(".png")
        if j.name == "sources.json" or not png.exists():
            continue
        w, h = _png_size(png)
        tot += len(prompt) / 3.5 + len(j.read_text(encoding="utf-8")) / 3.5 + (w // 28) * (h // 28)
    page = (int(px) // 28) * (int(int(px) / 1.414) // 28)
    return int(tot + len(prompt) / 3.5 + pages * page)


def fits(tokens, num_ctx, num_predict):
    """Room left for the answer after the request (5 % margin for the estimate)."""
    return tokens * 1.05 + num_predict <= num_ctx


def rejected_examples(d):
    return set(read_json(Path(d) / "rejected_examples.json", []) or [])


def add_rejected(d, hashes, job, why):
    old = rejected_examples(d)
    write_json(Path(d) / "rejected_examples.json", sorted(old | set(hashes)))
    job.log(f"  not tried again as worked examples ({why}): {len(set(hashes) - old)} more, {len(old | set(hashes))} in all")


def test_valid(a, b, n):
    """A comparison only counts when both setups could actually read the test documents."""
    lim = max(1, n // 10)
    if (b.get("failed_reads") or 0) > lim:
        return False, f"test not valid: {b['failed_reads']} of {n} reads failed with the new setup"
    if (a.get("failed_reads") or 0) > lim:
        return False, f"test not valid: {a['failed_reads']} of {n} reads failed with the current setup"
    return True, ""


# ------------------------------------------------------------------------------------------------ job plumbing
class Job:
    """job.json for the dashboard: state, step, progress; PAUSE handling so the worker leaves the GPU alone."""

    def __init__(self, cfg, kind):
        self.cfg, self.kind, self.d = cfg, kind, tdir(cfg)
        self.started = datetime.now().isoformat(timespec="seconds")
        self.log_path = self.d / "logs" / f"{kind}-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.pause = Path(cfg["root"]) / "PAUSE"
        self.made_pause = False
        self.state = {"kind": kind, "state": "running", "pid": os.getpid(), "started": self.started, "step": "", "done": 0,
                      "total": 0, "log": self.log_path.name}
        signal.signal(signal.SIGTERM, self._cancel)
        self.save()

    def save(self):
        write_json(self.d / "job.json", self.state)

    def log(self, msg):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def step(self, name, total=0):
        self.state.update(step=name, done=0, total=total)
        self.log(name)
        self.save()

    def tick(self):
        self.state["done"] += 1
        self.save()

    def pause_worker(self, why):
        if not self.pause.exists():
            self.pause.write_text(f"training job ({self.kind}): {why}\n")
            self.made_pause = True
            self.log("worker paused (GPU work) for the training job")

    def resume_worker(self):
        if self.made_pause:
            self.pause.unlink(missing_ok=True)
            self.made_pause = False
            self.log("worker resumed")

    def finish(self, state, summary):
        self.resume_worker()
        self.state.update(state=state, finished=datetime.now().isoformat(timespec="seconds"), summary=summary)
        self.save()
        self.log(f"{state}: {summary}")

    def _cancel(self, *_):
        self.finish("cancelled", "cancelled from the dashboard")
        sys.exit(1)


def evaluate(job, cfg, tests, model, fewshot_dir, px, label):
    """Run a reader setup over the test documents and score it against the checked answers."""
    job.step(f"scoring {label} on {len(tests)} test documents", len(tests))
    pairs, fails, date_misses = [], 0, []
    for e in tests:
        try:
            po_reader.JOBS_DIR = cfg["root"]           # job list, orders index, scan times - as the worker reads
            po_reader.PRICE_MEMORY = str(tdir(cfg) / "price_memory_train.json")   # never the test orders' own prices
            data, _, _ = po_reader.read(e["pdf"], cfg["ollama_url"], model, fewshot_dir, 2, px,
                                        int(cfg.get("ollama_timeout", 1200)), cfg.get("keep_alive", "15m"),
                                        **(e.get("source") or {}))
            got, _ = po_reader.to_fields(data)
        except Exception as ex:                        # a failed read scores as an empty reading
            fails += 1
            job.log(f"  read failed for {Path(e['pdf']).name}: {ex}")
            got = {}
        pairs.append((got, e["verified"]))
        if got and _n(got.get("date")) != _n(e["verified"].get("date")):
            miss = {"file": Path(e["pdf"]).name, "read": got.get("date"), "written": got.get("date_as_written"),
                    "checked": e["verified"].get("date")}
            date_misses.append(miss)
            job.log(f"  date: {miss['file']}: read {miss['read']} (as written: {miss['written']!r}), checked {miss['checked']}")
        job.tick()
    po_reader.PRICE_MEMORY = None
    s = score(pairs)
    s.update(model=model, fewshot_dir=str(fewshot_dir) if fewshot_dir else None, px=px, failed_reads=fails,
             date_misses=date_misses)
    job.log(f"  {label}: overall {s['overall']}  " + ", ".join(f"{k} {v['acc']}" for k, v in sorted(s["fields"].items())))
    return s


def focus_acc(s, keys):
    """Accuracy pooled over the focus fields (line quantities and prices: right / counted over every line)."""
    r = sum(((s.get("fields") or {}).get(k) or {}).get("right", 0) for k in keys)
    c = sum(((s.get("fields") or {}).get(k) or {}).get("counted", 0) for k in keys)
    return round(r / c, 4) if c else None


def better(b, a, min_gain, focus=None):
    """Install B only if it is better and not clearly worse on the fields that matter most. With a focus (Dino 25 Oct:
    line quantities and prices) only those count: B must read more line quantities + prices right; the order number,
    supplier and date must not drop by more than 2 points; everything else is ignored."""
    if focus:
        fa, fb = focus_acc(a, focus), focus_acc(b, focus)
        if fa is None or fb is None:
            return False, "no lines to compare"
        for k in ("order_number", "supplier", "date"):
            ka, kb = a["fields"].get(k, {}).get("acc"), b["fields"].get(k, {}).get("acc")
            if ka is not None and kb is not None and kb < ka - 0.02:
                return False, f"{k} got worse ({kb:.3f} vs {ka:.3f})"
        detail = ", ".join(f"{k} {b['fields'].get(k, {}).get('acc')} vs {a['fields'].get(k, {}).get('acc')}" for k in focus)
        if fb < fa + min_gain:
            return False, f"line qty + price {fb:.3f} vs {fa:.3f}: not better by {min_gain} ({detail})"
        return True, f"line qty + price {fb:.3f} vs {fa:.3f} ({detail})"
    if b["overall"] is None or a["overall"] is None:
        return False, "nothing to compare"
    if b["overall"] < a["overall"] + min_gain:
        return False, f"overall {b['overall']:.3f} vs {a['overall']:.3f}: not better by {min_gain}"
    for k in ("order_number", "supplier", "date"):
        fa, fb = a["fields"].get(k, {}).get("acc"), b["fields"].get(k, {}).get("acc")
        if fa is not None and fb is not None and fb < fa - 0.02:
            return False, f"{k} got worse ({fb:.3f} vs {fa:.3f})"
    return True, f"overall {b['overall']:.3f} vs {a['overall']:.3f}"


def install(cfg, job, new_active, report):
    d = tdir(cfg)
    old = read_json(d / "active.json")
    if old:
        write_json(d / "history" / f"active-{datetime.now():%Y%m%d-%H%M%S}.json", old)
    new_active.update(installed_at=datetime.now().isoformat(timespec="seconds"), job=report["id"])
    write_json(d / "active.json", new_active)
    job.log(f"installed: {json.dumps(new_active)}")


def test_set(t, pos):
    tests = sorted([e for e in pos if e["test"]], key=lambda e: e["hash"])
    return tests[: int(t["test_max"])]


# ------------------------------------------------------------------------------------------------ commands
def cmd_status(cfg, t, quiet=False):
    ex = examples(cfg)
    pos = [e for e in ex if e["doc_type"] == "po"]
    by_type = {}
    for e in ex:
        b = by_type.setdefault(e["doc_type"] or "?", {"verified": 0, "corrected": 0})
        b["verified"] += 1
        b["corrected"] += 1 if e["edits"] else 0
    acc = score([(e["read"], e["verified"]) for e in pos])
    corrected_train = sum(1 for e in pos if e["edits"] and not e["test"])
    venv_ok = Path(t["venv"], "bin", "python").exists()
    st = {"at": datetime.now().isoformat(timespec="seconds"), "by_type": by_type, "po_accuracy": acc,
          "po": {"verified": len(pos), "corrected": sum(1 for e in pos if e["edits"]), "train": sum(1 for e in pos if not e["test"]),
                 "test": sum(1 for e in pos if e["test"]), "corrected_train": corrected_train},
          "thresholds": {"quick_min_corrected": t["quick_min_corrected"], "finetune_min_verified": t["finetune_min_verified"]},
          "quick_ready": corrected_train >= t["quick_min_corrected"] and len(pos) - int(t["add_examples"]) >= int(t["quick_min_scored"]), "finetune_ready": len(pos) >= t["finetune_min_verified"] and venv_ok,
          "finetune_env": venv_ok, "active": po_settings(cfg),
          "finetune_resume": (resumable(cfg).name if resumable(cfg) else None)}
    if not quiet:
        print(json.dumps(st, indent=2))
    return st


def update_verified_suppliers(cfg, job=None):
    """Supplier names on checked orders -> training/suppliers_verified.json (the reader snaps misreadings to these)."""
    names = {}
    for e in examples(cfg):
        if e["doc_type"] != "po":
            continue
        s = (e["verified"].get("supplier") or "").strip().upper()
        if s and s != "UNKNOWN SUPPLIER":
            k = "pl" if str(e["verified"].get("order_number") or "").startswith("PL") else "all"
            names.setdefault(k, {}).setdefault(s, 0)
            names[k][s] += 1
    write_json(tdir(cfg) / "suppliers_verified.json", {"note": "Supplier names from checked orders (training.py)", **names})
    n = sum(len(v) for v in names.values())
    if job:
        job.log(f"verified supplier list: {n} names")
    # unit prices from checked orders: the reader corrects misread prices to these (price_memory.py). A second copy
    # from the training side only is what the reader uses while it is being scored, so test orders don't mark themselves.
    try:
        import price_memory
        pos = [e for e in examples(cfg) if e["doc_type"] == "po"]
        n_p = price_memory.write(tdir(cfg) / "price_memory.json", [e["verified"] for e in pos])
        price_memory.write(tdir(cfg) / "price_memory_train.json", [e["verified"] for e in pos if not e["test"]])
        if job:
            job.log(f"price memory: {n_p} item prices")
    except Exception as ex:                            # the reader works without it
        if job:
            job.log(f"price memory not built: {ex}")
    return n


def cmd_quick(cfg, t, force=False):
    job = Job(cfg, "quick")
    rid = f"quick-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        pos = po_examples(cfg)
        corrected = [e for e in pos if e["edits"] and not e["test"]]
        tests = test_set(t, pos)
        job.log(f"{len(pos)} verified orders: {len(corrected)} corrected training orders, {len(tests)} test orders")
        if len(corrected) < t["quick_min_corrected"] and not force:
            return job.finish("skipped", f"needs {t['quick_min_corrected']} corrected orders to learn from (has {len(corrected)})")
        update_verified_suppliers(cfg, job)
        po_reader.EXTRA_VOCAB = str(tdir(cfg) / "suppliers_verified.json")     # as the worker uses it
        cur = po_settings(cfg)
        # candidate worked examples: the current ones + the most-corrected orders, one per form / supplier where possible
        cand = tdir(cfg) / "candidates" / rid
        cand.mkdir(parents=True, exist_ok=True)
        cur_dir = Path(cur["fewshot_dir"])
        used = set(read_json(cur_dir / "sources.json") or [])       # orders already shown as worked examples
        rejected = rejected_examples(tdir(cfg))                       # tried as examples before and scored worse
        if rejected:
            job.log(f"  {len(rejected)} order(s) already tried as worked examples and not better: skipped")
        for f in sorted(cur_dir.glob("*.json")):
            if f.name == "sources.json":
                continue
            png = f.with_suffix(".png")
            if png.exists():
                shutil.copy2(f, cand / f.name)
                shutil.copy2(png, cand / png.name)
        have = len([f for f in cand.glob("*.json") if f.name != "sources.json"])
        room = max(0, min(int(t["add_examples"]), int(t["max_examples"]) - have))
        focus = t.get("focus")
        if focus and not room and have:
            # full: the example with the fewest priced lines teaches least about line numbers; it makes way for a new one
            def priced(j):
                return sum(1 for i in ((read_json(j) or {}).get("lines") or []) if i.get("unit_price") not in (None, ""))
            out = min((f for f in cand.glob("*.json") if f.name != "sources.json"), key=priced)
            n_out = priced(out)
            for f in (out, out.with_suffix(".png")):
                f.unlink(missing_ok=True)
            job.log(f"  - worked example {out.stem} taken out ({n_out} priced lines, the fewest) to make room")
            room = 1
        if focus:
            # Dino 25 Oct: train on line quantities and prices only - orders where the model's own reading of them was
            # wrong against the checked (ordering spreadsheet) values, same number of lines so the lines line up
            def misread(e):
                ri, vi = e["read"].get("items") or [], e["verified"].get("items") or []
                if not vi or len(ri) != len(vi):
                    return 0
                c = compare(e["read"], e["verified"])
                return sum(c[k][1] - c[k][0] for k in focus if k in c)
            for e in corrected:
                e["_misread"] = misread(e)
            corrected = [e for e in corrected if e["_misread"]]
            job.log(f"  focus {', '.join(focus)}: {len(corrected)} training orders with misread line numbers to choose from")
            ranked = sorted(corrected, key=lambda e: (-e["_misread"], e["hash"]))
        else:
            # most-corrected first; first pass one per form, then fill up with different suppliers
            ranked = sorted(corrected, key=lambda e: (-e["edits"], e["hash"]))
        picked, forms, sups = [], set(), set()
        for first_pass in (True, False):
            for e in ranked:
                if len(picked) >= room:
                    break
                key_f, key_s = e["form"], _n(e["supplier"])
                if e in picked or e["hash"] in rejected or e["hash"] in used or key_s in sups or (first_pass and key_f in forms):
                    continue
                picked.append(e)
                forms.add(key_f)
                sups.add(key_s)
        have = len([f for f in cand.glob("*.json") if f.name != "sources.json"])
        for i, e in enumerate(picked, 1):
            stem = cand / f"{have + i}_verified_{_n(e['supplier'])[:20] or 'order'}"
            e["_stem"] = stem
            render_png(e["pdf"], stem.with_suffix(".png"), cur["px"])
            write_json(stem.with_suffix(".json"), to_answer(e["verified"]))
            job.log(f"  + worked example: {Path(e['pdf']).name} ({e.get('_misread', e['edits'])} {'misread line numbers' if focus else 'corrections'})")
        # the request must fit the model's context (po_reader.NUM_CTX) with room for the answer: drop new examples until it does
        num_ctx = int(getattr(po_reader, "NUM_CTX", 32768))
        while picked and not fits(est_tokens(cand, po_reader.PROMPT, cur["px"], cur["pages"]), num_ctx, po_reader.NUM_PREDICT):
            e = picked.pop()
            for f in (e["_stem"].with_suffix(".png"), e["_stem"].with_suffix(".json")):
                f.unlink(missing_ok=True)
            job.log(f"  - {Path(e['pdf']).name}: left out, the request would be too long for the model's {num_ctx}-token context")
        if not picked:
            return job.finish("skipped", "no new orders to add as worked examples")
        job.log(f"  request size with the new examples: ~{est_tokens(cand, po_reader.PROMPT, cur['px'], cur['pages'])} of {num_ctx} tokens")
        write_json(cand / "sources.json", sorted(used | {e["hash"] for e in picked}))
        # score on the test orders, topped up with checked training orders that are not worked examples (in either
        # setup), so a few-shot run can start before there are enough test orders; a worked example is never scored
        excl = used | {e["hash"] for e in picked}
        if len(tests) < int(t["quick_min_scored"]):
            extra = sorted((e for e in pos if not e["test"] and e["hash"] not in excl), key=lambda e: e["hash"])
            tests = tests + extra[: int(t["quick_min_scored"]) * 2 - len(tests)]
            job.log(f"  scoring on {len(tests)} orders (test orders + checked orders not used as worked examples)")
        if len(tests) < int(t["quick_min_scored"]):
            return job.finish("skipped", f"needs at least {t['quick_min_scored']} checked orders to score against, besides the worked examples (has {len(tests)}); keep checking orders")
        job.pause_worker("A/B scoring")
        a = evaluate(job, cfg, tests, cur["model"], str(cur_dir), cur["px"], "current setup")
        po_reader._FEW.clear()
        b = evaluate(job, cfg, tests, cur["model"], str(cand), cur["px"], "with new worked examples")
        valid, invalid_why = test_valid(a, b, len(tests))
        ok, why = better(b, a, t["min_gain"], t.get("focus")) if valid else (False, invalid_why)
        if valid and not ok:
            add_rejected(tdir(cfg), [e["hash"] for e in picked], job, why)
        report = {"id": rid, "kind": "quick", "at": datetime.now().isoformat(timespec="seconds"), "tests": len(tests),
                  "added": [Path(e["pdf"]).name for e in picked], "added_hashes": [e["hash"] for e in picked],
                  "current": a, "candidate": b, "installed": ok, "valid": valid, "why": why,
                  "focus": focus, "focus_current": focus_acc(a, focus) if focus else None, "focus_candidate": focus_acc(b, focus) if focus else None}
        if ok:
            active = tdir(cfg) / "fewshot_active"
            if active.exists():
                shutil.move(str(active), str(tdir(cfg) / "history" / f"fewshot-{datetime.now():%Y%m%d-%H%M%S}"))
            shutil.copytree(cand, active)
            install(cfg, job, {"model": cur["model"], "fewshot_dir": str(active), "px": cur["px"]}, report)
        write_json(tdir(cfg) / "reports" / f"{rid}.json", report)
        job.finish("done", ("installed: " if ok else "kept the current setup: ") + why)
    except Exception as ex:
        job.finish("failed", f"{type(ex).__name__}: {ex}")
        raise


def ollama_cli(t):
    """How to run the ollama command: on the PATH, or inside its Docker container (mps-frigate runs ollama/ollama as a
    container called "ollama", with /srv/ocr/scans mounted at /scans). Returns (command, host->container path map)."""
    exe = shutil.which("ollama")
    if exe and not t.get("ollama_container"):
        return [exe], {}
    if shutil.which("docker"):
        return ["docker", "exec", t.get("ollama_container") or "ollama", "ollama"], dict(t.get("ollama_path_map") or {"/srv/ocr/scans": "/scans"})
    raise RuntimeError("no ollama command on this box and no docker to reach the Ollama container")


def in_container(path, pmap):
    p = str(path)
    if not pmap:
        return p
    for host, inside in sorted(pmap.items(), key=lambda kv: -len(kv[0])):
        h = host.rstrip("/")
        if p == h or p.startswith(h + "/"):
            return inside.rstrip("/") + p[len(h):]
    raise RuntimeError(f"{p} isn't inside a folder the Ollama container can see ({', '.join(pmap)})")


def complete_merged(work, t, job):
    """Files Ollama's converter reads that a merged save can leave out (the image preprocessor settings): copied from the
    base model in the Hugging Face cache when missing."""
    merged = work / "merged"
    need = [n for n in ("preprocessor_config.json", "chat_template.json", "generation_config.json") if not (merged / n).exists()]
    if not need:
        return
    roots = [t.get("hf_home"), os.environ.get("HF_HOME"), "/srv/ml/hf-cache", "/srv/ocr/hf-cache"]
    for r in [Path(x) for x in roots if x]:
        for snap in sorted(r.glob("hub/models--*Qwen2.5-VL-7B-Instruct*/snapshots/*")):
            for n in list(need):
                if (snap / n).exists():
                    shutil.copy2(snap / n, merged / n)
                    need.remove(n)
                    job.log(f"  {n} copied into the merged model from {snap}")
    if need:
        job.log(f"  not in the merged model or the base model cache: {', '.join(need)}")


def ollama_import(t, job, name, work):
    """ollama create <name> from the merged weights, 4-bit like the stock model. The quantisation flag depends on the
    Ollama version (older: q4_K_M; 0.40+: int4); each is tried in turn, last of all no quantisation (16-bit, ~16 GB:
    runs partly on the CPU, slow but testable). True when one worked."""
    cmd, pmap = ollama_cli(t)
    complete_merged(work, t, job)
    (work / "Modelfile").write_text(f"FROM {in_container(work / 'merged', pmap)}\nPARAMETER temperature 0\n", encoding="utf-8")
    quants = list(dict.fromkeys([t.get("ollama_quantize"), "q4_K_M", "int4", None]))   # a configured one first, None last
    if quants[0] is None:
        quants = quants[1:] + [None]
    for q in quants:
        full = cmd + ["create", name] + (["-q", q] if q else []) + ["-f", in_container(work / "Modelfile", pmap)]
        job.log("  " + " ".join(full))
        r = subprocess.run(full, capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        with open(job.log_path, "a", encoding="utf-8") as fh:
            fh.write(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out)[-4000:] + "\n")
        if r.returncode == 0:
            job.log(f"  imported as {name}" + (f" ({q})" if q else " (not quantised: 16-bit, expect it to be slow)"))
            return True
        if re.search(r"unsupported (MLX )?architecture", out):
            # this Ollama's safetensors importer doesn't know Qwen2.5-VL: convert to GGUF with llama.cpp instead
            job.log("  Ollama can't import these safetensors directly (" + out.strip().splitlines()[-1][:120] + "); converting to GGUF")
            return gguf_import(t, job, name, work, cmd, pmap)
        if "unsupported --quantize" not in out and "quantiz" not in out.lower():
            return False                      # not the flag: a real conversion error, no point retrying
        job.log(f"  quantisation {q!r} not accepted by this Ollama; trying the next")
    return False


def gguf_import(t, job, name, work, cmd, pmap):
    """merged safetensors -> GGUF (f16 text model + vision projector) with llama.cpp's converter, quantised to Q4_K_M like
    the stock qwen2.5vl:7b, then `ollama create` from the two GGUF files. llama.cpp runs from its Docker image
    (ghcr.io/ggml-org/llama.cpp:full, CPU only) so nothing is installed on the box. ~20-40 min, ~20 GB of disk."""
    img = t.get("llamacpp_image", "ghcr.io/ggml-org/llama.cpp:full")
    merged, gg = work / "merged", work / "gguf"
    gg.mkdir(exist_ok=True)
    f16, mm, q4 = gg / "model-f16.gguf", gg / "mmproj-f16.gguf", gg / "model-Q4_K_M.gguf"
    free = shutil.disk_usage(work).free / 2**30
    if not q4.exists() and free < 22:
        job.log(f"  only {free:.0f} GB free beside {work}: the GGUF conversion needs ~20 GB"); return False
    dock = ["docker", "run", "--rm", "-v", f"{work}:{work}", "-v", f"{merged}:{merged}"]

    def run_log(args, what):
        job.log(f"  {what}")
        r = subprocess.run(args, capture_output=True, text=True)
        with open(job.log_path, "a", encoding="utf-8") as fh:
            fh.write(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", (r.stdout or "") + (r.stderr or ""))[-6000:] + "\n")
        if r.returncode != 0:
            job.log(f"  failed (exit {r.returncode}): {what}")
        return r.returncode == 0

    if not mm.exists() and not run_log(dock + ["--entrypoint", "python3", img, "/app/convert_hf_to_gguf.py", str(merged), "--mmproj",
                                                "--outtype", "f16", "--outfile", str(mm)], "vision projector -> GGUF"):
        return False
    if not (q4.exists() or f16.exists()) and not run_log(dock + ["--entrypoint", "python3", img, "/app/convert_hf_to_gguf.py", str(merged),
                                                                   "--outtype", "f16", "--outfile", str(f16)], "text model -> GGUF f16"):
        return False
    if not q4.exists() and not run_log(dock + ["--entrypoint", "/app/llama-quantize", img, str(f16), str(q4), "Q4_K_M"], "quantising to Q4_K_M"):
        return False
    f16.unlink(missing_ok=True)
    (work / "Modelfile").write_text(f"FROM {in_container(q4, pmap)}\nFROM {in_container(mm, pmap)}\nPARAMETER temperature 0\n", encoding="utf-8")
    full = cmd + ["create", name, "-f", in_container(work / "Modelfile", pmap)]
    if not run_log(full, " ".join(full)):
        return False
    job.log(f"  imported as {name} (GGUF Q4_K_M + vision projector)")
    return True


def resumable(cfg):
    """The newest full-training run that finished fine-tuning (merged weights saved) but has no result yet, e.g. because
    the import into Ollama failed. None when there isn't one."""
    d = tdir(cfg) / "finetune"
    for w in sorted(d.glob("finetune-*"), reverse=True) if d.is_dir() else []:
        if (w / "merged" / "config.json").exists() and not (tdir(cfg) / "reports" / f"{w.name}.json").exists():
            return w
        if (tdir(cfg) / "reports" / f"{w.name}.json").exists():
            return None
    return None


def cmd_finetune(cfg, t, force=False, resume=False):
    job = Job(cfg, "finetune")
    rid = f"finetune-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        pos = po_examples(cfg)
        train = [e for e in pos if not e["test"]]
        tests = test_set(t, pos)
        px = int(t["ft_px"])
        cur = po_settings(cfg)
        if resume:
            work = resumable(cfg)
            if not work:
                return job.finish("skipped", "no finished fine-tuning run waiting to be imported and tested")
            rid = work.name
            n = sum(1 for _ in open(work / "data" / "train.jsonl", encoding="utf-8")) if (work / "data" / "train.jsonl").exists() else len(train)
            job.log(f"finishing {rid}: fine-tuned on {n} orders; importing and testing on {len(tests)} test orders")
            train = train[:n] if n <= len(train) else train
        else:
            py = Path(t["venv"], "bin", "python")
            if not py.exists():
                return job.finish("skipped", f"training environment not installed ({py}): run setup_training.sh first")
            job.log(f"{len(pos)} verified orders: {len(train)} to train on, {len(tests)} to test")
            if len(pos) < t["finetune_min_verified"] and not force:
                return job.finish("skipped", f"needs {t['finetune_min_verified']} checked orders (has {len(pos)})")
            if len(tests) < 8:
                return job.finish("skipped", f"needs at least 8 checked test orders (has {len(tests)})")
            work = tdir(cfg) / "finetune" / rid
            data = work / "data"
            (data / "images").mkdir(parents=True, exist_ok=True)
            job.step("building the training set", len(train))
            with open(data / "train.jsonl", "w", encoding="utf-8") as fh:
                for e in train:
                    img = data / "images" / f"{e['hash'][:16]}.png"
                    if not img.exists():
                        render_png(e["pdf"], img, px)
                    fh.write(json.dumps({"image": f"images/{img.name}", "prompt": po_reader.PROMPT,
                                         "answer": json.dumps(to_answer(e["verified"]), ensure_ascii=False)}, ensure_ascii=False) + "\n")
                    job.tick()
            update_verified_suppliers(cfg, job)
            job.pause_worker("fine-tuning on the GPU")
            unload_ollama(cfg, job)
            job.step("fine-tuning (this takes hours; progress in the log)")
            cmd = [str(py), str(HERE / "finetune_unsloth.py"), "--data", str(data), "--out", str(work), "--base", t["base_model"],
                   "--epochs", str(t["ft_epochs"]), "--rank", str(t["ft_rank"])]
            with open(job.log_path, "a", encoding="utf-8") as fh:
                r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=dict(os.environ, HF_HOME=t.get("hf_home", "/srv/ocr/hf-cache")))
            if r.returncode != 0 or not (work / "merged").exists():
                return job.finish("failed", f"fine-tuning stopped (exit {r.returncode}); see the log")
        po_reader.EXTRA_VOCAB = str(tdir(cfg) / "suppliers_verified.json")
        name = f"po-reader-ft-{rid.split('-')[1]}"
        job.step(f"importing into Ollama as {name}")
        if not ollama_import(t, job, name, work):
            return job.finish("failed", "Ollama could not import the fine-tuned model (see the log); the current setup is "
                                        "unchanged. Fix it, then 'Finish the last full training' runs only the import and the test")
        job.pause_worker("A/B scoring")
        a = evaluate(job, cfg, tests, cur["model"], cur["fewshot_dir"], cur["px"], "current setup")
        b1 = evaluate(job, cfg, tests, name, None, px, "fine-tuned model")
        po_reader._FEW.clear()
        b2 = evaluate(job, cfg, tests, name, cur["fewshot_dir"], px, "fine-tuned model + worked examples")
        b = b2 if (b2["overall"] or 0) > (b1["overall"] or 0) else b1
        valid, invalid_why = test_valid(a, b, len(tests))
        ok, why = better(b, a, t["min_gain"], t.get("focus")) if valid else (False, invalid_why)
        report = {"id": rid, "kind": "finetune", "at": datetime.now().isoformat(timespec="seconds"), "trained_on": len(train),
                  "tests": len(tests), "model": name, "current": a, "candidate": b, "candidate_alt": b2 if b is b1 else b1,
                  "installed": ok, "valid": valid, "why": why}
        if ok:
            install(cfg, job, {"model": name, "fewshot_dir": b["fewshot_dir"] or str(tdir(cfg) / "no_examples"), "px": px}, report)
            (tdir(cfg) / "no_examples").mkdir(exist_ok=True)
        write_json(tdir(cfg) / "reports" / f"{rid}.json", report)
        job.finish("done", ("installed " + name + ": " if ok else "kept the current setup: ") + why)
    except Exception as ex:
        job.finish("failed", f"{type(ex).__name__}: {ex}")
        raise


def unload_ollama(cfg, job):
    """Free the GPU: ask Ollama to unload every model it has loaded."""
    base = cfg["ollama_url"].rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/ps", timeout=10) as r:
            models = [m["name"] for m in json.load(r).get("models", [])]
        for m in models:
            req = urllib.request.Request(base + "/api/generate", data=json.dumps({"model": m, "keep_alive": 0}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).read()
        job.log(f"unloaded from the GPU: {', '.join(models) or 'nothing loaded'}")
        time.sleep(5)
    except Exception as ex:
        job.log(f"could not unload Ollama models ({ex}); continuing")


def cmd_revert(cfg):
    d = tdir(cfg)
    hist = sorted((d / "history").glob("active-*.json"))
    if hist:
        write_json(d / "active.json", read_json(hist[-1]))
        hist[-1].unlink()
        print("reverted to", json.dumps(read_json(d / "active.json")))
    else:
        (d / "active.json").unlink(missing_ok=True)
        print("reverted to the settings in config.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "quick", "finetune", "revert", "suppliers"])
    ap.add_argument("--force", action="store_true", help="run even below the minimum number of examples")
    ap.add_argument("--resume", action="store_true", help="finetune: import + test the last run that finished training")
    a = ap.parse_args()
    cfg, t = load_cfg()
    if a.command == "status":
        cmd_status(cfg, t)
    elif a.command == "suppliers":
        print(update_verified_suppliers(cfg), "verified supplier names")
    elif a.command == "quick":
        cmd_quick(cfg, t, a.force)
    elif a.command == "finetune":
        cmd_finetune(cfg, t, a.force, a.resume)
    elif a.command == "revert":
        cmd_revert(cfg)


if __name__ == "__main__":
    main()

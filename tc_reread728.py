#!/usr/bin/env python3
"""728 (Dino 14 Nov: "On a pass, re-read the 28 sources and update their SharePoint items in place" - "Yes, update in place").
The 28 Test Cert sources of the 13 Nov re-reads (the four 12 Nov scans, the 24 "<batch> unread pages.pdf" extracts) were
uploaded on 14 Nov as the 10 Nov reader read them. Once the new reader is installed (tc_reader.py = the build the 727 test
passed), each source is read again with it INTO A STAGING FOLDER:
    work/tc_reread728/<source>/   one PDF + sidecar per test certificate, written the way the worker writes them
Nothing goes to out/, synced/ or SharePoint, state.db is not touched, the originals stay in done/. The staged documents
are matched to the SharePoint items afterwards (tc_rematch728.py) and written only after Dino has seen the matching.

Run by mps-tc-reread728.timer every 10 minutes; does nothing until the new reader is live, and nothing once every source
is staged. The worker is paused while a source is read (its own PAUSE, after the worker's current page). Between
21:50 and 22:25 the pause is given up for the nightly Ronin JT shadow pass. work/tc_reread728/STOP ends it after the
page being read.
730 (Dino 16 Nov: "these aren't critical ... run from 21:00 to 03:00 the next day until they are finished and leave the
worker open during the day"): it reads only between 21:00 and 03:00 and gives the worker back at 03:00. Every page
answer is kept in work/tc_reread728/_cache/<source>/, so a source cut off at 03:00 goes on from its next page the
following night instead of from its first.
    tc_reread728.py [--dry] [--test "<source>.pdf"]      (--test: that source only, with the reader that is live, into
                                                         work/tc_reread728_test/ - a check of this script)"""
import atexit, glob, hashlib, json, os, signal, subprocess, sys, tempfile, time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("MPS_OCR_CONFIG", "/srv/ocr/pipeline/config.json")
sys.path.insert(0, "/srv/ocr/pipeline")
P = Path("/srv/ocr/pipeline")
R = Path("/srv/ocr/scans/pipeline")
ST = R / "work" / "tc_reread728"
LOG = R / "reports" / "tc reread 728.log"
NEW_MD5 = "b2cf432b"                                    # the reader the 727 test passed (first 8 of its md5)
MINE = "tc reread 728"
dry = "--dry" in sys.argv
test = sys.argv[sys.argv.index("--test") + 1] if "--test" in sys.argv[:-1] else None     # one source, the live reader, its own folder
if test:
    ST = R / "work" / "tc_reread728_test"


def night(now=None):
    """730: 21:00 - 03:00, the hours the re-read may hold the worker."""
    t = (now or datetime.now())
    return t.hour >= 21 or t.hour < 3


def say(*a):
    line = f"{datetime.now():%F %T} " + " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


names = [b + ".pdf" for b in ("DOC121126-12112026155725", "DOC121126-12112026161857", "DOC121126-12112026171932",
                               "DOC121126-12112026173303")]
names += sorted(Path(p).name[:-5] for p in glob.glob(str(R / "work/tc_unread718/*.json")))
if test:
    names = [test]
todo = [n for n in names if not (ST / n[:-4] / "_done.json").is_file()]
if not dry and not test:
    if (ST / "DONE").is_file() or not todo:
        sys.exit(0)
    if md5(P / "tc_reader.py")[:8] != NEW_MD5:           # the test has not passed (yet): nothing to do
        sys.exit(0)
    if (ST / "STOP").is_file():
        sys.exit(0)
    if not night():                                     # 730: the worker is left alone from 03:00 to 21:00
        sys.exit(0)
ST.mkdir(parents=True, exist_ok=True)
READER_MD5 = md5(P / "tc_reader.py")

import worker as W                                      # noqa: E402  (the pipeline's own reading, splitting and naming)
T = W.tc_reader
cfg = W.load_config()
w = object.__new__(W.Worker)                            # no state.db, no work/ recovery: only the Test Cert methods are used
w.cfg, w.root, w.types, w.lane = cfg, Path(cfg["root"]), cfg["doc_types"], "all"
tcfg = w.types["TestCerts"]


# ------------------------------------------------------------------------------------------------ the worker's pause
def pause_text():
    try:
        return (R / "PAUSE").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def release():
    if (pause_text() or "").startswith(MINE):
        (R / "PAUSE").unlink(missing_ok=True)
        say("worker resumed")


def idle():
    """as tc_chem_test5.sh: no current file, or the worker asleep between pages (three looks, 4 s apart)."""
    try:
        if json.load(open(R / "status.json")).get("current") is None:
            return True
    except Exception:
        pass
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "mps-ocr-worker"], capture_output=True, text=True).stdout.strip()
    for _ in range(3):
        try:
            if Path(f"/proc/{pid}/wchan").read_text().strip() != "hrtimer_nanosleep":
                return False
        except OSError:
            return False
        time.sleep(4)
    return True


def busy_elsewhere():
    return any(subprocess.run(["systemctl", "is-active", "--quiet", s + ".service"]).returncode == 0
               for s in ("mps-ocr-training", "mps-numbers-redo", "mps-numbers-weekly", "mps-rjt-shadow", "mps-weekend-train",
                         "mps-after-train", "mps-ab-finish"))


def pause():
    """Waits for anyone else's pause to end (up to 3 h), then pauses the worker and waits for it to finish its page."""
    t0 = time.time()
    while True:
        txt = pause_text()
        if txt is None and not busy_elsewhere():
            break
        if txt is not None and txt.startswith(MINE):
            break
        if time.time() - t0 > 3 * 3600:
            say("the worker stayed paused by another job for 3 h:", (txt or "a training job")[:80], "- trying again at the next timer run")
            sys.exit(0)
        time.sleep(30)
    (R / "PAUSE").write_text(f"{MINE} (Dino 14 Nov: re-read the 28 Test Cert sources with the new reader, staged only)\n", encoding="utf-8")
    for _ in range(360):
        if idle():
            say("worker paused and idle")
            return
        time.sleep(5)
    release()
    say("worker still on a page after 30 min - trying again at the next timer run")
    sys.exit(0)


atexit.register(release)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
signal.signal(signal.SIGINT, lambda *_: sys.exit(1))


def quiet_window(now=None):
    """21:50 - 22:25: the Ronin JT shadow pass (21:57, ends by 22:20) skips its reading when PAUSE is set."""
    t = (now or datetime.now())
    return (21, 50) <= (t.hour, t.minute) < (22, 25)


class Stop(BaseException):                              # not an Exception: tc_reader.read() records those as an unread page and goes on
    pass


_read_page = T.read_page


CUR = {"stem": None, "cached": 0, "read": 0}


def _cache_file(a, k):
    """730: one file per page reading - everything that decides the answer except the file's path and the timeouts."""
    a = list(a) + [None] * 10
    what = {"page": a[1], "model": a[3], "few": a[4], "family": a[5], "px": a[6], "hint": hashlib.sha256(str(a[7] or "").encode()).hexdigest(),
            "kw": {x: k[x] for x in sorted(k) if x not in ("timeout", "keep_alive")}, "reader": READER_MD5}
    h = hashlib.sha256(json.dumps(what, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return ST / "_cache" / CUR["stem"] / f"p{int(a[1] or 0):04d}-{h}.json"


def read_page(*a, **k):                                 # between two pages: STOP, the night hours, the shadow pass's window
    cf = _cache_file(a, k) if CUR["stem"] and not test else None
    if cf is not None and cf.is_file():                 # read on an earlier night
        CUR["cached"] += 1
        return json.loads(cf.read_text(encoding="utf-8"))["answer"]
    if (ST / "STOP").is_file():
        raise Stop("work/tc_reread728/STOP")
    if not test and not night():
        raise Stop("03:00 - goes on at 21:00")
    if quiet_window():
        release()
        say("pause given up for the Ronin JT shadow pass until 22:25")
        while quiet_window():
            time.sleep(30)
        pause()
    r = _read_page(*a, **k)
    CUR["read"] += 1
    if cf is not None:
        try:
            cf.parent.mkdir(parents=True, exist_ok=True)
            tmp = cf.with_suffix(".tmp")
            tmp.write_text(json.dumps({"answer": r}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, cf)
        except (TypeError, ValueError, OSError):        # an answer that cannot be stored is simply read again
            pass
    return r


T.read_page = read_page


# ------------------------------------------------------------------------------------------------ one source
def stage(name, accept_errors=False):
    stem = name[:-4]
    srcs = sorted(glob.glob(str(R / "done/TestCerts/*" / glob.escape(name))), key=os.path.getmtime)
    if not srcs:
        say(f"{name}: original not in done/TestCerts - left")
        return None
    src = Path(srcs[-1])
    digest = sha(src)
    out_dir = ST / stem
    if out_dir.is_dir():                                # a source that was stopped part-way: start it again clean
        for p in out_dir.iterdir():
            p.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    docs = []
    CUR.update(stem=stem, cached=0, read=0)
    with tempfile.TemporaryDirectory() as td:
        staged = w.read_tc("TestCerts", src, Path(name), {}, digest, [], Path(td))
        bad = sum(1 for *_, extra in staged for q in extra.get("tc_pages") or [] if q.get("how") == "error")
        if bad and not accept_errors:                   # 730: the pages that were read are kept; only the failed ones are read again
            raise RuntimeError(f"{bad} page(s) could not be read")
        for page, part, fields, flags, doc_id, extractor, npages, secs, extra in staged:
            stem_out = w.output_stem("test_cert", fields, Path(name), page)
            hold_reason = w.hold_reason("TestCerts", "test_cert", fields, flags)
            pdf_dst = W.unique_path(out_dir, stem_out, ".pdf")
            if hold_reason:
                flags = [f for f in flags if f != "held"] + ["held: " + hold_reason]
            W.shutil.copy2(part, pdf_dst)
            sidecar = {"schema_version": 1, "doc_id": doc_id, "doc_type": "test_cert", "folder": "TestCerts", "file": pdf_dst.name,
                       "source_file": name, "source_sha256": digest, "source_page": page, "pages_read": npages,
                       "processed_at": W.now_iso(), "seconds": round(secs, 1), "model": tcfg.get("model", cfg.get("model")),
                       "extractor": extractor, **({"hold_reason": hold_reason} if hold_reason else {}), **extra,
                       "needs_review": bool(flags), "flags": flags, "fields": fields}
            sidecar["model"] = sidecar.pop("tc_model", sidecar.get("model"))
            sidecar["reader"] = "tc_reader (page by page, no worked examples)"
            sidecar["reread_728"] = {"at": W.now_iso(), "reader_md5": md5(P / "tc_reader.py"), "staged_only": True}
            W.atomic_write_json(pdf_dst.with_suffix(".json"), sidecar)
            docs.append({"file": pdf_dst.name, "cert_no": fields.get("cert_no"), "held": hold_reason, "pages": npages,
                         "split_from": fields.get("split_from"), "items": fields.get("item_count"), "heats": fields.get("heat_count")})
    W.atomic_write_json(out_dir / "_done.json", {"source": name, "source_path": str(src.relative_to(R)), "source_sha256": digest,
                                                 "seconds": round(time.time() - t0), "documents": docs, "at": W.now_iso(),
                                                 "reader_md5": md5(P / "tc_reader.py"), "pages_read_now": CUR["read"],
                                                 "pages_from_earlier_nights": CUR["cached"]})
    say(f"{name}: {len(docs)} documents staged ({sum(1 for d in docs if d['held'])} would be held) in {time.time() - t0:.0f} s "
        f"({CUR['read']} page readings now, {CUR['cached']} kept from earlier)")
    return len(docs)


if dry:
    for n in names:
        hit = glob.glob(str(R / "done/TestCerts/*" / glob.escape(n)))
        print(n, "staged" if n not in todo else "to read", "| original", "found" if hit else "MISSING")
    print(f"{len(todo)} of {len(names)} to read; live reader {md5(P / 'tc_reader.py')[:8]} (reads start when it is {NEW_MD5}); "
          f"worker methods ok: {all(hasattr(w, m) for m in ('read_tc', 'output_stem', 'hold_reason', 'tc_document', 'tc_settings'))}; "
          f"settings {w.tc_settings('TestCerts')}; night now: {night()}; cached page readings: {len(glob.glob(str(ST / '_cache/*/*.json')))}")
    sys.exit(0)

say(f"re-read starts: {len(todo)} of {len(names)} sources to read with tc_reader {md5(P / 'tc_reader.py')[:8]}")
fails = json.loads((ST / "_fails.json").read_text()) if (ST / "_fails.json").is_file() else {}
pause()
try:
    for name in todo:
        if fails.get(name, 0) >= 4:
            continue
        try:
            stage(name, accept_errors=fails.get(name, 0) >= 2)     # a page that fails twice is staged as unread (flagged)
        except Stop as e:
            say(f"stopped at {name}: {e} ({CUR['read']} page readings this run, kept)")
            break
        except Exception as e:                          # one bad source does not end the run; four tries each
            fails[name] = fails.get(name, 0) + 1
            (ST / "_fails.json").write_text(json.dumps(fails), encoding="utf-8")
            say(f"{name}: FAILED ({fails[name]}): {str(e)[:300]}")
            if not W.ollama_up(cfg):
                say("Ollama is not answering - trying again at the next timer run")
                break
finally:
    release()
left = [n for n in names if not (ST / n[:-4] / "_done.json").is_file()]
if not [n for n in left if fails.get(n, 0) < 4] and not (ST / "STOP").is_file():
    (ST / "DONE").write_text(f"{datetime.now():%F %T} staged {len(names) - len(left)} of {len(names)}; failed twice: {left}\n", encoding="utf-8")
    say(f"all sources staged ({len(names) - len(left)} of {len(names)}); failed twice: {left or 'none'}")

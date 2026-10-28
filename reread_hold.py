#!/usr/bin/env python3
"""Night re-read of the purchase / Polymer orders on hold (Dino 28 Oct 17:34): every order on hold that nobody has
checked or edited is read again by the current reader (after tonight's training), a few at a time at night, so the
day's batch is never held up.

    python3 reread_hold.py plan                 # what would be re-read (count per hold reason) -> reports/hold re-read plan <stamp>.csv
    python3 reread_hold.py tick [--batch 30]    # what mps-po-reread.timer runs every 15 min
    python3 reread_hold.py report               # old reading vs new reading of everything re-read so far

tick, inside the night window (19:00-02:15) only, when no training job is running and the worker's Purchase Orders
queue holds nothing but re-reads: tops in/PurchaseOrders up to --batch re-read files. Each one:
  - the hold PDF is copied to in/PurchaseOrders/<original name>.pdf with a _meta note (original name, original folder,
    "reread_of"); worker.py (re-read patch) reads it even though its content was read before. Nothing is fed unless the
    installed worker.py has the patch and the worker service was started after it was installed.
  - the hold copy goes to deleted/<Type>/<yyyy-mm>/ (recoverable, reason "re-read") - the new reading replaces it
  - logged in work/reread/fed.csv (never fed twice)
The first tick after the window closes runs po_autocheck (queue, then hold) so clear re-read orders are checked against
the buying spreadsheet and released, as the 19:30 import does. When nothing is left the timer disables itself.
Left alone: checked orders, orders edited by a person on the dashboard (edits by the scripts - po_autocheck --park /
release, route_sort tags - don't count), uploaded ones, documents that were not read as an order (the sorter's labelling
sheet covers those), files being written, locked sidecars.
Blind review 28 Oct 18:50: no RR- prefix (the reader takes the supplier from the file name), script edits ignored,
worker patch checked before feeding, the ledger row written before the hold copy is removed, autocheck --no-reader-only.
"""
import argparse, csv, glob, json, os, re, shutil, subprocess, sys, time
from collections import Counter
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                     # noqa: E402
import route_sort                                       # noqa: E402  remove() -> deleted/<Type>/<yyyy-mm>/

ROOT = Path("/srv/ocr/scans/pipeline")
P = Path("/srv/ocr/pipeline")
W = ROOT / "work" / "reread"
FED = W / "fed.csv"
STATE = W / "state.json"
IN = ROOT / "in" / "PurchaseOrders"
TYPES = ("PurchaseOrders", "PolymerOrders")
ORDER_FORMS = {"mps_order", "polymer_order", "internal_group_transfer"}
WINDOW = (dtime(19, 0), dtime(2, 15))                   # night: 19:00 -> 02:15 (the 03:00 feed follows)
FIELDS = ["at", "type", "file", "source_file", "content_hash", "doc_id", "order_number", "supplier", "hold_reason", "fed_as"]
SCRIPT_EDIT = re.compile(r"^(po_autocheck|route_sort|hold_resort|od_sort|requeue_po12|numbers_pass|fix_)", re.I)


def human_edits(sc):
    return [e for e in (sc.get("edits") or []) if not SCRIPT_EDIT.match(str(e.get("by") or ""))]


def worker_ready():
    """The installed worker.py has the re-read patch and the worker was (re)started after it was installed."""
    w = P / "worker.py"
    try:
        if "reread_of" not in w.read_text(encoding="utf-8"):
            return False, "worker.py has no re-read patch"
        r = subprocess.run(["systemctl", "show", "mps-ocr-worker.service", "-p", "ActiveEnterTimestampMonotonic", "-p", "ActiveState"],
                           capture_output=True, text=True)
        kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        if kv.get("ActiveState") != "active":
            return False, f"worker is {kv.get('ActiveState')}"
        up = time.clock_gettime(time.CLOCK_MONOTONIC) - int(kv.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
        if time.time() - up < w.stat().st_mtime:
            return False, "worker started before the patched worker.py: restart it"
    except Exception as e:
        return False, f"cannot check the worker: {e}"
    return True, "ok"


def in_window(now=None):
    t = (now or datetime.now()).time()
    a, b = WINDOW
    return t >= a or t < b


def fed():
    if not FED.exists():
        return {}
    return {r["content_hash"] or r["doc_id"]: r for r in csv.DictReader(open(FED, encoding="utf-8"))}


def candidates(done):
    out = []
    for t in TYPES:
        for js in sorted(glob.glob(str(ROOT / "out" / t / "hold" / "*.json"))):
            js = Path(js)
            if js.name.endswith((".tmp", ".lock")):
                continue
            try:
                sc = json.load(open(js, encoding="utf-8"))
                age = datetime.now().timestamp() - js.stat().st_mtime
            except (OSError, ValueError):
                continue
            f = sc.get("fields") or {}
            if sc.get("doc_type") != "po" or f.get("form") not in ORDER_FORMS or not f.get("order_number"):
                continue
            if f.get("checked") or human_edits(sc) or Path(str(js) + ".uploaded").exists() or age < 600:
                continue
            key = str(f.get("content_hash") or "").upper() or sc.get("doc_id")
            if key in done:
                continue
            pdf = js.parent / str(sc.get("file") or "")
            if pdf.is_file():
                out.append((t, js, sc, pdf, key))
    # newest scans first: the ones read before the latest reader changes are all old; this keeps 2025/2026 first
    out.sort(key=lambda c: str(c[2].get("source_modified") or c[2].get("processed_at") or ""), reverse=True)
    return out


def reason_of(sc):
    return re.sub(r"\d", "#", str(sc.get("hold_reason") or "")).strip()[:70] or "(none)"


def plan(a):
    c = candidates(fed())
    cnt = Counter(reason_of(sc) for _, _, sc, _, _ in c)
    p = ROOT / "reports" / f"hold re-read plan {datetime.now():%Y-%m-%d %H%M}.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["type", "file", "source_file", "order_number", "supplier", "hold_reason", "source_modified"])
        for t, js, sc, pdf, _ in c:
            f = sc.get("fields") or {}
            w.writerow([t, pdf.name, sc.get("source_file"), f.get("order_number"), f.get("supplier"), sc.get("hold_reason"),
                        sc.get("source_modified")])
    print(json.dumps({"to_re_read": len(c), "by_type": dict(Counter(t for t, *_ in c)),
                      "by_hold_reason": dict(cnt.most_common(25)), "already_fed": len(fed()), "list": str(p)}, indent=1))


def training_running():
    try:
        j = json.loads((ROOT / "training" / "job.json").read_text(encoding="utf-8"))
        if j.get("state") == "running":
            os.kill(int(j.get("pid")), 0)
            return True
    except (OSError, ValueError, TypeError):
        pass
    return subprocess.run(["systemctl", "is-active", "--quiet", "mps-train-now.service"]).returncode == 0


def pending_rereads():
    """(re-reads waiting or being read, other files waiting). A re-read is known by its _meta note ("reread_of"), which the
    worker deletes when the file is done."""
    md = IN / "_meta"
    rr = set()
    for m in md.glob("*.json"):
        try:
            if "reread_of" in json.load(open(m, encoding="utf-8")):
                rr.add(m.name[:-5])
        except (OSError, ValueError):
            pass
    q = [p.name for p in IN.glob("*.pdf")]
    return len(rr), sum(1 for n in q if n not in rr)


def state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(s):
    W.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name("state.json.tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def autocheck(s):
    """After a night's re-reads: clear orders checked against the spreadsheet (queue), clear / checked ones released (hold)."""
    for args in (["--apply", "--no-reader-only"], ["--hold", "--apply", "--no-reader-only"]):
        r = subprocess.run([sys.executable, str(P / "po_autocheck.py")] + args, capture_output=True, text=True, cwd=str(P))
        tail = (r.stdout or "")[-1500:]
        print(f"po_autocheck {' '.join(args)} rc={r.returncode}\n{tail}")
        s.setdefault("autocheck", []).append({"at": datetime.now().isoformat(timespec="seconds"), "args": args, "rc": r.returncode})
    s["autocheck_due"] = False


def tick(a):
    s = state()
    now = datetime.now()
    if not in_window(now):
        if s.get("autocheck_due"):
            n, _ = pending_rereads()
            if n == 0 or now.time() >= dtime(2, 50):
                autocheck(s)
                save_state(s)
        print("outside the night window")
        return
    if training_running():
        print("training is running: no re-reads now")
        return
    ok, why = worker_ready()
    if not ok:
        print(f"not feeding: {why}")
        return
    n_rr, n_other = pending_rereads()
    if n_other:
        print(f"{n_other} other files waiting in the Purchase Orders queue: they go first")
        return
    room = a.batch - n_rr
    if room <= 0:
        print(f"{n_rr} re-reads still waiting / being read")
        return
    done = fed()
    c = candidates(done)
    if not c and n_rr == 0:
        print("nothing left to re-read: disabling mps-po-reread.timer")
        if s.get("autocheck_due"):
            autocheck(s)
        s["finished_at"] = now.isoformat(timespec="seconds")
        save_state(s)
        subprocess.run(["sudo", "-n", "systemctl", "disable", "mps-po-reread.timer"])
        subprocess.run(["sudo", "-n", "systemctl", "stop", "mps-po-reread.timer"])
        return
    W.mkdir(parents=True, exist_ok=True)
    md = IN / "_meta"
    md.mkdir(parents=True, exist_ok=True)
    new = not FED.exists()
    stamp = now.isoformat(timespec="seconds")
    moved = 0
    with open(FED, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for t, js, sc, pdf, key in c:
            if moved >= room:
                break
            try:
                with sidecar_lock.locked(js, "reread_hold", wait=2):
                    if not js.exists() or not pdf.exists():
                        continue
                    sc = json.load(open(js, encoding="utf-8"))
                    f = sc.get("fields") or {}
                    if f.get("checked") or human_edits(sc):
                        continue
                    stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(str(sc.get("source_file") or pdf.name)).stem)[:100]
                    page = sc.get("source_page")
                    name, k = f"{stem}{f'_p{page}' if page else ''}.pdf", 2
                    while (IN / name).exists() or (md / (name + ".json")).exists() or list((ROOT / "work").glob(f"PurchaseOrders__{name}")):
                        name = f"{stem}{f'_p{page}' if page else ''}_{k}.pdf"
                        k += 1
                    note = {"original_name": sc.get("source_file") or pdf.name, "reread_of": f"out/{t}/hold/{pdf.name}",
                            "reread_at": stamp, "previous_doc_id": sc.get("doc_id"), "previous_hold_reason": sc.get("hold_reason")}
                    for k2 in ("source_folder", "source_modified"):
                        if sc.get(k2):
                            note[k2] = sc[k2]
                    if sc.get("source_page"):
                        note["source_page"] = sc["source_page"]
                    w.writerow({"at": stamp, "type": t, "file": pdf.name, "source_file": sc.get("source_file"), "content_hash": key,
                                "doc_id": sc.get("doc_id"), "order_number": f.get("order_number"), "supplier": f.get("supplier"),
                                "hold_reason": sc.get("hold_reason"), "fed_as": name})
                    fh.flush()                                   # the ledger first: never fed twice, whatever happens next
                    (md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
                    shutil.copy2(pdf, IN / (name + ".part"))
                    try:
                        route_sort.remove(js, sc, t, f"re-read by the reader (night re-read {now:%d %b}): the new reading replaces "
                                                     f"this one; fed as in/PurchaseOrders/{name}", stamp)
                    except Exception as e:                       # hold copy stays: take the feed back, say so
                        (IN / (name + ".part")).unlink(missing_ok=True)
                        (md / (name + ".json")).unlink(missing_ok=True)
                        print(f"could not move {pdf.name} to deleted/ ({e}): not fed (still on hold; in fed.csv - remove the row to retry)")
                        continue
                    os.replace(IN / (name + ".part"), IN / name)   # the worker only sees it now, after the hold copy is gone
                    moved += 1
            except sidecar_lock.Locked:
                continue
    s["autocheck_due"] = s.get("autocheck_due") or moved > 0
    s["fed_total"] = int(s.get("fed_total") or 0) + moved
    s["last_tick"] = stamp
    s["left"] = max(len(c) - moved, 0)
    save_state(s)
    print(f"fed {moved} re-reads ({n_rr} already waiting); about {s['left']} left")


def report(a):
    rows = list(fed().values())
    by_hash = {}
    for pat, where in (("out/{t}/*.json", "queue"), ("out/{t}/hold/*.json", "hold"), ("synced/{t}/*/*.json", "synced")):
        for t in TYPES:
            for js in glob.glob(str(ROOT / pat.format(t=t))):
                try:
                    sc = json.load(open(js, encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                h = str((sc.get("fields") or {}).get("content_hash") or "").upper()
                if h:
                    by_hash[h] = (where, t, sc)
    out, cnt = [], Counter()
    for r in rows:
        hit = by_hash.get(str(r["content_hash"]).upper())
        if not hit:
            cnt["not read yet / elsewhere"] += 1
            out.append(dict(r, now="not read yet / elsewhere"))
            continue
        where, t, sc = hit
        f = sc.get("fields") or {}
        st = f"{where}{' checked' if f.get('checked') else ''}"
        cnt[st] += 1
        out.append(dict(r, now=st, new_order_number=f.get("order_number"), new_supplier=f.get("supplier"),
                        new_hold_reason=sc.get("hold_reason"), order_changed=str(f.get("order_number")) != str(r["order_number"]),
                        supplier_changed=str(f.get("supplier")) != str(r["supplier"])))
    p = ROOT / "reports" / f"hold re-read {datetime.now():%Y-%m-%d %H%M}"
    if out:
        keys = list(dict.fromkeys(k for o in out for k in o))
        with open(f"{p}.csv", "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(out)
    summ = {"re_read": len(rows), "now": dict(cnt.most_common()), "state": state(), "report": f"{p}.csv"}
    json.dump(summ, open(f"{p}.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("plan")
    t = sp.add_parser("tick")
    t.add_argument("--batch", type=int, default=30)
    sp.add_parser("report")
    a = ap.parse_args()
    {"plan": plan, "tick": tick, "report": report}[a.cmd](a)

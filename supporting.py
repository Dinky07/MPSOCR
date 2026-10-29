#!/usr/bin/env python3
"""Supporting pages behind orders (Dino 29 Oct 08:07): a scan in the order queues is one document around its order form,
the other pages ride behind it (worker group_pages, regroup_scans, coalesce_hold). Those pages are still documents of
their own - a job transfer, a supplier invoice, a proof of payment, a test cert - so:

 1. harvest   every supporting page is recorded (box: work/supporting/supporting.db; MPSBusiness docs.SupportingPage
              through the PC sync's outbox), tagged unverified: the order it is behind, its page, what the pre-sort /
              reader made of it, and the keys the CPU text gives (JT / invoice / IN / order numbers, supplier VAT).
 2. read      at night, each supporting page whose type has a reader (JT, invoice, proof of payment, workshop sheet,
              test cert) is read by that reader - fed to in/<Type> with a _meta note "supporting_of"; the worker reads it
              and writes the result to work/supporting/results/ instead of publishing it (nothing goes to SharePoint).
 3. match     the same document arriving through its own channel (JT folder, invoice archive, PoP library, test certs...)
              is looked up by its key: agreement / disagreement per field is written to that document's sidecar
              ("second_source"), a disagreement becomes a check on it; and both ways (a page read later finds a document
              that came earlier). Two copies of the same paper read by the same model share mistakes: agreement is
              recorded, never used to tick Checked on its own.
 Every change to a page record goes to work/supporting/outbox/ (one JSON per change) for the PC sync to send to
 MPSBusiness.docs.usp_IngestSupportingPage.

    python3 supporting.py tick [--harvest-max 150] [--feed-max 20]   # what mps-supporting.timer runs (every 15 min)
    python3 supporting.py status
"""
import argparse, glob, hashlib, json, os, re, sqlite3, subprocess, sys, tempfile, time
from collections import Counter
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, "/srv/ocr/pipeline")
import sidecar_lock                                     # noqa: E402

ROOT = Path("/srv/ocr/scans/pipeline")
W = ROOT / "work" / "supporting"
DB = W / "supporting.db"
OUTBOX, RESULTS = W / "outbox", W / "results"
ORDER_TYPES = ("PurchaseOrders", "PolymerOrders", "RoninOrders")
FORMS = {"mps_order", "polymer_order", "internal_group_transfer", "ronin_order"}
# supporting-page kind -> the queue whose reader reads it (step 2) and whose documents it is matched against (step 3)
READER = {"job_transfer": "JobTransfers", "supplier_invoice": "SupplierInvoices", "invoice": "SupplierInvoices",
          "Supplier Invoice": "SupplierInvoices", "proof_of_payment": "ProofOfPayments", "Proof of Payment": "ProofOfPayments",
          "workshop_sheet": "WorkshopSheets", "test_cert": "TestCerts"}
OWN_TYPES = ("JobTransfers", "SupplierInvoices", "SupplierInvoicesArchive", "ProofOfPayments", "WorkshopSheets", "TestCerts", "DeliveryNotes")
OWN_VAT = {"4320123039", "4240207755", "4570132524"}         # MPS, Polymer, Ronin: never a supplier's VAT number
WINDOW = (dtime(19, 0), dtime(5, 30))                         # night: supporting reads only then
COMPARE = {"JobTransfers": ("date", "job_number", "from", "to", "line_count"),
           "SupplierInvoices": ("invoice_date", "total", "supplier_vat_no", "supplier_name"),
           "ProofOfPayments": ("date", "amount", "account_name", "bank_reference"),
           "WorkshopSheets": ("date", "job_number", "mps_dwg_no", "sheet_no", "stamped", "stamp_date"),
           "TestCerts": ("date", "mill", "mps_po", "ronin_po", "heats_text", "item_count"),
           "DeliveryNotes": ("date", "customer", "job_number")}


def now():
    return datetime.now().isoformat(timespec="seconds")


def db():
    W.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS pages (source_sha TEXT, source_page INT, source_file TEXT, source_folder TEXT,
        parent_type TEXT, parent_json TEXT, parent_file TEXT, parent_hash TEXT, parent_order TEXT, company TEXT,
        position INT, kind TEXT, kind_source TEXT, keys TEXT, page_sha TEXT, status TEXT, reader_type TEXT,
        read TEXT, read_at TEXT, match TEXT, matched_at TEXT, fed_as TEXT, updated_at TEXT,
        PRIMARY KEY (source_sha, source_page));
    CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, type TEXT, place TEXT, mtime REAL, keys TEXT, content_hash TEXT,
        source_sha TEXT, fields TEXT);
    CREATE INDEX IF NOT EXISTS ix_docs_type ON docs (type);
    CREATE TABLE IF NOT EXISTS dockeys (k TEXT, v TEXT, path TEXT);
    CREATE INDEX IF NOT EXISTS ix_dockeys ON dockeys (k, v);
    CREATE INDEX IF NOT EXISTS ix_dockeys_path ON dockeys (path);
    """)
    return c


def norm(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def text_keys(t):
    """Keys the CPU text of a page gives (unverified)."""
    T = re.sub(r"[ \t]+", " ", str(t or "").upper())
    k = {}
    m = re.search(r"\bJ\.?T\.?\s*(?:NO\.?|NUMBER)?\s*:?\s*-?\s*(\d{5})\b", T)
    if m:
        k["jt_number"] = m.group(1)
    m = re.search(r"\b(?:TAX\s+)?INVOICE\s*(?:NO|NUMBER|NR|#)\.?\s*:?\s*([A-Z]{0,5}\s?\d[\w/-]{2,16})", T)
    if m:
        k["invoice_number"] = norm(m.group(1))
    vats = [v for v in dict.fromkeys(re.findall(r"(?<!\d)(4\d{9})(?!\d)", T)) if v not in OWN_VAT]
    if vats:
        k["supplier_vat_no"] = vats[0]
    m = re.search(r"\bIN\s?0?(\d{5,6})\b", T)
    if m:
        k["dn_number"] = "IN" + m.group(1).zfill(6)
    refs = list(dict.fromkeys(re.findall(r"(?<![A-Z0-9])(A\s?\d{5}|PL\s?\d{4}|DF\s?\d{4})(?!\d)", T)))
    if refs:
        k["order_refs"] = [norm(r) for r in refs][:5]
    m = re.search(r"\b(?:CERT(?:IFICATE)?\.?\s*(?:NO|NUMBER)\.?)\s*:?\s*([A-Z0-9][\w/-]{3,20})", T)
    if m:
        k["cert_no"] = norm(m.group(1))
    return k


def page_pdf(parent_pdf, position, out):
    r = subprocess.run(["pdfseparate", "-f", str(position), "-l", str(position), str(parent_pdf), str(out)],
                       capture_output=True, timeout=120)
    return r.returncode == 0 and Path(out).exists()


def page_text(pdf):
    try:
        t = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, timeout=60).stdout.decode("utf8", "ignore")
        if len(re.sub(r"\s", "", t)) > 80:
            return t
    except Exception:
        pass
    try:
        import pop_reader
        return (pop_reader.ocr_pages(pdf, 1, dpi=200, upright=True) or [""])[0]
    except Exception:
        return ""


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def to_outbox(row):
    """One JSON per change for the PC sync (MPSBusiness.docs.usp_IngestSupportingPage)."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    rec = {k: row[k] for k in row.keys()}
    for k in ("keys", "read", "match"):
        try:
            rec[k] = json.loads(rec[k]) if rec.get(k) else None
        except ValueError:
            pass
    name = f"{row['source_sha'][:16]}_p{row['source_page']:03d}_{int(time.time() * 1000)}.json"
    tmp = OUTBOX / (name + ".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, OUTBOX / name)


def update(c, sha, page, **kv):
    kv["updated_at"] = now()
    c.execute(f"UPDATE pages SET {', '.join(k + '=?' for k in kv)} WHERE source_sha=? AND source_page=?", list(kv.values()) + [sha, page])
    row = c.execute("SELECT * FROM pages WHERE source_sha=? AND source_page=?", (sha, page)).fetchone()
    if row:
        to_outbox(row)


# ------------------------------------------------------------------------------------------------ 1. harvest
def supporting_of(sc):
    """[(page, position in the parent PDF, kind, values)] for a grouped / merged order document."""
    f = sc.get("fields") or {}
    order = [int(x) for x in (sc.get("grouped_pages") or []) if str(x).isdigit()]
    out = []
    if f.get("supporting"):
        if not order:
            order = [int(x) for x in re.findall(r"\d+", str(f.get("source_pages") or ""))]
        for s in f["supporting"]:
            p = s.get("page")
            if not str(p).isdigit():
                continue
            p = int(p)
            pos = order.index(p) + 1 if p in order else None
            out.append((p, pos, s.get("kind"), {k: v for k, v in s.items() if k not in ("page", "kind")}))
    elif sc.get("merged_from"):
        mf = sc["merged_from"]
        main = next((i for i, m in enumerate(mf) if m.get("order_number") and m.get("order_number") == f.get("order_number")), 0)
        for i, m in enumerate(mf):
            if i == main or not str(m.get("page")).isdigit():
                continue
            kind = m.get("doc_type") or (m.get("form") if m.get("form") not in (None, "", "not_an_order") else "other")
            out.append((int(m["page"]), i + 1, kind, {k: m[k] for k in ("order_number", "supplier") if m.get(k)}))
    return out


def harvest(c, limit):
    seen, new, keyed = 0, 0, 0
    for t in ORDER_TYPES:
        for pat, place in (("out/{t}/*.json", "queue"), ("out/{t}/hold/*.json", "hold"), ("synced/{t}/*/*.json", "synced")):
            for js in glob.glob(str(ROOT / pat.format(t=t))):
                if js.endswith((".tmp", ".lock")):
                    continue
                try:
                    sc = json.load(open(js, encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                sup = supporting_of(sc)
                if not sup:
                    continue
                f = sc.get("fields") or {}
                sha = str(sc.get("source_sha256") or "").lower()
                if not sha:
                    continue
                pdf = Path(js).parent / str(sc.get("file") or "")
                for page, pos, kind, vals in sup:
                    seen += 1
                    row = c.execute("SELECT * FROM pages WHERE source_sha=? AND source_page=?", (sha, page)).fetchone()
                    parent = dict(parent_type=t, parent_json=js, parent_file=sc.get("file"), parent_hash=f.get("content_hash"),
                                  parent_order=f.get("order_number"), company=f.get("company") or ("Ronin" if t == "RoninOrders" else "Polymer" if t == "PolymerOrders" else "MPS"),
                                  position=pos)
                    if row is None:
                        c.execute("INSERT INTO pages (source_sha, source_page, source_file, source_folder, kind, kind_source, keys, status, updated_at) "
                                  "VALUES (?,?,?,?,?,?,?,?,?)", (sha, page, sc.get("source_file"), sc.get("source_folder"), kind,
                                                                  "reader / pre-sort (scan)", json.dumps(vals), "unverified", now()))
                        update(c, sha, page, **parent)
                        new += 1
                    elif any(row[k] != v for k, v in parent.items()):
                        update(c, sha, page, **parent)              # regrouped / released / synced since
    # CPU keys for pages that have none yet (text layer or Tesseract on the page cut from its parent)
    for row in c.execute("SELECT * FROM pages WHERE (page_sha IS NULL OR IFNULL(kind_source, '') NOT LIKE 'pre-sort (page)%') "
                         "AND position IS NOT NULL LIMIT ?", (limit,)).fetchall():
        parent = Path(row["parent_json"]).parent / str(row["parent_file"] or "")
        if not parent.is_file():
            continue
        with tempfile.TemporaryDirectory() as td:
            pp = Path(td) / "p.pdf"
            if not page_pdf(parent, row["position"], pp):
                update(c, row["source_sha"], row["source_page"], page_sha="?")
                continue
            keys = json.loads(row["keys"] or "{}")
            text = page_text(pp)
            keys.update({k: v for k, v in text_keys(text).items() if k not in keys})
            # what the page is: the pre-sort on the page itself (the order reader only said "not an order" / Other)
            kind, ks = row["kind"], "pre-sort (page): unsure"
            try:
                import presort
                v = presort.verdict(pp, texts=[text] if text.strip() else None)
                if v.get("kind") not in (None, "unsure") and v.get("strength") in ("sure", "likely"):
                    kind, ks = v["kind"], f"pre-sort (page): {v['strength']}"
            except Exception as e:
                ks = f"pre-sort (page): error {str(e)[:60]}"
            if keys.get("jt_number") and str(kind).lower() in ("other", "none", "not an order", "unsure"):
                kind, ks = "job_transfer", "pre-sort (page): JT number in the text"
            update(c, row["source_sha"], row["source_page"], page_sha=sha256(pp), keys=json.dumps(keys), kind=kind, kind_source=ks)
            keyed += 1
    c.commit()
    return {"supporting_pages_seen": seen, "new": new, "keyed": keyed}


# ------------------------------------------------------------------------------------------------ 2. read (night)
def in_window(t=None):
    t = (t or datetime.now()).time()
    a, b = WINDOW
    return t >= a or t < b


def busy():
    """Training running, or the worker has other files waiting (supporting reads go last)."""
    try:
        j = json.loads((ROOT / "training" / "job.json").read_text(encoding="utf-8"))
        if j.get("state") == "running":
            os.kill(int(j.get("pid")), 0)
            return "training"
    except (OSError, ValueError, TypeError):
        pass
    for d in (ROOT / "in").iterdir():
        if not d.is_dir() or (ROOT / "flags" / f"process_off_{d.name}").exists():
            continue
        for f in d.glob("*.pdf"):
            if not f.name.startswith("SUP-"):
                return f"{d.name} has files waiting"
    return None


def feed(c, limit):
    if not in_window():
        return "outside the night window"
    why = busy()
    if why:
        return why
    out = sum(1 for d in (ROOT / "in").iterdir() if d.is_dir() for _ in d.glob("SUP-*.pdf"))
    room = limit - out
    if room <= 0:
        return f"{out} supporting reads waiting"
    fed = 0
    types = json.load(open("/srv/ocr/pipeline/config.json", encoding="utf-8"))["doc_types"]
    for row in c.execute("SELECT * FROM pages WHERE status='unverified' AND position IS NOT NULL ORDER BY updated_at").fetchall():
        if fed >= room:
            break
        t = READER.get(str(row["kind"]))
        if not t or t not in types:
            continue
        parent = Path(row["parent_json"]).parent / str(row["parent_file"] or "")
        if not parent.is_file():
            continue
        d = ROOT / "in" / t
        (d / "_meta").mkdir(parents=True, exist_ok=True)
        name = f"SUP-{row['source_sha'][:16]}-p{row['source_page']:03d}.pdf"
        if not page_pdf(parent, row["position"], d / (name + ".part")):
            continue
        note = {"original_name": row["source_file"], "reread_of": "supporting page", "routed_at": now(),
                "supporting_of": {"source_sha": row["source_sha"], "source_page": row["source_page"],
                                  "parent": f"{row['parent_type']}/{row['parent_file']}", "order": row["parent_order"]}}
        if row["source_folder"]:
            note["source_folder"] = row["source_folder"]
        (d / "_meta" / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
        os.replace(d / (name + ".part"), d / name)
        update(c, row["source_sha"], row["source_page"], status="fed", reader_type=t, fed_as=f"{t}/{name}")
        fed += 1
    c.commit()
    return f"fed {fed}"


def collect(c):
    n = 0
    for p in sorted(RESULTS.glob("*.json")):
        try:
            r = json.load(open(p, encoding="utf-8"))
            s = r["supporting_of"]
            docs = r.get("docs") or []
            best = docs[0] if docs else {}
            update(c, s["source_sha"], int(s["source_page"]), status="read", read_at=r.get("read_at") or now(),
                   reader_type=r.get("type"), read=json.dumps({"doc_type": best.get("doc_type"), "fields": best.get("fields"),
                                                              "flags": best.get("flags"), "extractor": best.get("extractor"),
                                                              "more": len(docs) - 1 if docs else 0}, default=str))
            (RESULTS / "done").mkdir(exist_ok=True)
            os.replace(p, RESULTS / "done" / p.name)
            n += 1
        except Exception as e:
            print(f"result {p.name} not taken: {e}")
    c.commit()
    return n


# ------------------------------------------------------------------------------------------------ 3. match
def own_keys(t, f):
    k = {}
    if t == "JobTransfers" and f.get("jt_number"):
        k["jt_number"] = norm(f["jt_number"])
    if t in ("SupplierInvoices", "SupplierInvoicesArchive") and f.get("invoice_number"):
        k["invoice_number"] = norm(f["invoice_number"])
        if f.get("supplier_vat_no"):
            k["supplier_vat_no"] = norm(f["supplier_vat_no"])
    if t == "ProofOfPayments" and f.get("amount") is not None:
        k["payment"] = f"{float(f['amount']):.2f}|{f.get('date')}"
    if t == "WorkshopSheets" and f.get("job_number") and (f.get("mps_dwg_no") or f.get("sheet_no")):
        k["ws"] = f"{norm(f['job_number'])}|{norm(f.get('mps_dwg_no'))}|{f.get('sheet_no')}"
    if t == "TestCerts" and f.get("cert_no"):
        k["cert_no"] = norm(f["cert_no"])
    if t == "DeliveryNotes" and f.get("dn_number"):
        k["dn_number"] = norm(f["dn_number"])
    return k


def index_docs(c):
    have = {r["path"]: r["mtime"] for r in c.execute("SELECT path, mtime FROM docs")}
    seen, changed = set(), 0
    for t in OWN_TYPES:
        for pat, place in (("out/{t}/*.json", "queue"), ("out/{t}/hold/*.json", "hold"), ("synced/{t}/*/*.json", "synced")):
            for js in glob.glob(str(ROOT / pat.format(t=t))):
                if js.endswith((".tmp", ".lock")) or "/SUP-" in js:
                    continue
                seen.add(js)
                try:
                    m = os.path.getmtime(js)
                except OSError:
                    continue
                if have.get(js) == m:
                    continue
                try:
                    sc = json.load(open(js, encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                f = sc.get("fields") or {}
                ct = "SupplierInvoices" if t == "SupplierInvoicesArchive" else t
                small = {k: f.get(k) for k in COMPARE.get(ct, ()) if k in f}
                ok = own_keys(t, f)
                c.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?,?)",
                          (js, t, place, m, json.dumps(ok), f.get("content_hash"), sc.get("source_sha256"), json.dumps(small, default=str)))
                c.execute("DELETE FROM dockeys WHERE path=?", (js,))
                c.executemany("INSERT INTO dockeys VALUES (?,?,?)", [(kk, vv, js) for kk, vv in ok.items()])
                changed += 1
    gone = [p for p in have if p not in seen]
    c.executemany("DELETE FROM docs WHERE path=?", [(p,) for p in gone])
    c.executemany("DELETE FROM dockeys WHERE path=?", [(p,) for p in gone])
    c.commit()
    return changed


def page_keys(row):
    k = json.loads(row["keys"] or "{}")
    rd = json.loads(row["read"] or "{}")
    f = rd.get("fields") or {}
    t = row["reader_type"] or READER.get(str(row["kind"]))
    if t:
        k.update({kk: vv for kk, vv in own_keys(t, f).items()})             # the reader's values win over CPU keys
    return k, f, t


def same(a, b):
    if a in (None, "") or b in (None, ""):
        return None
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return norm(a) == norm(b)


def match(c):
    n = 0
    for row in c.execute("SELECT * FROM pages WHERE keys IS NOT NULL OR read IS NOT NULL").fetchall():
        k, f, t = page_keys(row)
        cands = []
        for key in ("jt_number", "invoice_number", "cert_no", "dn_number", "payment", "ws"):
            if not k.get(key):
                continue
            for d in c.execute("SELECT d.* FROM dockeys x JOIN docs d ON d.path = x.path WHERE x.k=? AND x.v=?", (key, str(k[key]))).fetchall():
                dk = json.loads(d["keys"] or "{}")
                if key == "invoice_number" and k.get("supplier_vat_no") and dk.get("supplier_vat_no") and dk["supplier_vat_no"] != k["supplier_vat_no"]:
                    continue
                cands.append((key, d))
        if not cands:
            continue
        key, d = cands[0]
        dt = "SupplierInvoices" if d["type"] == "SupplierInvoicesArchive" else d["type"]
        df = json.loads(d["fields"] or "{}")
        agree, differ = [], {}
        for fld in COMPARE.get(dt, ()):
            s = same(f.get(fld), df.get(fld))
            if s is True:
                agree.append(fld)
            elif s is False:
                differ[fld] = {"behind_order": f.get(fld), "own_copy": df.get(fld)}
        same_image = bool(row["page_sha"] and d["content_hash"] and row["page_sha"].upper() == str(d["content_hash"]).upper())
        m = {"doc": d["path"].replace(str(ROOT) + "/", ""), "type": d["type"], "place": d["place"], "key": key,
             "agree": agree, "differ": differ, "same_image": same_image, "read": bool(f),
             "result": "differs" if differ else ("agrees" if agree else "linked")}
        old = json.loads(row["match"] or "{}")
        if {kk: vv for kk, vv in old.items() if kk != "at"} != m:
            update(c, row["source_sha"], row["source_page"], match=json.dumps(dict(m, at=now())), matched_at=now())
            annotate(row, m)
            n += 1
    c.commit()
    return n


def annotate(row, m):
    """The own-channel document gets the second source in its sidecar (not once it is uploaded)."""
    if m["place"] == "synced":
        return
    js = ROOT / m["doc"]
    try:
        with sidecar_lock.locked(js, "supporting", wait=5):
            sc = json.load(open(js, encoding="utf-8"))
            f = sc.setdefault("fields", {})
            src = f"behind {row['parent_order'] or row['parent_file']} (scan {row['source_file']}, page {row['source_page']})"
            f["second_source"] = {"from": src, "result": m["result"], "agree": m["agree"], "differ": m["differ"],
                                  "same_image": m["same_image"], "read": m["read"], "at": now()}
            checks = [x for x in (f.get("checks") or []) if not str(x).startswith("second copy ")]
            for fld, v in m["differ"].items():
                checks.append(f"second copy {src} reads {fld} as {v['behind_order']}: check")
            if checks != (f.get("checks") or []):
                f["checks"] = checks
                f["review"] = "; ".join(map(str, checks)) or None
            tmp = js.with_name(js.name + ".sp.tmp")
            tmp.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, js)
    except (sidecar_lock.Locked, OSError, ValueError):
        pass


def prune():
    """Sent outbox files older than 30 days."""
    for p in (W / "sent").glob("*.json") if (W / "sent").exists() else []:
        if time.time() - p.stat().st_mtime > 30 * 86400:
            p.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    t = sp.add_parser("tick")
    t.add_argument("--harvest-max", type=int, default=150)
    t.add_argument("--feed-max", type=int, default=20)
    sp.add_parser("status")
    a = ap.parse_args()
    c = db()
    if a.cmd == "status":
        print(json.dumps({"pages": dict(Counter(r["status"] for r in c.execute("SELECT status FROM pages"))),
                          "kinds": dict(Counter(r["kind"] for r in c.execute("SELECT kind FROM pages")).most_common(15)),
                          "matches": dict(Counter(json.loads(r["match"]).get("result") for r in c.execute("SELECT match FROM pages WHERE match IS NOT NULL"))),
                          "own_docs_indexed": c.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
                          "outbox": len(list(OUTBOX.glob("*.json"))) if OUTBOX.exists() else 0}, indent=1))
        return
    out = {"at": now()}
    out["harvest"] = harvest(c, a.harvest_max)
    out["collected"] = collect(c) if RESULTS.exists() else 0
    out["own_docs_changed"] = index_docs(c)
    out["matched"] = match(c)
    out["feed"] = feed(c, a.feed_max)
    prune()
    (W / "last_tick.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
MPS OCR pipeline dashboard  (runs on mps-frigate as the systemd service mps-ocr-dashboard)

Browse to  http://mps-frigate:8090  and log in with the dashboard password.
Shows the worker, NAS collector, Ollama, GPU and the PC sync heartbeat; per-type switches for
collecting / processing / syncing; start/stop/restart of the services; retry buttons for failed files;
recent activity and service logs. One tab per document type; the Supplier Invoices tab also lists VAT numbers
read on invoices that aren't on the Sage supplier list (same check as /srv/ocr/scans/missing_vat.py).
Each job tab shows its out queue (open the PDF, see the line items, hold / release) and a "Sync now" button:
it leaves flags/sync_now_<Type>, which the PC's scheduled task (Sync-OcrOutput.ps1 -OnRequest) picks up.

Switches are flag files in <root>/flags (see worker.py, collector.py, Sync-OcrOutput.ps1), so nothing
here needs root except start/stop, which goes through a narrow sudoers rule installed by install.sh.

Set or change the password:   python3 /srv/ocr/pipeline/dashboard.py --set-password
Standard library only.
"""
import argparse, getpass, hashlib, hmac, json, os, re, secrets, shutil, sqlite3, subprocess, sys, threading, time
import urllib.request
from datetime import datetime, date
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sidecar_lock                                   # noqa: E402  (review F10: one lock per sidecar for every writer)
try:
    import po_reader                # same folder as the worker: library file names, PO number clean-up
except Exception:                   # pragma: no cover
    po_reader = None
try:
    import ws_reader                # workshop sheets: library file names, line text after an edit
except Exception:                   # pragma: no cover
    ws_reader = None
try:
    import tc_reader                # test certificates: summaries after an edit
except Exception:                   # pragma: no cover
    tc_reader = None

CONFIG_PATH = Path(os.environ.get("MPS_OCR_CONFIG", "/srv/ocr/pipeline/config.json"))
SESSIONS = {}                 # token -> {"exp": ts, "csrf": str}
FAILS = {}                    # ip -> [timestamps]
LOCK = threading.Lock()
PASSWORD_ITER = 260_000


# --------------------------------------------------------------------------- config / auth
def load_cfg():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PASSWORD_ITER).hex()
    return f"pbkdf2_sha256${PASSWORD_ITER}${salt}${dk}"


def check_password(pw, stored):
    try:
        _, iters, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters)).hex()
        return hmac.compare_digest(got, dk)
    except Exception:
        return False


def set_password():
    cfg = load_cfg()
    pw = getpass.getpass("New dashboard password: ")
    if len(pw) < 10:
        sys.exit("Use at least 10 characters.")
    if pw != getpass.getpass("Again: "):
        sys.exit("Passwords don't match.")
    cfg.setdefault("dashboard", {})["password_hash"] = hash_password(pw)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    print("Password saved. Restart the dashboard: sudo systemctl restart mps-ocr-dashboard")


# --------------------------------------------------------------------------- status gathering
def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def age_seconds(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return max(0, int((now - dt).total_seconds()))
    except Exception:
        return None


def service_state(name):
    rc, out, _ = run(["systemctl", "show", name, "-p", "ActiveState,SubState,ActiveEnterTimestamp,NRestarts,UnitFileState"])
    d = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"name": name, "active": d.get("ActiveState", "unknown"), "sub": d.get("SubState", ""),
            "since": d.get("ActiveEnterTimestamp", ""), "restarts": d.get("NRestarts", ""),
            "enabled": d.get("UnitFileState", "")}


def ollama_state(cfg):
    base = cfg.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
    st = {"up": False, "loaded": [], "version": None}
    try:
        with urllib.request.urlopen(base + "/api/version", timeout=3) as r:
            st["version"] = json.load(r).get("version")
        st["up"] = True
        with urllib.request.urlopen(base + "/api/ps", timeout=3) as r:
            st["loaded"] = [{"name": m.get("name"), "vram_mb": round(m.get("size_vram", 0) / 2**20),
                             "until": m.get("expires_at")} for m in json.load(r).get("models", [])]
    except Exception as e:
        st["error"] = str(e)[:150]
    return st


_PROBE = {}


def cached(key, ttl, fn, *args):
    """fn(*args) at most every ttl seconds (review F8, 28 Oct: the 5 s refresh was spawning smartctl, nvidia-smi and
    systemctl on every tick - ~50,000 processes a day with one browser tab open)."""
    hit = _PROBE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn(*args)
    _PROBE[key] = (time.time(), v)
    return v


def gpu_state():
    if not shutil.which("nvidia-smi"):
        return {"present": False}
    rc, out, err = run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                        "--format=csv,noheader,nounits"])
    if rc != 0 or not out:
        return {"present": True, "error": err[:150]}
    name, util, used, total, temp, power = [x.strip() for x in out.splitlines()[0].split(",")]
    rc, apps, _ = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
    procs = []
    for line in apps.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 3:
            pid, pname, mem = parts
            rc2, cmdline, _ = run(["ps", "-o", "args=", "-p", pid], 3)
            procs.append({"pid": pid, "name": os.path.basename(pname), "mem_mb": mem, "cmd": cmdline[:160]})
    return {"present": True, "name": name, "util": util, "mem_used": used, "mem_total": total,
            "temp": temp, "power": power, "procs": procs}


def count_files(d, pattern=None):
    try:
        return sum(1 for e in os.scandir(d) if e.is_file() and (pattern is None or re.search(pattern, e.name)))
    except FileNotFoundError:
        return 0


def list_failed(d, limit=50):
    items = []
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.stat().st_mtime, reverse=True)
    except FileNotFoundError:
        return items
    for e in entries:
        if not e.is_file() or e.name.endswith((".error.txt", ".attempts", ".uploaded")):
            continue
        if d.parent.name == "sync_failed" and not e.name.endswith(".json"):
            continue
        err_file = Path(d, e.name + ".error.txt")
        err = ""
        if err_file.exists():
            err = err_file.read_text(encoding="utf-8-sig", errors="replace").strip().splitlines()[-1][:240]
        items.append({"name": e.name, "at": datetime.fromtimestamp(e.stat().st_mtime).isoformat(timespec="seconds"), "error": err})
        if len(items) >= limit:
            break
    return items


def storage_state(cfg):
    """RAID (mdadm) state, SMART health of its members, the last backup run (backup_status.json written by
    mps-backup.sh) and what is deployed (git), for the Overview's Storage & backup card."""
    st = {"raid": None, "arrays": [], "smart": [], "volumes": [], "backup": None, "deploy": None}
    try:
        txt = Path("/proc/mdstat").read_text()
        heads = list(re.finditer(r"^(md\d+)\s*:\s*(\S+)\s+(\S+)\s+(.+?)$", txt, re.M))
        for i, m in enumerate(heads):
            dev, state, level, members = m.groups()
            blk = txt[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(txt)]
            st_m = re.search(r"\[(\d+)/(\d+)\]\s+\[([U_]+)\]", blk)
            sync = re.search(r"(resync|recovery|check|reshape)\s*=\s*([\d.]+)%.*?finish=([\d.]+)min", blk)
            st["arrays"].append({"dev": dev, "level": level, "state": state, "members": sorted(re.findall(r"(\w+)\[\d+\]", members)),
                                 "up": st_m.group(3) if st_m else "", "healthy": bool(st_m) and "_" not in st_m.group(3),
                                 "sync": {"what": sync.group(1), "pct": float(sync.group(2)), "min": float(sync.group(3))} if sync else None})
        st["arrays"].sort(key=lambda a: a["dev"])
        # the worst array first, for older cards that show one
        st["raid"] = next((a for a in st["arrays"] if not a["healthy"]), None) or next((a for a in st["arrays"] if a["sync"]), None) \
            or (st["arrays"][0] if st["arrays"] else None)
    except OSError:
        pass
    # each drive / array and what it holds (dashboard config "volumes" can override)
    vols = (cfg.get("dashboard") or {}).get("volumes") or [["System (NVMe)", "/"], ["Pipeline (RAID md0)", "/srv/ocr"],
                                                           ["Models & training (RAID md1)", "/srv/ml"]]
    seen = set()
    for label, mount in vols:
        try:
            if not os.path.ismount(mount) and mount != "/":
                st["volumes"].append({"label": label, "mount": mount, "missing": True}); continue
            dev = os.stat(mount).st_dev
            if dev in seen:
                continue
            seen.add(dev)
            u = shutil.disk_usage(mount)
            st["volumes"].append({"label": label, "mount": mount, "total_gb": round(u.total / 2**30), "used_gb": round(u.used / 2**30),
                                  "free_gb": round(u.free / 2**30), "pct": round(100 * u.used / (u.used + u.free)) if u.used + u.free else 0})   # as df's Use%
        except OSError as e:
            st["volumes"].append({"label": label, "mount": mount, "missing": True, "error": str(e)[:80]})
    for dev in [d for a in st["arrays"] for d in a["members"]]:
        rc, out, err = run(["sudo", "-n", "/usr/sbin/smartctl", "-H", "-A", f"/dev/{dev}"], 8)
        h = re.search(r"SMART overall-health self-assessment test result:\s*(\w+)", out)
        temp = re.search(r"Temperature_Celsius.*?\s(\d+)\s*$", out, re.M) or re.search(r"^194 .*?(\d+)\s*$", out, re.M)
        st["smart"].append({"dev": dev, "health": h.group(1) if h else ("n/a" if rc else "?"), "temp": temp.group(1) if temp else None,
                            "error": "" if h else (err or out)[-120:]})
    st["backup"] = read_json(Path(cfg["root"]) / "backup_status.json")
    if st["backup"]:
        st["backup"]["age"] = age_seconds(st["backup"].get("at"))
    repo = Path(cfg.get("repo", "/srv/ocr/repo"))
    if (repo / ".git").exists():
        rc, out, _ = run(["git", "-C", str(repo), "log", "-1", "--format=%h %ad %s", "--date=short"], 5)
        rc2, dirty, _ = run(["git", "-C", str(repo), "status", "--porcelain"], 5)
        st["deploy"] = {"last": out.strip()[:120], "dirty": bool(dirty.strip())}
    return st


def alert_flags(root):
    """flags/alert_<unit>: left by mps-alert@.service when a timer's job fails (review F9, 28 Oct); cleared from the
    overview. Each file: first line = when, rest = the last journal lines."""
    out = []
    try:
        for e in sorted(os.scandir(root / "flags"), key=lambda e: e.name):
            if e.is_file() and e.name.startswith("alert_"):
                t = e.path and Path(e.path).read_text(encoding="utf-8", errors="replace")
                lines = t.strip().splitlines()
                out.append({"flag": e.name, "unit": e.name[6:], "at": lines[0] if lines else "", "text": "\n".join(lines[1:])[-600:]})
    except FileNotFoundError:
        pass
    return out


def status(cfg):
    root = Path(cfg["root"])
    flags = root / "flags"
    types = list(cfg["doc_types"])
    dcfg = cfg.get("dashboard", {})
    services = [cached("svc:" + s, 10, service_state, s) for s in dcfg.get("services", ["mps-ocr-worker", "mps-ocr-collector"])]

    worker = read_json(root / "status.json") or {}
    worker["age"] = age_seconds(worker.get("updated_at"))
    collector = read_json(root / "collector_status.json") or {}
    collector["age"] = age_seconds(collector.get("updated_at"))
    sync = read_json(root / "sync_status.json") or {}
    sync["age"] = age_seconds(sync.get("finished_at") or sync.get("started_at"))

    today = date.today().isoformat()
    month = datetime.now().strftime("%Y-%m")
    done_today, recent, via_today, recent_by_type = {}, [], {}, {}
    try:
        db = sqlite3.connect(f"file:{cfg.get('state_db', '/srv/ocr/pipeline/state.db')}?mode=ro", uri=True, timeout=5)
        for folder, n in db.execute("SELECT folder, COUNT(*) FROM processed WHERE status='ok' AND substr(at,1,10)=? GROUP BY folder", (today,)):
            done_today[folder] = n
        for src, folder, st, outputs, at in db.execute(
                "SELECT source, folder, status, outputs, at FROM processed ORDER BY at DESC LIMIT 25"):
            try:
                out = json.loads(outputs)
            except Exception:
                out = outputs
            recent.append({"source": src, "type": folder, "status": st, "outputs": out, "at": at})
        for t in types:
            for src, st, outputs, at in db.execute(
                    "SELECT source, status, outputs, at FROM processed WHERE folder=? ORDER BY at DESC LIMIT 20", (t,)):
                try:
                    out = json.loads(outputs)
                except Exception:
                    out = outputs
                recent_by_type.setdefault(t, []).append({"source": src, "type": t, "status": st, "outputs": out, "at": at})
        try:
            for folder, via, n in db.execute("SELECT folder, via, COUNT(*) FROM extractions WHERE substr(at,1,10)=? GROUP BY folder, via", (today,)):
                via_today.setdefault(folder, {})[via] = n
        except sqlite3.OperationalError:
            pass
        db.close()
    except Exception as e:
        recent.append({"source": f"(state DB not readable: {e})", "type": "", "status": "", "outputs": "", "at": ""})

    rows = []
    for t in types:
        synced_today = 0
        try:
            for e in os.scandir(root / "synced" / t / month):
                if e.name.endswith(".json") and date.fromtimestamp(e.stat().st_mtime).isoformat() == today:
                    synced_today += 1
        except FileNotFoundError:
            pass
        rows.append({
            "type": t,
            "label": {"JobTransfers": "Job Transfers", "PurchaseOrders": "Purchase Orders",
                      "SupplierInvoices": "Supplier Invoices",
                      "SupplierInvoicesArchive": "Supplier Invoices (archive)",
                      "DeliveryNotes": "Delivery Notes"}.get(t, cfg["doc_types"][t].get("label") or t),
            "collect_on": not (flags / f"collect_off_{t}").exists(),
            "process_on": not (flags / f"process_off_{t}").exists(),
            "sync_on": not (flags / f"sync_off_{t}").exists(),
            "in_queue": count_files(root / "in" / t, r"^(?!~\$|\.).*\.(pdf|jpe?g|png)$"),
            "awaiting_sync": count_files(root / "out" / t, r"\.json$"),
            "done_today": done_today.get(t, 0),
            "via_today": via_today.get(t, {}),
            "cpu_first": bool(cfg["doc_types"][t].get("cpu_first")),
            "synced_today": synced_today,
            "failed": list_failed(root / "failed" / t),
            "sync_failed": list_failed(root / "sync_failed" / t),
            "recent": recent_by_type.get(t, []),
            "held": count_files(root / "out" / t / "hold", r"\.json$"),
            "sync_requested": (flags / f"sync_now_{t}").exists(),
            "sync_running": (flags / f"sync_now_{t}.running").exists(),
            "sync_last": read_json(root / f"sync_last_{t}.json"),
        })

    du = shutil.disk_usage(root)
    return {
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": os.uname().nodename,
        "services": services,
        "worker": worker,
        "worker_paused": (root / "PAUSE").exists(),
        "collector": collector,
        "collector_configured": bool(cfg.get("collector", {}).get("sources")),
        "collect_all_on": not (flags / "collect_off").exists(),
        "sync": sync,
        "sync_every_minutes": int(dcfg.get("sync_every_minutes", 15)),   # how often the PC's on-request task checks
        "sync_all_on": not (flags / "sync_off").exists(),
        "ollama": ollama_state(cfg),
        "gpu": cached("gpu", 10, gpu_state),
        "alerts": alert_flags(root),
        "types": rows,
        "recent": recent,
        "disk": {"free_gb": round(du.free / 2**30, 1), "total_gb": round(du.total / 2**30, 1)},
        "storage": cached("storage", 60, storage_state, cfg),
    }


# --------------------------------------------------------------------------- missing VAT numbers
OWN_VAT = {"4320123039"}                                   # MPS's own VAT number: never a supplier
VAT_SKIP = {"THE", "PTY", "LTD", "CC", "AND", "TA", "INC", "SA", "GROUP", "MINING"}


def _name_key(name):
    for w in re.split(r"[\s\-_,/&().]+", (name or "").upper()):
        w = re.sub(r"[^A-Z0-9]", "", w)
        if len(w) >= 3 and w not in VAT_SKIP:
            return w
    return None


def missing_vat(cfg):
    """VAT numbers read on supplier invoices that aren't on the Sage list (suppliers.csv); same rules as missing_vat.py."""
    import csv
    root = Path(cfg["root"])
    own = OWN_VAT | {re.sub(r"\D", "", str(v)) for v in (cfg.get("own_vat") or [])}
    sage, csv_at = {}, None
    p = root / "suppliers.csv"
    try:
        csv_at = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                v = re.sub(r"\D", "", r.get("VatNo") or "")
                if v:
                    sage[v] = (r.get("Name") or "").strip()
    except FileNotFoundError:
        pass
    sage_keys = {_name_key(n) for n in sage.values()} - {None}
    seen, no_vat = {}, {}
    for sub in ("synced", "out", "done"):
        for t in ("SupplierInvoices", "SupplierInvoicesArchive"):
            d = root / sub / t
            if not d.is_dir():
                continue
            for j in d.rglob("*.json"):
                try:
                    doc = json.loads(j.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(doc, dict) or doc.get("doc_type") != "supplier_invoice":
                    continue
                if "supplier_is_mps" in (doc.get("flags") or []):
                    continue
                f = doc.get("fields") or {}
                v = re.sub(r"\D", "", str(f.get("supplier_vat_no") or ""))
                name = (f.get("supplier_name") or "?").strip() or "?"
                if not re.fullmatch(r"4\d{9}", v) or v in own:
                    if _name_key(name) not in sage_keys:     # GPU left the VAT out, but the name is a Sage supplier: fine
                        no_vat[name] = no_vat.get(name, 0) + 1
                    continue
                s = seen.setdefault(v, {"names": {}, "n": 0, "last": ""})
                s["n"] += 1
                s["names"][name] = s["names"].get(name, 0) + 1
                s["last"] = max(s["last"], str(f.get("invoice_date") or ""))
    note = None
    try:                                                   # VAT numbers the worker learned from invoices
        db = sqlite3.connect(f"file:{cfg.get('state_db', '/srv/ocr/pipeline/state.db')}?mode=ro", uri=True, timeout=5)
        for v, name, _at in db.execute("SELECT vat_no, name, at FROM suppliers WHERE source='learned'"):
            if v not in seen:
                seen[v] = {"names": {name or "?": 0}, "n": 0, "last": ""}
        db.close()
    except Exception as e:
        note = f"state.db not read: {e}"
    missing = sorted(((v, s) for v, s in seen.items() if v not in sage), key=lambda x: -x[1]["n"])
    return {
        "sage_count": len(sage), "sage_list_at": csv_at, "note": note,
        "checked": sum(s["n"] for s in seen.values()) + sum(no_vat.values()),
        "missing": [{"vat": v, "invoices": s["n"], "latest": s["last"] or None,
                     "name": max(s["names"], key=s["names"].get) if s["names"] else "?"} for v, s in missing],
        "no_vat_total": sum(no_vat.values()),
        "no_vat": [{"name": n, "invoices": c} for n, c in sorted(no_vat.items(), key=lambda x: -x[1])[:60]],
        "at": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- out queue (what "Sync now" will send)
OUT_FIELDS = ("supplier_name", "supplier_vat_no", "invoice_number", "invoice_date", "order_ref", "job_number", "subtotal",
              "vat", "total", "currency", "po_number", "jt_number", "date", "from", "to", "ordered_by", "written_by",
              # purchase orders (trained reader): the training sheet's header fields and the rest of the order header
              "order_number", "supplier", "order_method", "att_of", "date_as_written", "allocation", "other_po_ref", "quote_ref",
              "notes", "payment_terms", "signed_by", "checked", "writer", "company", "form_label", "delivery_address",
              "transfer_from", "transfer_to", "legibility", "polymer_po", "pl_order", "supplier_read",
              # proofs of payment
              "amount", "statement_ref", "account_name", "creditor_code", "from_account_name", "bank_reference", "bank",
              "beneficiary_account", "cross_checked", "library_doc_type", "payment_pages", "payment_ocr", "cancelled", "amended",
              # delivery notes (barcode + Sage)
              "dn_number", "number_read_from", "customer", "customer_account", "customer_order", "ext_order", "job_name", "rep",
              "deliver_to", "pages", "source_pages", "extra_pages", "page_kinds", "doc_label", "release_number", "sage_found",
              "sage_company", "sage_matches", "printed_date", "total_excl", "total_incl", "lines_text",
              # workshop sheets
              "sheet", "sheet_no", "sheet_of", "project", "corrosion_int", "corrosion_ext", "destination", "spec", "customer_dwg_no",
              "rev", "mps_dwg_no", "batch_no", "mto", "drawn_by", "status", "end_detail", "layout", "job_number_read",
              "job_customer", "line_count", "total_qty", "review", "stamped", "ifc_signed", "stamp_date", "stamp_date_as_written",
              "stamp_text",
              # test certificates
              "cert_no", "cert_type", "material_no", "mill", "purchaser", "mps_po", "ronin_po", "contract", "family",
              "standard", "grade", "grade_summary", "heats_text", "item_count", "heat_count", "heats", "old_columns",
              "sync_mode", "sp_item_id", "heat_check_text")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _jpath(obj, path):
    cur = obj
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
        if cur is None:
            return None
    return cur


def _doc_date(doc):
    """Same order as Expand-FolderTemplate in Sync-OcrOutput.ps1: the document's own date, else the processing date."""
    for k in ("fields.date", "fields.invoice_date", "processed_at"):
        v = _jpath(doc, k)
        if v and len(str(v)) >= 10:
            try:
                return datetime.strptime(str(v)[:10], "%Y-%m-%d"), k
            except ValueError:
                continue
    return datetime.now(), "today"


def _invoice_upload_name(doc, fallback):
    """Port of Get-InvoiceUploadName (library naming SUPPLIER-INVOICE-dd-MM-yyyy.pdf), used by link-or-upload."""
    sup, inv = str(_jpath(doc, "fields.supplier_name") or ""), str(_jpath(doc, "fields.invoice_number") or "")
    if not sup or not inv:
        return fallback
    parts = [sup.upper().strip(), inv.strip()]
    try:
        parts.append(datetime.strptime(str(_jpath(doc, "fields.invoice_date") or "")[:10], "%Y-%m-%d").strftime("%d-%m-%Y"))
    except ValueError:
        pass
    n = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#%~]', " ", "-".join(parts))).strip(" .")
    return n[:120].strip() + ".pdf"


SQL_TABLES = {"supplier_invoice": ("SupplierInvoice", "SupplierInvoiceLine"), "po": ("PurchaseOrder", "PurchaseOrderLine"),
              "jt": ("JobTransfer", "JobTransferLine")}


def sage_po_text(spo, fields, company):
    """Same text the PC sync writes for '@sage_po:<COMPANY>' columns."""
    r = sage_po_check(spo, fields, company)
    if r["status"] == "no_data":
        return None
    if r["status"] == "no_po":
        return "No PO number read"
    if r["status"] == "not_found":
        return "Not found in Sage"
    b = r["best"]
    what = b["number"] if b["own"] else f"{b['source']} {b['number']} (order {r['po']})"
    amt = f" - R{b['amount']:,.2f}" if b["amount"] is not None else ""
    more = f" - +{r['count'] - 1} more" if r["count"] > 1 else ""
    return f"Found: {what} - {b['vendor']} - {b['date']}{amt}{more}"


def sharepoint_preview(tmap, doc, sage_acct, spo=None):
    """What the PC sync will do with this document in SharePoint, from the map it publishes (sharepoint_map.json)."""
    if not tmap:
        return None
    d, date_from = _doc_date(doc)
    month = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"][d.month - 1]
    folder = (tmap.get("folder") or "").replace("{yyyy}", d.strftime("%Y")).replace("{MMMM}", month).replace("{MM}", d.strftime("%m"))
    mode = tmap.get("mode") or "upload"
    pdf = str(doc.get("file") or "")
    orig = re.sub(r"^[A-Z]_\d{5}__(?:[0-9a-f]{10} )?", "", str(doc.get("source_file") or ""))
    vat = re.sub(r"\D", "", str(_jpath(doc, "fields.supplier_vat_no") or ""))
    cols = []
    for col, src in (tmap.get("fields") or {}).items():
        v = sage_acct.get(vat) if src == "@sage_account" else src[1:] if src.startswith("=") else \
            _jpath(doc, "fields.job_number") if src.startswith("@job_lookup:") else \
            sage_po_text(spo, doc.get("fields") or {}, src[9:]) if src.startswith("@sage_po:") else _jpath(doc, src)
        if isinstance(v, (dict, list)):
            v = json.dumps(v)
        cols.append({"column": col, "source": "Sage account (by VAT number)" if src == "@sage_account" else "fixed value" if src.startswith("=") else
                     f"link to {src[12:]} (job number)" if src.startswith("@job_lookup:") else
                     f"Sage {src[9:].title()} PO check" if src.startswith("@sage_po:") else src,
                     "value": None if v is None or str(v) == "" else (v if isinstance(v, (int, float, bool)) else str(v)[:1000])})
    tables = SQL_TABLES.get(doc.get("doc_type"))
    return {"mode": mode, "site": tmap.get("site"), "library": tmap.get("library"), "folder": folder, "date_from": date_from,
            "name": pdf, "link_name": orig, "fallback_name": _invoice_upload_name(doc, pdf) if mode == "link-or-upload" else None,
            "columns": cols, "blank": [c["column"] for c in cols if c["value"] is None], "dedupe": tmap.get("dedupe") or None,
            "link_orders": bool(tmap.get("link_orders")), "skip_sql": bool(tmap.get("skip_sql")),
            "lines": tmap.get("lines") or None,
            "sql": {"table": tables[0], "lines_table": tables[1]} if tables and not tmap.get("skip_sql") else None}


_SAGE = {"mtime": None, "by_account": {}, "accounts_by_vat": {}, "rows": 0}


def ref_keys(ref):
    """Ways an invoice number can be written: CIN0387242 / CIN-0387242 / 0387242; I55038 vs Sage 55038."""
    k = re.sub(r"[\s\-/._#]", "", str(ref or "")).upper()
    keys = {k} if k else set()
    digits = re.sub(r"\D", "", k).lstrip("0")
    if len(digits) >= 3:
        keys.add("#" + digits)
    return keys


_SAGE_PO = {"mtime": None, "idx": {}, "at": None}


def sage_po(root):
    """sage_po.csv (Sage purchase orders, GRVs and AP invoices by PO number; the PC sync writes it from MPSBusiness
    rpt.vw_PurchaseOrderRefs), indexed "COMPANY|PO7022" -> rows. PO0007022 = PO7022 = PO 7022."""
    import csv
    p = Path(root) / "sage_po.csv"
    try:
        m = p.stat().st_mtime
    except FileNotFoundError:
        return None
    if _SAGE_PO["mtime"] != m:
        idx = {}
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                for fld in ("DocNumber", "OrderNum", "ExtOrderNum"):
                    mm = re.fullmatch(r"\s*P\s*O?\s*[-#:.]?\s*0*(\d{1,7})\s*", r.get(fld) or "", re.I)
                    if mm:
                        idx.setdefault(f"{r.get('Company')}|PO{int(mm.group(1))}", []).append(
                            {"own": fld == "DocNumber" and r.get("Source") == "Document", "source": r.get("Source"),
                             "doc_type": r.get("DocType"), "number": r.get("DocNumber"), "order": r.get("OrderNum"),
                             "ext": r.get("ExtOrderNum"), "vendor": r.get("VendorName"), "account": r.get("VendorAccount"),
                             "date": r.get("DocDate"), "amount": _num(r.get("AmountExcl"))})
        _SAGE_PO.update(mtime=m, idx=idx, at=datetime.fromtimestamp(m).isoformat(timespec="minutes"))
    return _SAGE_PO


def sage_po_check(spo, fields, company="POLYMER"):
    """Is the Polymer PO number on this PL order in Polymer's Sage? Best hit = the PO document itself."""
    po = fields.get("polymer_po")
    if spo is None:
        return {"status": "no_data"}
    if not po:
        return {"status": "no_po"}
    hits = spo["idx"].get(f"{company}|{po}") or []
    if not hits:
        return {"status": "not_found", "po": po, "data_at": spo["at"]}
    hits = sorted(hits, key=lambda h: (not h["own"], h["date"] or ""))
    b = hits[0]
    sup = re.sub(r"[^A-Z0-9]", "", str(fields.get("supplier") or "").upper())
    ven = re.sub(r"[^A-Z0-9]", "", str(b["vendor"] or "").upper())
    return {"status": "found", "po": po, "best": b, "count": len(hits), "data_at": spo["at"],
            "pl_in_sage": any(str(fields.get("order_number") or "") and str(fields.get("order_number")).upper() in
                              str(h.get("ext") or "").upper().replace(" ", "") for h in hits),
            "supplier_ok": bool(sup and ven and (sup in ven or ven in sup or sup[:5] == ven[:5]))}


def sage_ap(root):
    """sage_ap.csv (Sage supplier invoices, written by the PC sync from MPSBusiness rpt.vw_APInvoices), cached."""
    import csv
    p = Path(root) / "sage_ap.csv"
    try:
        m = p.stat().st_mtime
    except FileNotFoundError:
        return None
    if _SAGE["mtime"] != m:
        by_account, acc_by_vat, n = {}, {}, 0
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                acc = (r.get("VendorAccount") or "").strip()
                if not acc:
                    continue
                n += 1
                row = {"ref": r.get("Reference"), "date": r.get("TxDate"), "amount": _num(r.get("AmountIncl")),
                       "order": r.get("OrderNo"), "name": r.get("VendorName")}
                for k in ref_keys(r.get("Reference")):
                    by_account.setdefault(acc, {}).setdefault(k, []).append(row)
                v = re.sub(r"\D", "", r.get("VatNo") or "")
                if re.fullmatch(r"4\d{9}", v):
                    acc_by_vat.setdefault(v, set()).add(acc)
        _SAGE.update(mtime=m, by_account=by_account, accounts_by_vat=acc_by_vat, rows=n,
                     at=datetime.fromtimestamp(m).isoformat(timespec="minutes"))
    return _SAGE


def sage_check(sage, fields, sage_account=None):
    """Is this invoice in Sage, and do date and amount agree? Matched on the supplier's Sage account (from its VAT
    number) + invoice number."""
    if sage is None:
        return None
    vat = re.sub(r"\D", "", str(fields.get("supplier_vat_no") or ""))
    accounts = set(sage["accounts_by_vat"].get(vat, set()))
    if sage_account:
        accounts.add(sage_account)
    if not accounts:
        return {"status": "no_account", "why": "supplier's VAT number isn't on any Sage supplier" if vat else "no VAT number read"}
    keys = ref_keys(fields.get("invoice_number"))
    if not keys:
        return {"status": "no_number", "accounts": sorted(accounts)}
    hits = {}
    for a in accounts:
        for k in sorted(keys):                               # exact form first ("#..." digits-only sorts first: prefer full key)
            for r in sage["by_account"].get(a, {}).get(k, []):
                hits[(a, r["ref"], r["date"], r["amount"])] = dict(r, account=a)
    hits = list(hits.values())
    if not hits:
        return {"status": "not_found", "accounts": sorted(accounts), "data_at": sage.get("at")}
    if len(hits) > 1:                                         # same number twice: prefer the one whose amount matches
        tot = _num(fields.get("total"))
        same = [h for h in hits if tot is not None and h["amount"] is not None and abs(h["amount"] - tot) <= 1]
        hits = same or hits
    h = hits[0]
    inv_date, tot = str(fields.get("invoice_date") or ""), _num(fields.get("total"))
    date_ok = bool(inv_date) and h["date"] == inv_date[:10]
    amt_ok = tot is not None and h["amount"] is not None and abs(h["amount"] - tot) <= 1
    amount_fine = amt_ok or tot is None                     # no total read: judge on the date alone
    status = ("match" if amount_fine else "amount") if date_ok else ("date" if amount_fine else "date_amount")
    return {"status": status, "account": h["account"], "ref": h["ref"], "date": h["date"], "amount": h["amount"],
            "order": h["order"], "multiple": len(hits) > 1, "date_ok": date_ok, "amount_ok": amt_ok}


# people who write the forms (from the Job Transfer training set); more can be added from the Edit card
DEFAULT_WRITERS = ["Melyssa", "Ivor", "Derrick", "David", "Sanet", "Whity", "Joanne", "Dican", "Bonga", "Edward", "Ashton",
                   "Hannes", "Unknown"]


def writers(cfg):
    p = Path(cfg["root"]) / "training" / "writers.json"
    w = read_json(p)
    return w if isinstance(w, list) and w else list(DEFAULT_WRITERS)


def add_writer(cfg, name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name or len(name) > 40 or not re.fullmatch(r"[\w .'/&-]+", name):
        raise ValueError("enter a name (letters, up to 40 characters)")
    name = name.title() if name.isupper() or name.islower() else name
    w = writers(cfg)
    if name.lower() not in (x.lower() for x in w):
        w.insert(len(w) - 1 if w and w[-1] == "Unknown" else len(w), name)
        p = Path(cfg["root"]) / "training" / "writers.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(w, indent=1), encoding="utf-8")
    return {"ok": True, "writers": w, "name": next(x for x in w if x.lower() == name.lower())}


# price units on order lines: the reader's value -> how the forms write it
DEFAULT_UNITS = [["each", "ea"], ["kg", "PKG"], ["litre", "/L"], ["length", "PLEN"], ["metre", "PM"], ["lot", "lot"]]


def units(cfg):
    extra = read_json(Path(cfg["root"]) / "training" / "units.json") or []
    known = {u[0].lower() for u in DEFAULT_UNITS}
    return DEFAULT_UNITS + [[x, x] for x in extra if isinstance(x, str) and x.lower() not in known]


def add_unit(cfg, name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name or len(name) > 20 or not re.fullmatch(r"[\w ./%-]+", name):
        raise ValueError("enter a unit (up to 20 characters)")
    p = Path(cfg["root"]) / "training" / "units.json"
    extra = read_json(p) or []
    if name.lower() not in {u[0].lower() for u in units(cfg)} | {u[1].lower() for u in DEFAULT_UNITS}:
        extra.append(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(extra, indent=1), encoding="utf-8")
    hit = next((u for u in units(cfg) if name.lower() in (u[0].lower(), u[1].lower())), [name, name])
    return {"ok": True, "units": units(cfg), "value": hit[0]}


_SORTVAL = {}          # sidecar path -> (mtime, {key: value}) so a sorted refresh doesn't re-parse 2,000 files


def _sort_value(path, key):
    try:
        mt = os.stat(path).st_mtime
    except OSError:
        return None
    c = _SORTVAL.get(path)
    if not c or c[0] != mt:
        doc = read_json(path) or {}
        f = doc.get("fields") or {}
        c = (mt, {"_f": f, "_folder": doc.get("source_folder"), "_hold": doc.get("hold_reason"),
                  "_lines": len([i for i in (f.get("items") or []) if isinstance(i, dict)]), "_file": doc.get("file")})
        _SORTVAL[path] = c
    v = c[1]
    if key in ("_folder", "_hold", "_lines", "_file"):
        return v[key]
    f = v["_f"]
    x = f.get(key)
    if x in (None, "") and key in ("order_number", "supplier"):
        x = f.get({"order_number": "po_number", "supplier": "supplier_name"}[key])
    return x


def _sort_files(files, key, desc):
    """Blanks last whichever way; numbers as numbers, everything else as text (dates are ISO, so text order works)."""
    full, blank = [], []
    for e in files:
        v = _sort_value(e.path, key)
        if v in (None, "", []):
            blank.append(e)
            continue
        if isinstance(v, bool):
            k = (0, float(v), "")
        else:
            try:
                k = (0, float(str(v).replace(",", "").replace(" ", "")), "")
            except ValueError:
                k = (1, 0.0, str(v).strip().lower())
        full.append((k, e))
    full.sort(key=lambda kv: kv[0], reverse=desc)
    return [e for _, e in full] + blank


def out_queue(cfg, t, limit=300, sorts=None):
    if t not in cfg["doc_types"]:
        raise ValueError("unknown type")
    import csv
    root = Path(cfg["root"])
    sage, sage_acct = set(), {}
    try:
        with open(root / "suppliers.csv", newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                v = re.sub(r"\D", "", r.get("VatNo") or "")
                if v:
                    sage.add(v)
                    if (r.get("SageAccount") or "").strip():
                        sage_acct[v] = r["SageAccount"].strip()
    except FileNotFoundError:
        pass
    tmap = ((read_json(root / "sharepoint_map.json") or {}).get("types") or {}).get(t)
    sap = sage_ap(root) if cfg["doc_types"][t].get("doc_type") == "supplier_invoice" or t.startswith("SupplierInvoices") else None
    spo = sage_po(root) if t == "PolymerOrders" else None
    res = {"type": t, "ready": [], "held": [], "ready_total": 0, "held_total": 0, "limit": limit, "sp_map": bool(tmap), "writers": writers(cfg), "units": units(cfg),
           "sage_rows": sap["rows"] if sap else None, "sage_at": sap.get("at") if sap else None}
    for key, d in (("ready", root / "out" / t), ("held", root / "out" / t / "hold")):
        try:
            files = sorted((e for e in os.scandir(d) if e.is_file() and e.name.endswith(".json")), key=lambda e: e.stat().st_mtime)
        except FileNotFoundError:
            continue
        res[key + "_total"] = len(files)
        sk, sd = (sorts or {}).get(key) or (None, False)
        if sk and re.fullmatch(r"_?[a-z0-9_]{1,40}", sk):
            files = _sort_files(files, sk, sd)
        for e in files[:limit]:
            doc = read_json(e.path) or {}
            f = doc.get("fields") or {}
            items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
            amounts = [_num(i.get("amount")) for i in items]
            attempts = 0
            try:
                attempts = int(Path(e.path + ".attempts").read_text().strip() or 0)
            except (FileNotFoundError, ValueError):
                pass
            vat = re.sub(r"\D", "", str(f.get("supplier_vat_no") or ""))
            res[key].append({
                "name": e.name, "file": doc.get("file"),
                "pdf": bool(doc.get("file")) and Path(d, str(doc.get("file"))).is_file(),
                "doc_type": doc.get("doc_type"), "needs_review": bool(doc.get("needs_review")),
                "flags": doc.get("flags") or [], "extractor": doc.get("extractor"), "processed_at": doc.get("processed_at"),
                "reader": doc.get("reader"), "model": doc.get("model"), "checked_at": doc.get("checked_at"),
                "source_folder": doc.get("source_folder"), "edits": len(doc.get("edits") or []),
                "source_file": doc.get("source_file"), "attempts": attempts, "hold_reason": doc.get("hold_reason"),
                "uploaded": Path(e.path + ".uploaded").exists(),
                "fields": {k: f.get(k) for k in OUT_FIELDS if f.get(k) not in (None, "")},
                "items": items[:150], "item_count": len(items),
                "items_total": round(sum(a for a in amounts if a is not None), 2) if any(a is not None for a in amounts) else None,
                "vat_in_sage": (vat in sage or (sap is not None and vat in sap["accounts_by_vat"])) if (doc.get("doc_type") == "supplier_invoice" and re.fullmatch(r"4\d{9}", vat)) else None,
                "sp": sharepoint_preview(tmap, doc, sage_acct, spo),
                "sage": sage_check(sap, f, sage_acct.get(vat)) if doc.get("doc_type") == "supplier_invoice" else None,
                "sage_po": sage_po_check(spo, f) if t == "PolymerOrders" else None,
            })
    return res


def out_pdf(cfg, t, name, held):
    if t not in cfg["doc_types"]:
        raise ValueError("unknown type")
    d = Path(cfg["root"]) / "out" / t / ("hold" if held else "")
    p = safe_child(d, name)
    if p.suffix.lower() != ".pdf":
        raise ValueError("not a PDF")
    return p


# fields that can be edited on the dashboard, per document type (anything else in the request is ignored)
EDITABLE = {
    "po": ("order_number", "polymer_po", "supplier", "order_method", "att_of", "date", "allocation", "job_number", "other_po_ref",
           "quote_ref", "payment_terms", "signed_by", "writer", "delivery_address", "notes", "cancelled", "amended"),
    "pop": ("date", "amount", "statement_ref", "account_name", "creditor_code", "beneficiary_account", "from_account_name", "bank_reference"),
    "supplier_invoice": ("supplier_name", "supplier_vat_no", "invoice_number", "invoice_date", "order_ref", "job_number", "subtotal", "vat", "total"),
    "jt": ("jt_number", "date", "job_number", "from", "to", "written_by"),
    "delivery_note": ("dn_number", "date", "customer", "customer_account", "customer_order", "job_number", "deliver_to"),
    "ws": ("date", "sheet_no", "sheet_of", "project", "corrosion_int", "corrosion_ext", "destination", "job_number", "spec",
           "customer_dwg_no", "rev", "mps_dwg_no", "batch_no", "mto", "drawn_by", "status", "end_detail", "notes",
           "stamped", "ifc_signed", "stamp_date"),
    "test_cert": ("cert_no", "cert_type", "date", "mill", "supplier", "customer", "purchaser", "mps_po", "ronin_po", "contract",
                  "family", "standard", "grade", "material_no"),
}
EDIT_LINES = {"po": ("quantity", "description", "unit_price", "price_unit"),
              "supplier_invoice": ("item_code", "description", "quantity", "unit_price", "amount"),
              "jt": ("item_no", "quantity", "unit", "description"),
              "delivery_note": ("quantity", "description"),
              "ws": ("item_no", "mark", "qty", "size", "dwg_no", "dims_text"),
              "test_cert": ("family", "product", "description", "size_text", "size_nb", "schedule", "class", "grade", "ends",
                            "length_m", "qty", "unit", "heat_no")}
NUMERIC = {"amount", "subtotal", "vat", "total", "unit_price", "qty", "sheet_no", "sheet_of", "size_nb", "length_m"}


def _edit_value(k, v, doc_type):
    if k in ("cancelled", "amended", "stamped", "ifc_signed"):
        return str(v).strip().lower() in ("yes", "true", "1", "on")
    v = "" if v is None else str(v).strip()
    if v == "":
        return None
    if k in NUMERIC or (k == "quantity" and doc_type != "jt"):
        n = _num(v.replace("R", "").replace(" ", "").replace(",", "") if v.count(",") and v.count(".") else v.replace("R", "").replace(" ", "").replace(",", "."))
        if n is None:
            raise ValueError(f"{k}: '{v}' is not a number")
        return int(n) if k in ("quantity", "qty", "sheet_no", "sheet_of", "size_nb") and float(n).is_integer() else n
    if k in ("date", "invoice_date", "stamp_date"):
        try:
            return date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            raise ValueError(f"{k}: use a date (YYYY-MM-DD)")
    return v.upper() if k in ("order_number", "polymer_po", "jt_number", "job_number", "dn_number", "customer_account", "mps_dwg_no") else v


def _recompute(doc_type, f):
    """Derived values after an edit, the way the readers build them."""
    items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
    if doc_type == "po":
        for i in items:
            q, p = _num(i.get("quantity")), _num(i.get("unit_price"))
            i["amount"] = round(q * p, 2) if q is not None and p is not None else None
        if po_reader and f.get("polymer_po"):
            f["polymer_po"] = po_reader.polymer_po(f["polymer_po"]) or f["polymer_po"]
        for a, b in (("po_number", "order_number"), ("supplier_name", "supplier"), ("ordered_by", "signed_by"), ("delivery_to", "delivery_address")):
            f[a] = f.get(b)
        priced = [i for i in items if i.get("unit_price") is not None]
        amounts = [i["amount"] for i in items if i.get("amount") is not None]
        total = round(sum(amounts), 2) if priced and len(amounts) == len(priced) else None
        def lt(i):
            q = "" if i.get("quantity") is None else f"{i['quantity']:g} x " if isinstance(i["quantity"], (int, float)) else f"{i['quantity']} x "
            pr = "" if i.get("unit_price") is None else f" @ R{i['unit_price']:,.2f}" + (f" per {i['price_unit']}" if i.get("price_unit") not in (None, "each", "null") else "")
            return f"{q}{i.get('description') or ''}{pr}"
        f.update(items=items, line_count=len(items), lines_text="\n".join(lt(i) for i in items) or None, order_value=total, total=total,
                 value_note=f"{len(priced)} of {len(items)} lines priced" if items else None)
    elif doc_type == "delivery_note":
        def dl(i):
            q = i.get("quantity")
            q = "" if q is None else (format(q, "g") if isinstance(q, (int, float)) else str(q)) + " x "
            return q + str(i.get("description") or "")
        f.update(items=items, line_count=len(items), lines_text="\n".join(dl(i) for i in items) or None)
    elif doc_type == "ws":
        for i in items:              # "A=227 R=533.4 θ=45°" typed on the dashboard -> {"A": "227", ...}
            if "dims_text" in i:
                txt = str(i.get("dims_text") or "")
                i["dims"] = {m.group(1): m.group(2) for m in re.finditer(r"([^\s=]+)\s*=\s*(\S+)", txt)}
        f["items"] = items
        if ws_reader:
            ws_reader.recompute(f)
    elif doc_type == "test_cert":
        f["items"] = items
        if tc_reader and hasattr(tc_reader, "recompute"):
            tc_reader.recompute(f)
    elif doc_type == "supplier_invoice":
        for i in items:
            if i.get("amount") is None and _num(i.get("quantity")) is not None and i.get("unit_price") is not None:
                i["amount"] = round(_num(i["quantity"]) * i["unit_price"], 2)
        f["items"] = items


def _stem_for(doc_type, f, side):
    if doc_type == "delivery_note" and f.get("dn_number"):
        return re.sub(r"[^A-Za-z0-9_-]+", "", str(f["dn_number"]))[:40] or None
    if doc_type == "po" and po_reader and "form_label" in f:
        return po_reader.library_name(f, side.get("source_file") or side.get("file") or "document", "")[:150]
    if doc_type == "ws" and ws_reader:
        return ws_reader.library_name(f, side.get("source_file") or side.get("file") or "document", "")[:150]
    if doc_type == "pop" and f.get("account_name"):
        d = str(f.get("date") or "")
        dd = "-".join(reversed(d.split("-"))) if re.fullmatch(r"\d{4}-\d\d-\d\d", d) else ""
        name = "-".join(x for x in (str(f["account_name"]).upper(), "POP", dd) if x)
        return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#%]+', " ", name)).strip(" .")[:150]
    return None


def _complete_job(root, side, f):
    """Job numbers typed (or kept) without their letter get it from the job list and the order date, as the reader
    does. Several jobs ("5919 5911") are each completed and kept, joined with ", "."""
    po_reader.JOBS_DIR = str(root)
    typed = str(f.get("job_number") or "").upper()
    toks = re.findall(r"(?<![A-Z0-9])0*(\d{3,5})\s?([A-E])?(?![A-Z0-9])", typed)
    chk, jobs, recs = [], [], []
    for dg, lt in toks:
        job, rec = po_reader.complete_job(dg, lt or None, f.get("date"), chk, written=dg)
        if job and job not in jobs:
            jobs.append(job); recs.append(rec)
    if toks:
        if any(not lt for _, lt in toks):
            f["job_number_read"] = typed
        f["job_number"] = ", ".join(jobs) or typed
        rec = next((r for r in recs if r), None) or {}
        f["job_customer"], f["job_name"] = rec.get("customer"), rec.get("name")
    is_job = lambda c: str(c).startswith(("job ", "job:", "file name names more than one job"))
    f["checks"] = [c for c in (f.get("checks") or []) if not is_job(c)] + chk
    f["review"] = "; ".join(f["checks"]) or None
    side["flags"] = [x for x in (side.get("flags") or []) if not (str(x).startswith("check: ") and is_job(str(x)[7:]))] + ["check: " + c for c in chk]


def edit_doc(root, t, body):
    """Save corrections from the dashboard into the sidecar (before sync), with a log of what changed. Renames the
    PDF + JSON when the library file name changes (supplier, order number, date)."""
    d = root / "out" / t / ("hold" if body.get("held") else "")
    name = str(body.get("name") or "")
    js = safe_child(d, name)
    if not name.endswith(".json") or not js.is_file():
        raise ValueError("document not found (synced already?)")
    if Path(str(js) + ".uploaded").exists():
        raise ValueError("already uploaded to SharePoint: edit it there")
    side = read_json(js) or {}
    dt = side.get("doc_type") or ""
    f = side.setdefault("fields", {})
    if "fields_read" not in side:          # the model's own reading, kept for training / accuracy scoring
        side["fields_read"] = json.loads(json.dumps(f))
    changes = []
    for k, v in (body.get("fields") or {}).items():
        if k not in EDITABLE.get(dt, ()):
            continue
        new = _edit_value(k, v, dt)
        old = f.get(k)
        if (old if old != "" else None) != new:
            changes.append({"field": k, "old": old, "new": new})
            f[k] = new
    if dt in ("po", "ws") and po_reader and hasattr(po_reader, "complete_job") and any(c["field"] in ("job_number", "date") for c in changes):
        _complete_job(root, side, f)
    if body.get("items") is not None and dt in EDIT_LINES:
        cols = EDIT_LINES[dt]
        old_items = [i for i in (f.get("items") or []) if isinstance(i, dict)]
        new_items = []
        for n, row in enumerate(body["items"]):
            base = dict(old_items[n]) if n < len(old_items) else {}
            for c in cols:
                base[c] = _edit_value(c, row.get(c), dt)
            if any(base.get(c) not in (None, "") for c in cols if c != "price_unit"):
                new_items.append(base)
        sig = lambda its: [[i.get(c) for c in cols] for i in its]
        if sig(new_items) != sig(old_items):
            changes.append({"field": "items", "old": len(old_items), "new": len(new_items)})
            f["items"] = new_items
    if not changes:
        return {"ok": True, "changed": 0}
    _recompute(dt, f)
    # flags that the edit has answered
    flags = [x for x in (side.get("flags") or [])
             if not (x.startswith("missing_") and f.get(x[8:]) not in (None, "")) and x not in ("no_po", "cancelled")
             and not (dt == "ws" and ((x == "no_lines" and f.get("items")) or (x == "missing_sheet" and f.get("sheet_no"))))
             and not (dt == "test_cert" and ((x == "no_items" and f.get("items")) or (x == "family_other" and f.get("family") not in (None, "", "Other"))))]
    if dt == "po" and f.get("cancelled"):
        flags.insert(0, "cancelled")
    side["flags"], side["needs_review"] = flags, bool(flags)
    side.setdefault("edits", []).append({"at": datetime.now().isoformat(timespec="seconds"), "changes": changes})
    # rename to the new library name if it changed
    stem = _stem_for(dt, f, side)
    pdf = d / str(side.get("file") or "")
    new_js = js
    if stem and pdf.is_file() and not re.fullmatch(re.escape(stem) + r"(_\d+)?", pdf.stem):     # "_2" = a second copy: same name
        new_pdf = free_name(d, stem + pdf.suffix.lower())
        os.replace(pdf, new_pdf)
        side["file"] = new_pdf.name
        new_js = new_pdf.with_suffix(".json")
    tmp = new_js.with_name(new_js.name + ".tmp")
    tmp.write_text(json.dumps(side, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, new_js)
    if new_js != js:
        js.unlink(missing_ok=True)
        att = Path(str(js) + ".attempts")
        if att.exists():
            os.replace(att, Path(str(new_js) + ".attempts"))
    return {"ok": True, "changed": len(changes), "name": new_js.name}


# ---- training (training.py beside this file): status for the Training tab, start / cancel / revert jobs
_TRAIN = {"at": 0, "data": None}
_TRAIN_WS = {"at": 0, "data": None}
_TRAIN_JT = {"at": 0, "data": None}
_TRAIN_TC = {"at": 0, "data": None}


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def training_status(cfg, reader="po"):
    import training as T
    t = dict(T.DEFAULTS)
    t.update(cfg.get("training") or {})
    if reader == "ws":                      # workshop sheets: ws_training.py, its own folder training/ws
        import ws_training as W
        for k, v in W.DEFAULTS.items():
            t.setdefault(k, v)
        cache = _TRAIN_WS
        if time.time() - cache["at"] > 30 or cache["data"] is None:
            cache["data"], cache["at"] = W.cmd_status(cfg, t, quiet=True), time.time()
    elif reader == "tc":                    # test certificates: tc_training.py, its own folder training/tc
        import tc_training as C
        for k, v in C.DEFAULTS.items():
            t.setdefault(k, v)
        cache = _TRAIN_TC
        if time.time() - cache["at"] > 30 or cache["data"] is None:
            cache["data"], cache["at"] = C.cmd_status(cfg, t, quiet=True), time.time()
    elif reader == "jt":                    # job transfers: jt_training.py, its own folder training/jt
        import jt_training as J
        for k, v in J.DEFAULTS.items():
            t.setdefault(k, v)
        cache = _TRAIN_JT
        if time.time() - cache["at"] > 30 or cache["data"] is None:
            cache["data"], cache["at"] = J.cmd_status(cfg, t, quiet=True), time.time()
    else:
        cache = _TRAIN
        if time.time() - cache["at"] > 30 or cache["data"] is None:
            cache["data"], cache["at"] = T.cmd_status(cfg, t, quiet=True), time.time()
    d = Path(cfg["root"]) / "training"
    rd = d / reader if reader in ("ws", "jt", "tc") else d    # reports, history and the installed setup per reader
    job = read_json(d / "job.json") or {}
    if job.get("state") == "running" and not _pid_alive(job.get("pid")):
        job["state"], job["summary"] = "stopped", "the job stopped without finishing (box restarted?)"
    tail = ""
    if job.get("log") and (d / "logs" / job["log"]).exists():
        tail = "\n".join((d / "logs" / job["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
    reports = []
    for r in sorted((rd / "reports").glob("*.json"), key=lambda q: q.stat().st_mtime, reverse=True)[:12]:
        x = read_json(r) or {}
        reports.append({k: x.get(k) for k in ("id", "kind", "at", "tests", "installed", "why", "added", "model", "trained_on", "valid")} |
                       {"current": (x.get("current") or {}).get("overall"), "candidate": (x.get("candidate") or {}).get("overall")})
    return {"reader": reader, "status": cache["data"], "job": job, "log_tail": tail, "reports": reports,
            "history": len(list((rd / "history").glob("active-*.json"))) if (rd / "history").exists() else 0,
            "installed": read_json(rd / "active.json"), "schedule": training_schedule(cfg), "ws_available": (Path(__file__).resolve().with_name("ws_training.py")).exists(),
            "jt_available": (Path(__file__).resolve().with_name("jt_training.py")).exists(),
            "tc_available": (Path(__file__).resolve().with_name("tc_training.py")).exists()}


# ---- training schedule card: systemd training timers, the training service, other GPU work (runner jobs, Ollama)
_SCHED = {"at": 0, "data": None}
_RUN_DIR = "/srv/ocr/scans/pipeline/_src/run"


def _sd_show(unit, props):
    try:
        out = subprocess.run(["systemctl", "show", unit, "--timestamp=unix", "-p", ",".join(props)],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k] = v.strip()
    return d


def _sd_ts(v):
    """'@1792785600' -> ISO local time; empty / n/a -> None"""
    m = re.match(r"@(\d+)", v or "")
    if m:
        return datetime.fromtimestamp(int(m.group(1))).isoformat(timespec="seconds") if int(m.group(1)) > 0 else None
    m = re.search(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)", v or "")
    return f"{m.group(1)}T{m.group(2)}" if m else None


def training_schedule(cfg):
    if time.time() - _SCHED["at"] < 10 and _SCHED["data"] is not None:
        return _SCHED["data"]
    timers = []
    try:
        names = subprocess.run(["systemctl", "list-units", "--all", "--type=timer", "--no-legend", "--plain"],
                               capture_output=True, text=True, timeout=5).stdout.split("\n")
        names = [n.split()[0] for n in names if n.strip()]
    except Exception:
        names = []
    for t in names:
        tp = _sd_show(t, ["Id", "Description", "Unit", "NextElapseUSecRealtime", "LastTriggerUSec", "ActiveState"])
        svc = tp.get("Unit") or t.replace(".timer", ".service")
        sp = _sd_show(svc, ["Description", "ExecStart", "ActiveState", "SubState", "Result", "ExecMainStartTimestamp"])
        exe = sp.get("ExecStart", "")
        if not (t.startswith(("mps-finetune", "mps-train", "mps-quick", "mps-po-", "mps-tc-")) or re.search(r"(ws_)?training\.py", exe)):
            continue
        what = "Full training (fine-tune)" if "finetune" in exe or "finetune" in t else "Quick training" if "quick" in exe else (sp.get("Description") or t)
        reader = "Workshop sheets" if "ws_training" in exe else "Job transfers" if "jt_training" in exe else "Purchase orders" if "training.py" in exe else ""
        _daily = {"mps-po-feed.timer": ("Feed 1,500 archive orders", "Purchase orders"),
                  "mps-tc-feed.timer": ("Feed 250 backfill files", "Test certificates"),
                  "mps-po-check.timer": ("Checking file when the batch is read", "Purchase orders"),
                  "mps-po-import.timer": ("Import rows marked Use as Checked", "Purchase orders"),
                  "mps-ocr-training.timer": ("Training queue (quick training)", "PO, workshop sheets, job transfers")}
        if t in _daily:
            what, reader = _daily[t]
        state = sp.get("ActiveState")
        note = ""
        if state == "activating":
            try:
                note = subprocess.run(["journalctl", "-u", svc, "-n", "1", "-o", "cat", "--no-pager"], capture_output=True,
                                      text=True, timeout=5).stdout.strip()[-160:]
            except Exception:
                pass
        timers.append({"timer": t, "service": svc, "what": what, "reader": reader, "next": _sd_ts(tp.get("NextElapseUSecRealtime")),
                       "last": _sd_ts(tp.get("LastTriggerUSec")), "timer_state": tp.get("ActiveState"), "state": state,
                       "result": sp.get("Result"), "started": _sd_ts(sp.get("ExecMainStartTimestamp")), "note": note})
    timers.sort(key=lambda x: x["next"] or "9999")
    runner = []
    rd = Path(_RUN_DIR)
    if rd.is_dir():
        for f in sorted(rd.glob("*.sh")):
            if f.stem in ("mps-runner", "install-runner"):
                continue
            log, rc = rd / "done" / (f.stem + ".log"), rd / "done" / (f.stem + ".rc")
            running = log.exists() and not rc.exists()
            about, since = "", None
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    about = next((l[1:].strip() for l in fh if l.startswith("#") and not l.startswith("#!")), "")
                if running:
                    with open(log, encoding="utf-8", errors="replace") as fh:
                        m = re.match(r"(\S+) running", fh.readline())
                    since = m.group(1)[:19] if m else None
            except OSError:
                pass
            runner.append({"name": f.stem, "state": "running" if running else "queued", "about": about[:140], "since": since})
    loaded = []
    try:
        with urllib.request.urlopen(cfg["ollama_url"].rstrip("/") + "/api/ps", timeout=3) as r:
            loaded = [{"name": m.get("name"), "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1), "until": m.get("expires_at")}
                      for m in json.loads(r.read()).get("models", [])]
    except Exception:
        loaded = None
    data = {"timers": timers, "runner": runner, "ollama": loaded,
            "paused": (Path(cfg["root"]) / "PAUSE").exists(), "at": datetime.now().isoformat(timespec="seconds")}
    _SCHED["data"], _SCHED["at"] = data, time.time()
    return data


def training_action(cfg, a, body):
    d = Path(cfg["root"]) / "training"
    d.mkdir(parents=True, exist_ok=True)
    job = read_json(d / "job.json") or {}
    running = job.get("state") == "running" and _pid_alive(job.get("pid"))
    script = {"ws": "ws_training.py", "jt": "jt_training.py", "tc": "tc_training.py"}.get(body.get("reader"), "training.py")
    if a == "train_start":
        kind = body.get("job")
        if kind not in ("quick", "finetune", "resume") or (kind == "resume" and script != "training.py") or (kind == "finetune" and script in ("jt_training.py", "tc_training.py")):
            raise ValueError("unknown training job")
        if running:
            raise ValueError(f"a {job.get('kind')} job is already running")
        (d / "logs").mkdir(exist_ok=True)
        err = open(d / "logs" / "start.log", "a")
        args = ["finetune", "--resume"] if kind == "resume" else [kind] + (["--force"] if body.get("force") else [])
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().with_name(script))] + args,
                         stdout=subprocess.DEVNULL, stderr=err, start_new_session=True, cwd=str(Path(__file__).resolve().parent),
                         env=dict(os.environ, MPS_OCR_CONFIG=str(CONFIG_PATH)))
        _TRAIN["at"] = _TRAIN_WS["at"] = _TRAIN_JT["at"] = _TRAIN_TC["at"] = 0
        return {"ok": True}
    if a == "train_cancel":
        if not running:
            raise ValueError("no training job is running")
        os.kill(int(job["pid"]), 15)
        return {"ok": True}
    if a == "train_revert":
        if running:
            raise ValueError("wait for the running job to finish")
        if body.get("reader") == "ws":
            import ws_training as W
            W.cmd_revert(cfg)
        elif body.get("reader") == "jt":
            import jt_training as J
            J.cmd_revert(cfg)
        elif body.get("reader") == "tc":
            import tc_training as C
            C.cmd_revert(cfg)
        else:
            import training as T
            T.cmd_revert(cfg)
        _TRAIN["at"] = _TRAIN_WS["at"] = _TRAIN_JT["at"] = _TRAIN_TC["at"] = 0
        return {"ok": True}
    raise ValueError("unknown action")


def move_doc(d_from, d_to, json_name, json_last):
    """Move a sidecar JSON with its PDF and markers. The PC only syncs JSONs directly in out/<Type>, so moving the
    JSON first (hold) or last (release) means it never sees half a pair."""
    js = safe_child(d_from, json_name)
    if not json_name.endswith(".json"):
        raise ValueError("not a document")
    side = read_json(js) or {}
    d_to.mkdir(parents=True, exist_ok=True)
    others = [Path(d_from, n) for n in ((side.get("file") or ""), json_name + ".uploaded", json_name + ".attempts")
              if n and n != json_name and Path(d_from, n).is_file()]
    for q in others + [js]:
        if Path(d_to, q.name).exists():
            raise ValueError(f"{q.name} already exists in {d_to.name}")
    order = (others + [js]) if json_last else ([js] + others)
    for q in order:
        shutil.move(str(q), str(Path(d_to, q.name)))


# --------------------------------------------------------------------------- actions
def flag_path(cfg, flag):
    root = Path(cfg["root"])
    types = set(cfg["doc_types"])
    if flag == "PAUSE":
        return root / "PAUSE"
    m = re.fullmatch(r"(collect_off|sync_off|process_off)(?:_(\w+))?", flag or "")
    if not m or (m.group(1) == "process_off" and not m.group(2)) or (m.group(2) and m.group(2) not in types):
        raise ValueError("unknown switch")
    return root / "flags" / flag


def safe_child(directory, name):
    """Only accept a name that is actually listed in that directory (no path tricks)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("bad file name")
    if name not in os.listdir(directory):
        raise ValueError("file not found")
    return Path(directory) / name


def free_name(directory, name):
    p = Path(directory) / name
    n = 2
    while p.exists():
        p = Path(directory) / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return p


def do_action(cfg, body):
    """Document actions take the sidecar's lock first (review F10, 28 Oct): the PC sync holds it while it uploads
    and moves the pair, the numbers pass and the import while they rewrite it."""
    a, t, name = body.get("action"), body.get("type"), str(body.get("name") or "")
    if a in ("edit", "check", "delete", "hold", "release") and name and t in cfg.get("doc_types", {}):
        d = Path(cfg["root"]) / "out" / t
        js = (d / "hold" / name) if (a in ("delete", "release") or (a not in ("hold",) and body.get("held"))) else (d / name)
        try:
            with sidecar_lock.locked(js, "dashboard", wait=15):
                return _do_action(cfg, body)
        except sidecar_lock.Locked as e:
            raise ValueError(f"{e}: it is being synced or edited; try again in a minute")
    return _do_action(cfg, body)


def _do_action(cfg, body):
    root = Path(cfg["root"])
    a = body.get("action")
    if a == "add_unit":
        return add_unit(cfg, body.get("name"))

    if a == "add_writer":
        return add_writer(cfg, body.get("name"))

    if a in ("train_start", "train_cancel", "train_revert"):
        return training_action(cfg, a, body)

    if a == "clear_alert":
        name = str(body.get("flag") or "")
        if not re.fullmatch(r"alert_[\w.@-]+", name):
            raise ValueError("not an alert flag")
        (root / "flags" / name).unlink(missing_ok=True)
        return {"ok": True}

    if a == "set_switch":
        p = flag_path(cfg, body.get("flag"))
        p.parent.mkdir(parents=True, exist_ok=True)
        if body.get("off"):
            p.write_text(f"set from dashboard {datetime.now().isoformat(timespec='seconds')}\n")
        else:
            p.unlink(missing_ok=True)
        return {"ok": True}

    if a == "service":
        name, op = body.get("name"), body.get("op")
        if name not in cfg.get("dashboard", {}).get("services", []) or op not in ("start", "stop", "restart"):
            raise ValueError("not allowed")
        rc, out, err = run(["sudo", "-n", "/usr/bin/systemctl", op, name], 60)
        if rc != 0:
            raise RuntimeError(err or f"systemctl {op} {name} failed")
        return {"ok": True}

    t = body.get("type")
    if t not in cfg["doc_types"]:
        raise ValueError("unknown type")

    if a == "sync_now":
        flags = root / "flags"
        if (flags / "sync_off").exists() or (flags / f"sync_off_{t}").exists():
            raise ValueError("sync is switched off for this type")
        if (flags / f"sync_now_{t}.running").exists():
            raise ValueError("a sync of this type is already running")
        flags.mkdir(parents=True, exist_ok=True)
        (flags / f"sync_now_{t}").write_text(f"requested from dashboard {datetime.now().isoformat(timespec='seconds')}\n")
        return {"ok": True}

    if a == "cancel_sync":
        (root / "flags" / f"sync_now_{t}").unlink(missing_ok=True)
        return {"ok": True}

    if a == "edit":
        return edit_doc(root, t, body)

    if a == "delete":
        # held documents only: out of the pipeline into deleted/<Type>/<yyyy-mm>/ (recoverable); never synced
        held = root / "out" / t / "hold"
        name = str(body.get("name") or "")
        if not name.endswith(".json") or not (held / name).is_file():
            raise ValueError("only documents on hold can be deleted (Hold it first)")
        js = safe_child(held, name)
        if Path(str(js) + ".uploaded").exists():
            raise ValueError("already uploaded to SharePoint: delete it there")
        side = read_json(js) or {}
        dest = root / "deleted" / t / datetime.now().strftime("%Y-%m")
        dest.mkdir(parents=True, exist_ok=True)
        side["deleted_at"] = datetime.now().isoformat(timespec="seconds")
        for q in [held / n for n in (side.get("file") or "", name + ".attempts") if n] + [js]:
            if q.is_file():
                shutil.move(str(q), str(free_name(dest, q.name)))
        (dest / (Path(name).stem + ".deleted.json")).write_text(json.dumps({"name": name, "at": side["deleted_at"],
                                                                         "source_file": side.get("source_file")}), encoding="utf-8")
        return {"ok": True}

    if a == "check":
        # the reviewer's tick (training sheet "Checked"); goes to the PO library's Checked column on sync
        d = root / "out" / t / ("hold" if body.get("held") else "")
        name = str(body.get("name") or "")
        if not name.endswith(".json"):
            raise ValueError("not a document")
        js = safe_child(d, name)
        side = read_json(js)
        if side is None:
            raise ValueError("document not found (synced already?)")
        side.setdefault("fields", {})["checked"] = bool(body.get("value"))
        side["checked_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = js.with_name(js.name + ".tmp")
        tmp.write_text(json.dumps(side, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, js)
        return {"ok": True}

    if a in ("hold", "release"):
        out = root / "out" / t
        if a == "hold":
            move_doc(out, out / "hold", body.get("name"), json_last=False)
        else:
            move_doc(out / "hold", out, body.get("name"), json_last=True)
        return {"ok": True}

    if a in ("retry_failed", "retry_failed_all"):
        d = root / "failed" / t
        names = [body.get("name")] if a == "retry_failed" else \
            [n for n in os.listdir(d) if not n.endswith(".error.txt")]
        for n in names:
            src = safe_child(d, n)
            shutil.move(str(src), str(free_name(root / "in" / t, n)))
            Path(d, n + ".error.txt").unlink(missing_ok=True)
        return {"ok": True, "moved": len(names)}

    if a in ("retry_sync", "retry_sync_all"):
        d = root / "sync_failed" / t
        names = [body.get("name")] if a == "retry_sync" else [n for n in os.listdir(d) if n.endswith(".json")]
        for n in names:
            js = safe_child(d, n)
            side = read_json(js) or {}
            out = root / "out" / t
            pdf = d / side.get("file", "")
            if side.get("file") and pdf.exists():
                shutil.move(str(pdf), str(out / pdf.name))
            uploaded = Path(str(js) + ".uploaded")
            if uploaded.exists():
                shutil.move(str(uploaded), str(out / uploaded.name))
            for extra in (".attempts", ".error.txt"):
                Path(str(js) + extra).unlink(missing_ok=True)
            shutil.move(str(js), str(out / js.name))          # JSON last: the PC only picks up once it's there
        return {"ok": True, "moved": len(names)}

    raise ValueError("unknown action")


def logs(cfg, svc, n=120):
    if svc not in cfg.get("dashboard", {}).get("services", []):
        raise ValueError("not allowed")
    rc, out, err = run(["journalctl", "-u", svc, "-n", str(n), "--no-pager", "-o", "short-iso"], 15)
    return out if rc == 0 else f"(journalctl failed: {err}. Is the dashboard user in the systemd-journal group?)"


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "MPS-OCR-Dashboard"

    def log_message(self, fmt, *args):
        if self.command != "GET" or not self.path.startswith(("/api/status", "/api/missing_vat", "/api/out", "/api/training")):
            sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % args))

    # ---- helpers
    def session(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["mps_ocr"].value if "mps_ocr" in c else None
        with LOCK:
            s = SESSIONS.get(tok)
            if s and s["exp"] > time.time():
                return tok, s
            SESSIONS.pop(tok, None)
        return None, None

    def send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        textual = ctype.startswith("text/") or ctype == "application/json"
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if textual and "charset" not in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ValueError("too large")
        return self.rfile.read(n).decode() if n else ""

    # ---- routes
    def do_GET(self):
        cfg = load_cfg()
        path = urlparse(self.path).path
        tok, s = self.session()
        if path == "/":
            return self.send(200, (APP_HTML if s else LOGIN_HTML).replace("__CSRF__", s["csrf"] if s else ""), "text/html")
        if not s:
            return self.send(401, {"error": "login required"})
        if path == "/api/status":
            return self.send(200, status(cfg))
        if path in ("/api/out", "/api/pdf"):
            q = parse_qs(urlparse(self.path).query)
            t, name = q.get("type", [""])[0], q.get("name", [""])[0]
            try:
                if path == "/api/out":
                    g = lambda n: q.get(n, [""])[0]
                    sorts = {"ready": (g("sort") or None, g("dir") == "desc"), "held": (g("hsort") or None, g("hdir") == "desc")}
                    return self.send(200, out_queue(cfg, t, sorts=sorts))
                p = out_pdf(cfg, t, name, q.get("held", ["0"])[0] == "1")
                safe = re.sub(r'[^\w .()\-]', "_", p.name)
                return self.send(200, p.read_bytes(), "application/pdf", {"Content-Disposition": f'inline; filename="{safe}"'})
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if path == "/api/training":
            try:
                rdr = parse_qs(urlparse(self.path).query).get("reader", ["po"])[0]
                return self.send(200, training_status(cfg, rdr if rdr in ("ws", "jt", "tc") else "po"))
            except Exception as e:
                return self.send(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/missing_vat":
            try:
                return self.send(200, missing_vat(cfg))
            except Exception as e:
                return self.send(500, {"error": str(e)})
        if path == "/api/logs":
            svc = parse_qs(urlparse(self.path).query).get("svc", [""])[0]
            try:
                return self.send(200, logs(cfg, svc), "text/plain")
            except ValueError as e:
                return self.send(400, str(e), "text/plain")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        cfg = load_cfg()
        path = urlparse(self.path).path
        ip = self.client_address[0]
        if path == "/login":
            with LOCK:
                recent = [t for t in FAILS.get(ip, []) if t > time.time() - 600]
                FAILS[ip] = recent
            if len(recent) >= 8:
                return self.send(429, "Too many attempts. Try again in 10 minutes.", "text/plain")
            pw = parse_qs(self.body()).get("password", [""])[0]
            stored = cfg.get("dashboard", {}).get("password_hash", "")
            if stored and check_password(pw, stored):
                tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                hours = float(cfg.get("dashboard", {}).get("session_hours", 12))
                with LOCK:
                    SESSIONS[tok] = {"exp": time.time() + hours * 3600, "csrf": csrf}
                return self.send(303, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"mps_ocr={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(hours*3600)}"})
            with LOCK:
                FAILS.setdefault(ip, []).append(time.time())
            time.sleep(1)
            return self.send(303, b"", "text/plain", {"Location": "/?bad=1"})

        tok, s = self.session()
        if not s:
            return self.send(401, {"error": "login required"})
        if not hmac.compare_digest(self.headers.get("X-CSRF", ""), s["csrf"]):
            return self.send(403, {"error": "bad csrf token"})
        if path == "/logout":
            with LOCK:
                SESSIONS.pop(tok, None)
            return self.send(200, {"ok": True}, headers={"Set-Cookie": "mps_ocr=; Max-Age=0; Path=/"})
        if path == "/api/action":
            try:
                return self.send(200, do_action(cfg, json.loads(self.body() or "{}")))
            except (ValueError, RuntimeError, OSError) as e:
                return self.send(400, {"ok": False, "error": str(e)})
        self.send(404, {"error": "not found"})


# --------------------------------------------------------------------------- pages
LOGIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>MPS OCR</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2330;--muted:#6b7280;--line:#e3e6eb;--accent:#1f6feb;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#181c24;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2a303b;--accent:#4c8dff;--bad:#ef5350}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px;width:min(340px,calc(100vw - 32px));box-sizing:border-box}
h1{font-size:18px;margin:0 0 4px}p{color:var(--muted);margin:0 0 18px;font-size:13px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--ink);font-size:15px}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-size:15px;cursor:pointer}
.err{color:var(--bad);font-size:13px;margin-top:10px;display:none}
</style></head><body>
<form method="post" action="/login"><h1>MPS OCR pipeline</h1><p>mps-frigate dashboard</p>
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<button>Sign in</button><div class="err" id="e">Wrong password.</div></form>
<script>if(location.search.includes('bad'))document.getElementById('e').style.display='block'</script>
</body></html>"""

APP_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>MPS OCR</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2330;--muted:#6b7280;--line:#e3e6eb;--accent:#1f6feb;
--ok:#1e8e3e;--warn:#b26a00;--bad:#c62828;--off:#9aa0a6;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#181c24;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2a303b;
--accent:#4c8dff;--ok:#4caf50;--warn:#ffb300;--bad:#ef5350;--off:#6b7280;--chip:#222833}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.top{position:sticky;top:0;z-index:2;background:var(--card);border-bottom:1px solid var(--line)}
header{display:flex;align-items:center;gap:12px;padding:14px 16px 10px}
nav.tabs{display:flex;gap:2px;padding:0 12px;overflow-x:auto;scrollbar-width:none}
nav.tabs button{border:0;border-bottom:2px solid transparent;border-radius:0;background:none;padding:9px 12px;font-size:13px;color:var(--muted);white-space:nowrap}
nav.tabs button:hover{color:var(--ink);border-color:var(--line)}
nav.tabs button.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
nav.tabs .n{display:inline-block;min-width:18px;margin-left:6px;padding:0 5px;border-radius:9px;background:var(--bad);color:#fff;font-size:11px;line-height:17px;text-align:center}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px;margin-top:10px}
.stat{background:var(--chip);border-radius:8px;padding:8px 10px}.stat b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.stat span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.vat td.mono,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px;white-space:nowrap}
details summary{cursor:pointer;color:var(--muted);font-size:13px;margin-top:12px}
[hidden]{display:none!important}
.qhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.qhead h2{margin:0;flex:1}
.qstat{font-size:12px;margin:8px 0 4px}.qstat.warn{color:var(--warn)}.qstat.bad{color:var(--bad)}
table.q td{font-size:13px;white-space:nowrap}table.q td:first-child,table.q td.chk{white-space:normal}table.q tr.doc{cursor:pointer}table.q tr.doc:hover td{background:var(--chip)}
table.q tr.det td{background:var(--chip);padding:8px 10px;white-space:normal}table.q td.act{white-space:nowrap;text-align:right}
.tag{display:inline-block;font-size:11px;line-height:16px;padding:0 6px;border-radius:8px;margin:1px 2px 1px 0;background:var(--chip);border:1px solid var(--line);white-space:nowrap}
.tag.warn{color:var(--warn);border-color:var(--warn)}.tag.bad{color:var(--bad);border-color:var(--bad)}.tag.ok{color:var(--ok);border-color:var(--ok)}
table.lines td,table.lines th{font-size:12px;padding:4px 6px}
a.btn{font-size:12px;padding:5px 10px;border-radius:7px;border:1px solid var(--line);background:var(--chip);color:var(--ink);text-decoration:none;display:inline-block}
a.btn:hover{border-color:var(--accent)}
.sp{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:4px 0 10px;background:var(--card)}
.sp .path{font-size:13px;word-break:break-word}.sp .path b{font-weight:600}
.sec{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600;margin:2px 0 4px}
header h1{font-size:16px;margin:0;flex:1}header .sub{color:var(--muted);font-size:12px}
main{max-width:1180px;margin:0 auto;padding:16px;display:grid;gap:16px}
main>*,.grid>*,#outq>*{min-width:0}
table.q td.wrap{min-width:180px;max-width:320px;white-space:normal}
/* wide screens (4K): use the full width; job tabs get a side column (switches, stats, failed, recent) and the queue */
@media (min-width:1600px){
  main{max-width:none;padding:16px 24px}
  #tab-job:not([hidden]){grid-template-columns:340px minmax(0,1fr);grid-template-areas:"cards q" "side q";grid-template-rows:auto 1fr;align-items:start}
  #jobcards{grid-area:cards;grid-template-columns:1fr!important}#qwrap{grid-area:q}#jobside{grid-area:side;grid-template-columns:1fr!important}
  #jobside .list{max-height:420px}
}
.detgrid{display:grid;gap:14px;grid-template-columns:minmax(0,1fr)}
.detgrid.editing>.dl{grid-column:1/-1!important}
.detgrid>.rv{grid-column:1/-1}.rv .sp{border-left:4px solid var(--warn,#b26a00);margin-bottom:0}   /* while editing, the line items get the full width */
/* details card: header | Sage + SharePoint side by side across the full width, line items underneath */
@media (min-width:1300px){.detgrid{grid-template-columns:minmax(0,1.15fr) minmax(0,1fr)}.detgrid>.dl{grid-column:1/-1}}
.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:0 14px;font-size:12px}
@media (min-width:900px){.kv{grid-template-columns:max-content minmax(150px,1fr) max-content minmax(150px,1fr)}}
.kv>div{padding:4px 2px;border-bottom:1px solid var(--line);min-width:0;overflow-wrap:anywhere}.kv .k{color:var(--muted);white-space:nowrap}
.kv .v{white-space:pre-wrap}.kv .wide{grid-column:span 3}
table.lines select{font:inherit;font-size:12px;padding:3px 4px;border:1px solid var(--accent);border-radius:5px;background:var(--card);color:var(--ink)}
.kv select{width:100%;font:inherit;font-size:12px;padding:3px 5px;border:1px solid var(--accent);border-radius:5px;background:var(--card);color:var(--ink)}
.kv input,.kv textarea,table.lines input{width:100%;font:inherit;font-size:12px;padding:3px 5px;border:1px solid var(--accent);border-radius:5px;background:var(--card);color:var(--ink)}
.kv textarea{min-height:46px;resize:vertical}
table.lines input[data-c=quantity],table.lines input[data-c=item_no],table.lines input[data-c=unit]{width:calc(7ch + 14px)}
table.lines input[data-c=unit_price],table.lines input[data-c=amount]{width:calc(11ch + 14px)}
table.lines select[data-c=price_unit]{width:calc(7ch + 30px)}
table.lines td:has([data-c=description]){width:100%}
table.lines textarea[data-c=description]{display:block;width:100%;min-width:220px;font:inherit;font-size:12px;line-height:1.35;padding:3px 5px;
  border:1px solid var(--accent);border-radius:5px;background:var(--card);color:var(--ink);resize:vertical;field-sizing:content;min-height:calc(1.35em + 8px);max-height:calc(2.7em + 8px);overflow:auto}
.editbar{display:flex;gap:6px;align-items:center;margin:2px 0 8px;flex-wrap:wrap}
table.lines.lt-test_cert{width:100%}
table.lines.lt-test_cert td:has([data-c=description]){width:auto;min-width:340px}
table.lines.lt-test_cert textarea[data-c=description]{min-width:0;width:100%;field-sizing:fixed}
table.lines.lt-test_cert input[data-c=family],table.lines.lt-test_cert input[data-c=ends]{width:calc(8ch + 14px)}
table.lines.lt-test_cert input[data-c=product]{width:calc(12ch + 14px)}
table.lines.lt-test_cert input[data-c=size_text]{width:calc(18ch + 14px)}
table.lines.lt-test_cert input[data-c=size_nb],table.lines.lt-test_cert input[data-c=qty],table.lines.lt-test_cert input[data-c=length_m]{width:calc(7ch + 14px)}
table.lines.lt-test_cert input[data-c=schedule],table.lines.lt-test_cert input[data-c=class]{width:calc(8ch + 14px)}
table.lines.lt-test_cert input[data-c=grade]{width:calc(11ch + 14px)}
table.lines.lt-test_cert input[data-c=heat_no]{width:calc(14ch + 14px)}
.editbar .grow{flex:1}
.tag.edited{color:var(--accent);border-color:var(--accent)}
table.q td.tickc{text-align:center}
.qscroll{max-height:70vh;overflow:auto}
table.q thead th{position:sticky;top:0;z-index:2;background:var(--card);box-shadow:inset 0 -1px 0 var(--line)}
table.q th.sortable{cursor:pointer;user-select:none;white-space:nowrap}table.q th.sortable:hover{color:var(--ink)}
table.q th.sorted{color:var(--accent)}
table.q input.tick{width:18px;height:18px;cursor:pointer}
@media (max-width:640px){main{padding:10px}.grid{grid-template-columns:1fr 1fr}th,td{padding:7px 4px}}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 8px;display:flex;align-items:center;gap:8px}
.big{font-size:17px;font-weight:600}.muted{color:var(--muted)}.small{font-size:12px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;background:var(--off);flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.row{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
button{font:inherit;font-size:12px;padding:5px 10px;border-radius:7px;border:1px solid var(--line);background:var(--chip);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad)}button:disabled{opacity:.5;cursor:default}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}th.num{text-align:right}
.sw{position:relative;display:inline-block;width:38px;height:22px;flex:none}.sw input{opacity:0;width:0;height:0}
.sw span{position:absolute;inset:0;background:var(--off);border-radius:22px;transition:.15s;cursor:pointer}
.sw span:before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}
.sw input:checked+span{background:var(--ok)}.sw input:checked+span:before{transform:translateX(16px)}
.sw input:disabled+span{opacity:.45;cursor:default}
.label{display:flex;align-items:center;gap:8px;margin-top:8px}
.bar{height:6px;background:var(--chip);border-radius:6px;overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;background:var(--accent)}
.list{max-height:260px;overflow:auto}.item{display:flex;gap:8px;align-items:flex-start;padding:7px 0;border-bottom:1px solid var(--line)}
.item .grow{flex:1;min-width:0}.item .name{font-weight:500;word-break:break-all}.err{color:var(--bad);font-size:12px;word-break:break-word}
pre{margin:0;max-height:380px;overflow:auto;font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-all}
select{font:inherit;padding:5px 8px;border-radius:7px;border:1px solid var(--line);background:var(--chip);color:var(--ink)}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:8px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}
.toast.show{opacity:1}
.scroll{overflow-x:auto}
@media (max-width:640px){.hide-sm{display:none}}
</style></head><body>
<div class="top"><header><h1>MPS OCR pipeline</h1><span class="sub" id="upd">loading…</span>
<button id="logout">Sign out</button></header><nav class="tabs" id="tabs"></nav></div>
<main id="tab-overview">
<section class="grid" id="health"></section>
<section class="card"><h2>Document types</h2>
<div class="scroll"><table><thead><tr><th>Type</th><th>Collect</th><th>Process</th><th>Sync</th>
<th class="num">In queue</th><th class="num">To sync</th><th class="num hide-sm">Done today</th><th class="num hide-sm">Synced today</th>
<th class="num">Failed</th><th class="num">Sync failed</th></tr></thead><tbody id="types"></tbody></table></div>
<div class="small muted" style="margin-top:8px">Switching a type off stops new work of that type; anything already in progress finishes first.</div>
</section>
<section class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))">
<div class="card"><h2>Failed files</h2><div class="list" id="failed"></div></div>
<div class="card"><h2>Recent activity</h2><div class="list" id="recent"></div></div>
</section>
<section class="card"><h2>Logs <select id="svc"></select> <button id="loadlogs">Refresh</button></h2><pre id="logs" class="muted">Choose a service.</pre></section>
</main>
<main id="tab-job" hidden>
<section class="grid" id="jobcards" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))"></section>
<div id="qwrap" style="display:grid;gap:16px;min-width:0">
<div id="outq" style="display:grid;gap:16px"></div>
<section class="card" id="vatcard" hidden><h2>VAT numbers not on the Sage supplier list
<button id="vatrefresh" style="margin-left:auto">Refresh</button></h2>
<div id="vat" class="muted small">Loading…</div></section>
</div>
<section class="grid" id="jobside" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))">
<div class="card"><h2>Failed files</h2><div class="list" id="jobfailed"></div></div>
<div class="card"><h2>Recent activity</h2><div class="list" id="jobrecent"></div></div>
</section>
</main>
<main id="tab-train" hidden>
<section class="card" id="trsched"><div class="muted small">Loading…</div></section>
<section class="grid" id="trcards" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))"></section>
<section class="card" id="trjob"><div class="muted small">Loading…</div></section>
<section class="grid" style="grid-template-columns:repeat(auto-fit,minmax(460px,1fr))">
<div class="card" id="tracc"></div><div class="card" id="trrep"></div></section>
</main>
<div class="toast" id="toast"></div>
<script>
const CSRF="__CSRF__";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let busy=false,last=null;
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),2600)}
function ago(s){if(s==null)return"never";if(s<60)return s+"s ago";if(s<3600)return Math.round(s/60)+" min ago";if(s<86400)return Math.round(s/3600)+" h ago";return Math.round(s/86400)+" d ago"}
async function act(body,msg){
  busy=true;try{const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF":CSRF},body:JSON.stringify(body)});
  const j=await r.json();if(r.status==401)return location.reload();if(!j.ok)throw new Error(j.error||"failed");toast(msg||"Done")}
  catch(e){toast("Error: "+e.message)}finally{busy=false;refresh()}}
function sw(flag,on,title,disabled){return `<label class="sw" title="${esc(title)}"><input type="checkbox" ${on?"checked":""} ${disabled?"disabled":""}
  onchange="act({action:'set_switch',flag:'${flag}',off:!this.checked},'${esc(title)} '+(this.checked?'on':'off'))"><span></span></label>`}
function svcCard(title,s,extra,paused){
  const st=s?s.active:"unknown";const cls=paused?"warn":st=="active"?"ok":st=="activating"?"warn":"bad";
  const txt=paused?"paused":st=="active"?"running":st;
  return `<div class="card"><h2><span class="dot ${cls}"></span>${title}</h2><div class="big">${esc(txt)}</div>
  <div class="small muted">${s&&s.since&&st=="active"?"since "+esc(s.since.replace(/^\w+ /,"")):""}${s&&+s.restarts?" · "+s.restarts+" restarts":""}</div>${extra||""}
  <div class="row">${st=="active"?`<button onclick="act({action:'service',name:'${s.name}',op:'restart'},'Restarting')">Restart</button>
  <button class="danger" onclick="if(confirm('Stop ${title}?'))act({action:'service',name:'${s.name}',op:'stop'},'Stopping')">Stop</button>`
  :`<button class="primary" onclick="act({action:'service',name:'${s?s.name:''}',op:'start'},'Starting')">Start</button>`}</div></div>`}
function render(d){
  last=d;$("#upd").textContent=d.host+" · updated "+new Date(d.now).toLocaleTimeString();
  const svc=n=>d.services.find(s=>s.name==n);
  const w=d.worker||{},cur=w.current;
  let wx=`<div class="label">${sw("PAUSE",!d.worker_paused,"GPU work")}<span class="small">GPU work ${d.worker_paused?"paused (CPU invoices continue)":"on"}</span></div>`;
  if(w.waiting_for_gpu)wx+=`<div class="small muted">${w.waiting_for_gpu} invoice(s) waiting for the GPU</div>`;
  if(cur)wx=`<div class="small" style="margin-top:6px">Now: <b>${esc(cur.file)}</b> <span class="muted">(${esc(cur.type)})</span></div>`+wx;
  else if(w.last_done)wx=`<div class="small muted" style="margin-top:6px">Last: ${esc(w.last_done.file)} · ${ago(Math.round((Date.now()-new Date(w.last_done.at))/1000))}</div>`+wx;
  const c=d.collector||{},srcs=Object.entries(c.sources||{});
  let cx=d.collector_configured?srcs.map(([k,v])=>`<div class="small"><span class="dot ${v.state=="ok"?"ok":v.state=="off"?"":"bad"}"></span> ${esc(k)}: ${esc(v.state)}${v.last_scan?" · scanned "+ago(Math.round((Date.now()-new Date(v.last_scan))/1000)):""}</div>`).join("")
    :`<div class="small muted">No NAS folders configured yet.</div>`;
  cx+=`<div class="label">${sw("collect_off",d.collect_all_on,"NAS collecting",!d.collector_configured)}<span class="small">Collecting ${d.collect_all_on?"on":"off"}</span></div>`;
  const o=d.ollama;
  const ox=`<div class="card"><h2><span class="dot ${o.up?"ok":"bad"}"></span>Ollama</h2><div class="big">${o.up?"up":"down"}</div>
    <div class="small muted">${o.version?"v"+esc(o.version):esc(o.error||"")}</div>
    ${(o.loaded||[]).map(m=>`<div class="small">Loaded: <b>${esc(m.name)}</b> · ${m.vram_mb} MB</div>`).join("")||`<div class="small muted">No model loaded</div>`}</div>`;
  const g=d.gpu;let gx;
  if(!g.present||g.error)gx=`<div class="card"><h2><span class="dot bad"></span>GPU</h2><div class="big">${g.present?"error":"not found"}</div><div class="small muted">${esc(g.error||"")}</div></div>`;
  else{const pct=Math.round(100*g.mem_used/g.mem_total);
    gx=`<div class="card"><h2><span class="dot ${+g.util>0?"ok":""}"></span>GPU</h2><div class="big">${g.util}% · ${g.temp}°C</div>
    <div class="small muted">${esc(g.name)} · ${(g.mem_used/1024).toFixed(1)} / ${(g.mem_total/1024).toFixed(0)} GB</div><div class="bar"><i style="width:${pct}%"></i></div>
    ${g.procs.map(p=>`<div class="small" title="${esc(p.cmd)}" style="margin-top:4px">${esc(p.name)} · ${p.mem_mb} MB <span class="muted">${esc(p.cmd.includes("train")?"(training)":p.cmd.includes("ollama")?"(Ollama)":"")}</span></div>`).join("")}</div>`}
  const sy=d.sync||{},every=d.sync_every_minutes||15;const stale=sy.age==null||sy.age>every*120+120;
  const scls=!d.sync_all_on||sy.result=="paused"?"warn":stale?"bad":(sy.result=="ok"||sy.result=="idle")?"ok":"warn";
  const sx=`<div class="card"><h2><span class="dot ${scls}"></span>PC sync (${esc(sy.host||"MPS-DINO2023")})</h2>
    <div class="big">${!d.sync_all_on?"switched off":stale?"no heartbeat":sy.result=="idle"?"waiting for Sync now":esc(sy.result||"?")}</div>
    <div class="small muted">Last check ${ago(sy.age)} · every ${every} min${sy.synced?" · synced "+sy.synced:""}${sy.failed?" · "+sy.failed+" failed":""}</div>
    ${sy.message?`<div class="err">${esc(sy.message)}</div>`:""}
    <div class="label">${sw("sync_off",d.sync_all_on,"PC sync")}<span class="small">Sync ${d.sync_all_on?"on":"off"}</span></div></div>`;
  const so=d.storage||{},ra=so.raid,bk=so.backup,arrs=so.arrays||(ra?[ra]:[]),vols=so.volumes||[];
  const raidBad=arrs.some(a=>!a.healthy),raidSync=arrs.find(a=>a.sync);
  const volFull=vols.some(v=>!v.missing&&v.pct>=95),volWarn=vols.some(v=>v.missing||v.pct>=85);
  const bkAge=bk?bk.age:null,bkStale=bkAge==null||bkAge>26*3600,bkBad=!bk||bk.result!="ok";
  const diskCls=d.disk.free_gb<20?"bad":d.disk.free_gb<60?"warn":"ok";
  const cls=raidBad||volFull||(bk&&bkBad)?"bad":(raidSync||volWarn||bkStale||diskCls!="ok"||(so.smart||[]).some(x=>x.health!="PASSED"))?"warn":"ok";
  const volHtml=vols.map(v=>v.missing?`<div class="small" style="margin-top:6px"><b>${esc(v.label)}</b> <span style="color:var(--bad)">${esc(v.mount)} not mounted</span></div>`
    :`<div class="small" style="margin-top:6px"><b>${esc(v.label)}</b> <span class="muted">${esc(v.mount)}</span><span style="float:right">${v.used_gb} / ${v.total_gb} GB · ${v.free_gb} GB free</span></div>
      <div class="bar" style="margin:2px 0 0"><i style="width:${v.pct}%;background:${v.pct>=95?"var(--bad)":v.pct>=85?"var(--warn)":"var(--ok)"}"></i></div>`).join("");
  const disk=`<div class="card"><h2><span class="dot ${cls}"></span>Storage &amp; backup</h2>
    ${vols.length?volHtml:`<div class="big">${d.disk.free_gb} GB free</div><div class="small muted">of ${d.disk.total_gb} GB on the pipeline volume</div>`}
    ${arrs.length?arrs.map(a=>`<div class="small" style="margin-top:4px">RAID ${esc(a.level)} ${esc(a.dev)} (${esc(a.members.join(" + "))}): <b>${!a.healthy?"DEGRADED "+esc(a.up):"healthy"}</b>${a.sync?` · ${esc(a.sync.what)} ${a.sync.pct}% (${Math.round(a.sync.min)} min left)`:""}</div>`).join(""):`<div class="small muted">no RAID array</div>`}
    ${(so.smart||[]).map(x=>`<div class="small">${esc(x.dev)}: SMART ${x.health=="PASSED"?"ok":"<b>"+esc(x.health)+"</b>"}${x.temp?" · "+x.temp+"°C":""}${x.error?' <span class="muted">'+esc(x.error)+'</span>':""}</div>`).join("")}
    <div class="small" style="margin-top:4px">Backup to NAS: ${bk?`<b>${esc(bk.result)}</b> ${esc(ago(bkAge))}${bk.message?" · "+esc(bk.message):""}`:'<span class="muted">not set up</span>'}</div>
    ${so.deploy?`<div class="small muted">Deployed: ${esc(so.deploy.last)}${so.deploy.dirty?" (uncommitted changes)":""}</div>`:""}</div>`;
  const al=d.alerts||[];const ax=al.length?`<div class="card" style="grid-column:1/-1"><h2><span class="dot bad"></span>Alerts (${al.length})</h2>
    ${al.map(a=>`<div class="small" style="margin-top:6px"><b>${esc(a.unit)}</b> failed ${esc(a.at)} <button class="btn small" style="float:right" onclick="act({action:'clear_alert',flag:'${esc(a.flag)}'},'alert cleared')">Clear</button>
      <pre style="white-space:pre-wrap;margin:4px 0 0;font-size:12px;color:var(--bad)">${esc(a.text)}</pre></div>`).join("")}</div>`:"";
  $("#health").innerHTML=ax+svcCard("Worker",svc("mps-ocr-worker"),wx,d.worker_paused&&svc("mps-ocr-worker")?.active=="active")
    +svcCard("NAS collector",svc("mps-ocr-collector"),cx)+ox+gx+sx+disk;
  const via=t=>{const v=t.via_today||{},c=v.cpu||0,g=(v.gpu_after_cpu||0)+(v.builtin||0)+(v.command||0);
    return t.cpu_first?`<div class="small muted">CPU-first · today ${c} CPU, ${g} GPU</div>`:""};
  $("#types").innerHTML=d.types.map(t=>`<tr><td><b>${esc(t.label)}</b>${via(t)}</td>
    <td>${sw("collect_off_"+t.type,t.collect_on,t.label+" collecting",!d.collector_configured)}</td>
    <td>${sw("process_off_"+t.type,t.process_on,t.label+" processing")}</td>
    <td>${sw("sync_off_"+t.type,t.sync_on,t.label+" syncing")}</td>
    <td class="num">${t.in_queue}</td><td class="num">${t.awaiting_sync}</td><td class="num hide-sm">${t.done_today}</td><td class="num hide-sm">${t.synced_today}</td>
    <td class="num" style="color:${t.failed.length?"var(--bad)":""}">${t.failed.length}</td><td class="num" style="color:${t.sync_failed.length?"var(--bad)":""}">${t.sync_failed.length}</td></tr>`).join("");
  $("#failed").innerHTML=failedHtml(d.types);
  $("#recent").innerHTML=recentHtml(d.recent);
  const sel=$("#svc");if(!sel.options.length)d.services.forEach(s=>sel.add(new Option(s.name,s.name)));
  renderTabs(d);
}
function failedHtml(types){
  let f="";
  for(const t of types){
    if(t.failed.length)f+=`<div class="row" style="justify-content:space-between;align-items:center"><b>${esc(t.label)} — processing</b>
      <button onclick="act({action:'retry_failed_all',type:'${t.type}'},'Re-queued')">Retry all</button></div>`+t.failed.map(x=>`<div class="item"><div class="grow">
      <div class="name">${esc(x.name)}</div><div class="err">${esc(x.error)}</div><div class="small muted">${esc(x.at)}</div></div>
      <button onclick="act({action:'retry_failed',type:'${t.type}',name:${esc(JSON.stringify(x.name))}},'Re-queued')">Retry</button></div>`).join("");
    if(t.sync_failed.length)f+=`<div class="row" style="justify-content:space-between;align-items:center"><b>${esc(t.label)} — sync</b>
      <button onclick="act({action:'retry_sync_all',type:'${t.type}'},'Back in the sync queue')">Retry all</button></div>`+t.sync_failed.map(x=>`<div class="item"><div class="grow">
      <div class="name">${esc(x.name)}</div><div class="err">${esc(x.error)}</div><div class="small muted">${esc(x.at)}</div></div>
      <button onclick="act({action:'retry_sync',type:'${t.type}',name:${esc(JSON.stringify(x.name))}},'Back in the sync queue')">Retry</button></div>`).join("");}
  return f||`<div class="muted small">Nothing failed.</div>`;
}
function recentHtml(list){
  return list.map(r=>`<div class="item"><span class="dot ${r.status=="ok"?"ok":r.status?"bad":""}" style="margin-top:5px"></span><div class="grow">
    <div class="name">${esc(r.source)}</div><div class="small muted">${esc(r.type)} · ${esc((r.at||"").replace("T"," ").slice(0,19))}${Array.isArray(r.outputs)&&r.outputs.length?" → "+esc(r.outputs.join(", ")):""}${r.outputs&&r.outputs.error?` <span class="err">${esc(r.outputs.error)}</span>`:""}</div></div></div>`).join("")||`<div class="muted small">No activity yet.</div>`;
}
// ---- tabs: Overview + one per document type (the invoice archive sits with Supplier Invoices)
const TAB_DEFS=[{id:"jt",label:"Job Transfers",types:["JobTransfers"]},{id:"po",label:"Purchase Orders",types:["PurchaseOrders"]},
  {id:"pl",label:"Polymer Orders",types:["PolymerOrders"]},{id:"pop",label:"Proof of Payments",types:["ProofOfPayments"]},
  {id:"si",label:"Supplier Invoices",types:["SupplierInvoices","SupplierInvoicesArchive"],vat:true},
  {id:"dn",label:"Delivery Notes",types:["DeliveryNotes"]},{id:"ws",label:"Workshop Sheets",types:["WorkshopSheets"]},{id:"tc",label:"Test Certificates",types:["TestCerts"]}];
let tab=(location.hash||"#overview").slice(1),tabsKey="";
function tabList(d){
  const have=new Set(d.types.map(t=>t.type)),used=new Set(),out=[{id:"overview",label:"Overview",types:[]}];
  for(const t of TAB_DEFS){const ts=t.types.filter(x=>have.has(x));ts.forEach(x=>used.add(x));if(ts.length)out.push({...t,types:ts})}
  for(const t of d.types)if(!used.has(t.type))out.push({id:"t-"+t.type,label:t.label,types:[t.type]});
  out.push({id:"train",label:"Training",types:[]});
  return out}
function renderTabs(d){
  const tabs=tabList(d);if(!tabs.find(t=>t.id==tab))tab="overview";
  const bad=t=>d.types.filter(x=>t.types.includes(x.type)).reduce((n,x)=>n+x.failed.length+x.sync_failed.length,0);
  const key=tabs.map(t=>t.id+":"+bad(t)).join("|")+"/"+tab;
  if(key!=tabsKey){tabsKey=key;
    $("#tabs").innerHTML=tabs.map(t=>`<button class="${t.id==tab?"on":""}" onclick="go('${t.id}')">${esc(t.label)}${bad(t)?`<span class="n">${bad(t)}</span>`:""}</button>`).join("")}
  const cur=tabs.find(t=>t.id==tab);
  $("#tab-overview").hidden=tab!="overview";$("#tab-job").hidden=tab=="overview"||tab=="train";$("#tab-train").hidden=tab!="train";
  if(tab=="train"){if(!trTimer)loadTrain()}
  else if(tab!="overview")renderJob(d,cur);
}
function go(id){tab=id;history.replaceState(null,"","#"+id);tabsKey="";if(last)renderTabs(last);window.scrollTo(0,0)}
window.addEventListener("hashchange",()=>{const h=location.hash.slice(1)||"overview";if(h!=tab)go(h)});
function stat(n,label,bad){return `<div class="stat"><b style="${bad&&n?"color:var(--bad)":""}">${n}</b><span>${label}</span></div>`}
function renderJob(d,t){
  const ts=d.types.filter(x=>t.types.includes(x.type));
  $("#jobcards").innerHTML=ts.map(x=>{const v=x.via_today||{},cpu=v.cpu||0,gpu=(v.gpu_after_cpu||0)+(v.builtin||0)+(v.command||0);
    return `<div class="card"><h2>${esc(x.label)}</h2>
    <div class="label">${sw("collect_off_"+x.type,x.collect_on,x.label+" collecting",!d.collector_configured)}<span class="small">Collect from NAS ${d.collector_configured?(x.collect_on?"on":"off"):"(no NAS folders configured)"}</span></div>
    <div class="label">${sw("process_off_"+x.type,x.process_on,x.label+" processing")}<span class="small">Process (OCR) ${x.process_on?"on":"off"}</span></div>
    <div class="label">${sw("sync_off_"+x.type,x.sync_on,x.label+" syncing")}<span class="small">Sync to SharePoint / SQL ${x.sync_on?"on":"off"}</span></div>
    <div class="stats">${stat(x.in_queue,"In queue")}${stat(x.awaiting_sync,"To sync")}${stat(x.done_today,"Done today")}${stat(x.synced_today,"Synced today")}
    ${stat(x.failed.length,"Failed",1)}${stat(x.sync_failed.length,"Sync failed",1)}</div>
    ${x.cpu_first?`<div class="small muted" style="margin-top:8px">CPU-first: today ${cpu} read on the CPU, ${gpu} needed the GPU</div>`:""}</div>`}).join("");
  $("#jobfailed").innerHTML=failedHtml(ts);
  const rec=ts.flatMap(x=>x.recent||[]).sort((a,b)=>(b.at||"").localeCompare(a.at||"")).slice(0,25);
  $("#jobrecent").innerHTML=recentHtml(rec);
  $("#vatcard").hidden=!t.vat;
  if(t.vat&&!vatLoaded)loadVat();
  const key=t.types.join(",");
  if(key!=outKey){outKey=key;outData={};$("#outq").innerHTML="";loadOut()}
  else renderOut();
}
// ---- out queue + Sync now (job tabs). The list is fetched on tab open, after actions and every 10 s.
let outKey="",outData={},outOpen=new Set(),outTimer=null;
const outSort={},outScrollReset=new Set();      // "<type>|r" / "<type>|h" -> {k: column key, d: "asc"|"desc"}: header clicks
async function loadOut(){
  const types=outKey?outKey.split(","):[];
  try{for(const t of types){const sr=outSort[t+"|r"],sh=outSort[t+"|h"];
    const qs=(sr?`&sort=${encodeURIComponent(sr.k)}&dir=${sr.d}`:"")+(sh?`&hsort=${encodeURIComponent(sh.k)}&hdir=${sh.d}`:"");
    const r=await fetch("/api/out?type="+encodeURIComponent(t)+qs);if(r.status==401)return location.reload();outData[t]=await r.json();if(outData[t].writers)WRITERS=outData[t].writers;if(outData[t].units)UNITS=outData[t].units}}
  catch(e){}
  renderOut();clearTimeout(outTimer);outTimer=setTimeout(()=>{if(tab!="overview")loadOut()},10000)}
const COLS={supplier_invoice:[["Supplier","supplier_name"],["Invoice","invoice_number",0,1],["Date","invoice_date"],["Total","total",1]],
  po:[["Order no","order_number",0,1],["Supplier","supplier"],["Method","order_method"],["Att of","att_of"],["Date","date"],
    ["Allocation","allocation"],["Job","job_number"],["Other PO ref","other_po_ref"],["Quote ref","quote_ref"],
    ["Writer","writer"],["Checked","checked",0,0,"tick"],["Total","total",1]],
  pl:[["PL order","order_number",0,1],["PO number","polymer_po"],["Supplier","supplier"],["Attention","att_of"],["Order method","order_method"],
    ["Date","date"],["Writer","writer"],["Checked","checked",0,0,"tick"],["Total","total",1]],
  pop:[["Date","date"],["Amount","amount",1],["Statement ref","statement_ref"],["Account name","account_name",0,1],["Creditor code","creditor_code"],
    ["Paid from","from_account_name"],["Original folder","_folder",0,0,"wrap"],["Checked","checked",0,0,"tick"]],
  jt:[["JT","jt_number",0,1],["Job","job_number"],["Date","date"],["From","from"],["Writer","written_by"]],
  dn:[["Delivery note","dn_number",0,1],["Date","date"],["Customer","customer",0,0,"wrap"],["Account","customer_account"],
    ["Customer order","customer_order"],["Job","job_number"],["Pages","pages"],["Extra pages","extra_pages",0,0,"wrap"],["Held because","_hold",0,0,"wrap"]],
  ws:[["MPS DWG","mps_dwg_no",0,1],["Job","job_number"],["Sheet","sheet"],["Date","date"],["Project","project",0,0,"wrap"],["Corrosion int","corrosion_int"],
    ["Corrosion ext","corrosion_ext"],["Spec","spec"],["Batch","batch_no"],["Customer DWG","customer_dwg_no"],["QA stamp","stamp_text",0,0,"wrap"],["Checked","checked",0,0,"tick"]],
  test_cert:[["Cert no","cert_no",0,1],["Family","family"],["Date","date"],["Mill","mill",0,0,"wrap"],["MPS PO","mps_po"],["Ronin PO","ronin_po"],
    ["Type","cert_type"],["Heats","heats_text",0,0,"wrap"],["Checked","checked",0,0,"tick"]]};
const ALIAS={order_number:"po_number",supplier:"supplier_name"};      // older sidecars (generic reader)
const DT={SupplierInvoices:"supplier_invoice",SupplierInvoicesArchive:"supplier_invoice",PurchaseOrders:"po",PolymerOrders:"pl",ProofOfPayments:"pop",JobTransfers:"jt",DeliveryNotes:"dn",WorkshopSheets:"ws",TestCerts:"test_cert"};
const money=v=>v==null||v===""?"":(+v).toLocaleString("en-ZA",{minimumFractionDigits:2,maximumFractionDigits:2});
function checks(x){
  const f=x.fields||{},c=[];
  if(x.needs_review)c.push(`<span class="tag warn">needs review</span>`);
  if(f.cancelled)c.push(`<span class="tag bad" title="marked cancelled (on the form or in the file name)">CANCELLED</span>`);
  if(f.amended)c.push(`<span class="tag warn" title="marked amended (on the form or in the file name)">AMENDED</span>`);
  (x.flags||[]).filter(g=>g!="cancelled"&&!(x.doc_type=="test_cert"&&String(g).startsWith("check:"))).forEach(g=>c.push(`<span class="tag">${esc(String(g).replace(/_/g," "))}</span>`));
  if(x.doc_type=="test_cert"){const n=(x.flags||[]).filter(g=>String(g).startsWith("check:")).length;if(n)c.push(`<span class="tag warn" title="listed under Review checks when the row is opened">${n} review check${n>1?"s":""}</span>`)}
  if(x.vat_in_sage===false)c.push(`<span class="tag bad">VAT not in Sage</span>`);
  if(x.vat_in_sage===true)c.push(`<span class="tag ok">Sage supplier</span>`);
  if(x.doc_type=="supplier_invoice"&&!f.supplier_vat_no)c.push(`<span class="tag">no VAT no</span>`);
  if(f.subtotal!=null&&x.items_total!=null&&Math.abs(+f.subtotal-x.items_total)>1)c.push(`<span class="tag warn" title="lines add up to ${money(x.items_total)}">lines ≠ subtotal</span>`);
  if(!x.item_count&&x.doc_type!="pop")c.push(`<span class="tag warn">no lines</span>`);
  if(x.doc_type=="pop")c.push(f.cross_checked?`<span class="tag ok" title="the image reading and Tesseract agree">cross-checked</span>`:"");
  if(x.doc_type=="po"&&String(f.library_doc_type||"").includes("Proof of Payment"))c.push(`<span class="tag" title="page(s) ${esc((f.payment_pages||[]).join(", "))}: stays in this file; Document Type gets Proof of Payment too">+ proof of payment</span>`);
  if(x.attempts)c.push(`<span class="tag bad">${x.attempts} failed sync attempt(s)</span>`);
  if(!x.pdf)c.push(`<span class="tag bad">PDF missing</span>`);
  const sg=x.sage;
  if(sg){
    if(sg.status=="match")c.push(`<span class="tag ok" title="Sage ${esc(sg.account)} ref ${esc(sg.ref)}: ${esc(sg.date)}, R ${money(sg.amount)}">in Sage: date &amp; amount agree</span>`);
    else if(sg.status=="date")c.push(`<span class="tag bad" title="Sage ${esc(sg.account)} ref ${esc(sg.ref)}">Sage date ${esc(sg.date)}</span>`);
    else if(sg.status=="amount")c.push(`<span class="tag bad" title="Sage ${esc(sg.account)} ref ${esc(sg.ref)}">Sage amount R ${money(sg.amount)}</span>`);
    else if(sg.status=="date_amount")c.push(`<span class="tag bad" title="Sage ${esc(sg.account)} ref ${esc(sg.ref)}">Sage: ${esc(sg.date)}, R ${money(sg.amount)}</span>`);
    else if(sg.status=="not_found")c.push(`<span class="tag" title="Sage ${esc((sg.accounts||[]).join(", "))}, as at ${esc(sg.data_at||"")}">not in Sage yet</span>`);
  }
  const sp=x.sage_po;
  if(sp){
    if(sp.status=="found")c.push(`<span class="tag ${sp.supplier_ok?"ok":"warn"}" title="${esc(sp.best.source)} ${esc(sp.best.number)} · ${esc(sp.best.vendor||"")} · ${esc(sp.best.date||"")}">${esc(sp.po)} in Sage${sp.supplier_ok?"":": other supplier"}</span>`);
    else if(sp.status=="not_found")c.push(`<span class="tag bad" title="Polymer Sage copy as at ${esc(sp.data_at||"?")}">${esc(sp.po)} not in Sage</span>`);
    else if(sp.status=="no_po")c.push(`<span class="tag warn">no PO number</span>`);
  }
  if(x.sp&&x.sp.blank.length)c.push(`<span class="tag warn" title="Left empty in SharePoint: ${esc(x.sp.blank.join(", "))}">${x.sp.blank.length} SharePoint column${x.sp.blank.length>1?"s":""} blank</span>`);
  return c.join("")}
// header fields per document type: [label, key, hide when empty, read-only]
const HEAD={
  po:{title:"Order header",rows:[["Order number","order_number"],["Polymer PO number","polymer_po",1],["Supplier","supplier"],["Supplier as read","supplier_read",1,1],
    ["Supplier in file name","supplier_file",1,1],["Order method","order_method"],["Att of","att_of"],["Date","date"],["Date as written","date_as_written",1,1],
    ["Allocation","allocation"],["Job number","job_number"],["Job as written","job_number_read",1,1],["Job customer","job_customer",1,1],
    ["Other PO ref","other_po_ref"],["Quote ref","quote_ref"],["Payment terms","payment_terms"],
    ["Signed by","signed_by"],["Writer","writer",1],["Company","company",1,1],["Form","form_label",1,1],["Deliver to","delivery_address",1],
    ["Transferred from","transfer_from",1,1],["Transferred to","transfer_to",1,1],["Legibility","legibility",1,1],["Cancelled","cancelled",0,0,"bool"],["Amended","amended",0,0,"bool"],["Notes","notes",0,0,"wide"]]},
  pop:{title:"Payment",rows:[["Payment date","date"],["Amount","amount"],["Statement reference","statement_ref"],["Account name (paid to)","account_name"],
    ["Creditor code","creditor_code"],["Beneficiary account","beneficiary_account"],["Paid from","from_account_name"],["Bank reference","bank_reference"],["Bank","bank",0,1]]},
  supplier_invoice:{title:"Invoice header",rows:[["Supplier","supplier_name"],["VAT number","supplier_vat_no"],["Invoice number","invoice_number"],["Invoice date","invoice_date"],
    ["Order ref","order_ref"],["Job number","job_number"],["Subtotal","subtotal"],["VAT","vat"],["Total","total"]]},
  jt:{title:"Transfer header",rows:[["JT number","jt_number"],["Date","date"],["Job number","job_number"],["From","from"],["To","to"],["Written by","written_by"]]},
  delivery_note:{title:"Delivery note",rows:[["Delivery note (invoice)","dn_number"],["Number read from","number_read_from",1,1],["Date","date"],["Date printed","printed_date",1,1],
    ["Customer","customer"],["Account","customer_account"],["Customer order","customer_order"],["Job number","job_number"],["Deliver to","deliver_to",0,0,"wide"],
    ["Value excl VAT","total_excl",1,1],["Pages","pages",1,1],["Scan pages","source_pages",1,1],["Extra pages","extra_pages",1,1]]},
  ws:{title:"Workshop sheet",rows:[["MPS DWG No","mps_dwg_no"],["Job number","job_number"],["Job as written","job_number_read",1,1],["Job customer","job_customer",1,1],
    ["Date","date"],["Date as written","date_as_written",1,1],["Sheet no","sheet_no"],["Of","sheet_of"],["Project","project"],
    ["Corrosion int","corrosion_int"],["Corrosion ext","corrosion_ext"],["Destination","destination",1],["Spec","spec"],
    ["Customer DWG No","customer_dwg_no"],["Rev","rev",1],["Batch No","batch_no"],["MTO","mto",1],["Drawn by","drawn_by",1],
    ["QA stamped","stamped",0,0,"yn"],["Issued for construction signed","ifc_signed",0,0,"yn"],["Stamp date","stamp_date"],
    ["Stamp date as written","stamp_date_as_written",1,1],["Status","status",1],["End detail","end_detail",1],["Layout","layout",1,1],["Legibility","legibility",1,1],["Notes","notes",0,0,"wide"]]},
  test_cert:{title:"Test certificate",rows:[["Cert no","cert_no"],["Cert type","cert_type"],["Date","date"],["Date as written","date_as_written",1,1],
    ["Mill","mill"],["Supplier (trader)","supplier"],["Customer","customer"],["Purchaser","purchaser"],["MPS PO","mps_po"],["Ronin PO","ronin_po"],
    ["Contract / invoice","contract"],["Family","family"],["Standard","standard"],["Grade","grade"],["Material no","material_no",1],
    ["Grade summary","grade_summary",1,1],["Heat nos","heats_text",1,1],["Heats vs old site","heat_check_text",1,1],["Pages","pages",1,1],["Extra pages","extra_pages",1,1],
    ["Legibility","legibility",1,1],["Library item","sp_item_id",1,1]]},
  other:{title:"Not a delivery note",rows:[["Looks like","doc_label",1,1],["Pages","pages",1,1],["Scan pages","source_pages",1,1],["Page types","page_kinds",1,1],
    ["Release number","release_number",1,1]]}};
const HOLD={not_in_sage:"Not in Sage yet: released automatically after the next Sage read",no_sage_copy:"No Sage copy on the share yet (sage_invoices.csv): released automatically when it arrives",
  not_a_delivery_note:"Not a delivery note (no barcode on the first page)"};
const MONEYF=new Set(["amount","subtotal","vat","total","unit_price","order_value","total_excl"]),DATEF=new Set(["date","invoice_date","stamp_date"]);
const LINECOLS={po:[["Qty","quantity",1],["Description","description"],["Unit price","unit_price",1],["Unit","price_unit",0,0,"unit"],["Amount","amount",1,1]],
  supplier_invoice:[["Code","item_code"],["Description","description"],["Qty","quantity",1],["Unit price","unit_price",1],["Amount","amount",1]],
  jt:[["#","item_no"],["Qty","quantity"],["Unit","unit"],["Description","description"]],
  delivery_note:[["Qty","quantity",1],["Description","description"]],
  ws:[["Item","item_no"],["Mark","mark"],["Qty","qty",1],["Size","size"],["DWG No (customer)","dwg_no"],["Dimensions (A=227 R=533)","dims_text"]],
  test_cert:[["Family","family"],["Product","product"],["Description","description"],["Size as printed","size_text"],["NB","size_nb",1],
    ["Sch / WT","schedule"],["Class","class"],["Grade","grade"],["Ends","ends"],["Length m","length_m",1],["Qty","qty",1],["Unit","unit"],["Heat no","heat_no"]]};
let editing=null,WRITERS=[],UNITS=[["each","ea"],["kg","PKG"],["litre","/L"],["length","PLEN"],["metre","PM"],["lot","lot"]];
const unitLabel=v=>{if(v==null||v===""||v=="null")return "";const u=UNITS.find(u=>u[0].toLowerCase()==String(v).toLowerCase());return u?u[1]:String(v)};
function unitSelect(v){
  const vv=v==null||v=="null"?"":String(v),list=[...UNITS];if(vv&&!list.some(u=>u[0].toLowerCase()==vv.toLowerCase()))list.push([vv,vv]);
  return `<select data-c="price_unit"><option value=""></option>${list.map(([val,lab])=>`<option value="${esc(val)}" title="${esc(val=="each"?"each":"per "+val)}" ${vv.toLowerCase()==val.toLowerCase()?"selected":""}>${esc(lab)}</option>`).join("")}<option value="__add__">+ Add…</option></select>`}
const WRITERF=new Set(["writer","written_by"]);
function writerSelect(k,v){
  const list=[...WRITERS];if(v&&!list.some(w=>w.toLowerCase()==String(v).toLowerCase()))list.unshift(v);
  return `<select data-f="${k}"><option value="">(not known)</option>${list.map(w=>`<option ${String(v||"").toLowerCase()==w.toLowerCase()?"selected":""}>${esc(w)}</option>`).join("")}<option value="__add__">+ Add writer…</option></select>`}      // id of the document being edited (stops the 10 s refresh from wiping the inputs)
function headHtml(x,ed){
  const h=HEAD[x.doc_type];if(!h)return "";
  const f=x.fields||{},g=k=>f[k]??f[ALIAS[k]];
  const val=(k,kind)=>{const v=g(k);if(kind=="bool")return v?`<span style="color:var(--bad);font-weight:600">YES - ${k=="amended"?"amended":"cancelled"} order</span>`:"No";
    if(kind=="yn")return v==null?`<span class="muted">not read</span>`:v?"Yes":"No";
    if(v==null||v==="")return `<span class="muted">not read</span>`;return MONEYF.has(k)&&typeof v=="number"?"R "+money(v):esc(v)};
  const inp=(k,wide)=>wide=="bool"||wide=="yn"?`<select data-f="${k}"><option value="no">No</option><option value="yes" ${g(k)?"selected":""}>Yes</option></select>`
    :wide||k=="notes"||k=="delivery_address"?`<textarea data-f="${k}">${esc(g(k)??"")}</textarea>`
    :WRITERF.has(k)?writerSelect(k,g(k))
    :`<input data-f="${k}" ${DATEF.has(k)?'type="date"':""} ${k=="signed_by"?'list="writers"':""} value="${esc(g(k)??"")}">`;
  const cells=h.rows.filter(([l,k,hide,ro])=>ed?!(ro&&(g(k)==null||g(k)==="")):!(hide&&(g(k)==null||g(k)==="")))
    .map(([l,k,hide,ro,wide])=>`<div class="k">${l}</div><div class="v${wide=="wide"?" wide":""}">${ed&&!ro?inp(k,wide):val(k,wide)}</div>`);
  if(x.doc_type=="pop"){
    cells.push(`<div class="k">Original folder</div><div class="v">${x.source_folder?esc(x.source_folder):`<span class="muted">not known</span>`}</div>`);
    cells.push(`<div class="k">Cross-check</div><div class="v">${f.cross_checked?"image and Tesseract agree":`<span style="color:var(--bad)">${esc((x.flags||[]).filter(q=>String(q).startsWith("check:")).map(q=>String(q).slice(7)).join("; ")||"not cross-checked")}</span>`}</div>`)}
  else if(x.source_folder)cells.push(`<div class="k">Original folder</div><div class="v">${esc(x.source_folder)}</div>`);
  if(x.hold_reason)cells.unshift(`<div class="k">On hold</div><div class="v">${esc(HOLD[x.hold_reason]||x.hold_reason)}</div>`);
  if(x.doc_type=="delivery_note")cells.push(`<div class="k">Sage</div><div class="v">${f.sage_found?"found"+(f.sage_matches>1?` (${f.sage_matches} Sage documents carry this number)`:""):`<span style="color:var(--bad)">not found: values read from the page</span>`}</div>`);
  if(!["supplier_invoice","delivery_note","other"].includes(x.doc_type))cells.push(`<div class="k">Checked</div><div class="v">${f.checked?"Yes"+(x.checked_at?` <span class="muted">(${esc(String(x.checked_at).replace("T"," ").slice(0,16))})</span>`:""):`<span class="muted">not yet: tick the box in the row</span>`}</div>`);
  const ed_n=(x.edits||0)?` <span class="tag edited" title="edited on the dashboard">edited ${x.edits}×</span>`:"";
  return `<div class="sec">${h.title}${ed_n}</div><div class="sp"><datalist id="writers">${WRITERS.map(w=>`<option>${esc(w)}`).join("")}</datalist><div class="kv">${cells.join("")}</div>
    ${x.reader?`<div class="small muted" style="margin-top:6px">${esc(x.reader)} · ${esc(x.model||"")}</div>`:""}</div>`}
function linesHtml(x,ed){
  if(x.doc_type=="pop")return "";
  const cols=LINECOLS[x.doc_type]||LINECOLS.supplier_invoice,it=x.items||[];
  if(!ed&&!it.length)return `<div class="sec">Line items</div><div class="small muted">No line items were read.</div>`;
  const cell=(i,[l,k,num,ro,kind])=>kind=="unit"?(ed?`<td>${unitSelect(i[k])}</td>`:`<td>${esc(unitLabel(i[k]))}</td>`)
    :ed&&k=="description"?`<td><textarea data-c="description" rows="${String(i[k]??"").length>60?2:1}">${esc(i[k]??"")}</textarea></td>`
    :ed&&!ro?`<td class="${num?"num":""}"><input data-c="${k}" value="${esc(i[k]??"")}" ${num?'inputmode="decimal" style="text-align:right"':""}></td>`
    :`<td class="${num?"num":""}">${num&&MONEYF.has(k)?money(i[k]):esc(i[k]??"")}${k=="quantity"&&i.unit&&x.doc_type!="jt"?" "+esc(i.unit):""}</td>`;
  return `<div class="sec">Line items</div><div class="scroll"><table class="lines lt-${x.doc_type}"><thead><tr><th>#</th>${cols.map(([l,k,num])=>`<th class="${num?"num":""}">${l}</th>`).join("")}${ed?"<th></th>":""}</tr></thead><tbody>
    ${it.map((i,n)=>`<tr data-line="${n}"><td>${n+1}</td>${cols.map(c=>cell(i,c)).join("")}${ed?`<td><button data-act="delline" title="remove this line">✕</button></td>`:""}</tr>`).join("")}
    ${!ed&&x.items_total!=null?`<tr><td colspan="${cols.length}" class="num"><b>Lines total</b></td><td class="num"><b>${money(x.items_total)}</b></td></tr>`:""}</tbody></table></div>
    ${ed?`<button data-act="addline" style="margin-top:6px">+ Add line</button>`:""}`}
function reviewHtml(x){
  // the reader's review checks (fields.review, else its "check:" flags), first in an opened document
  const f=x.fields||{};
  let it=f.review?String(f.review).split("; "):(x.flags||[]).filter(g=>String(g).startsWith("check:")).map(g=>String(g).slice(6).trim());
  it=it.map(s=>s.trim()).filter(Boolean);
  if(!it.length)return "";
  return `<div class="rv"><div class="sec">Review checks (${it.length})</div><div class="sp small" style="white-space:pre-wrap">${esc(it.join("\n"))}</div></div>`}
function heatsHtml(x){
  if(x.doc_type!="test_cert")return "";
  const f=x.fields||{},hs=f.heats||[],EL=["C","Si","Mn","P","S","Cr","Ni","Mo","Cu","V"];
  let h=`<div class="sec">Heats: chemistry and mechanicals <span class="muted small">(read-only; one row per heat, first test shown)</span></div>`;
  if(!hs.length)h+=`<div class="small muted">No heats were read.</div>`;
  else h+=`<div class="scroll"><table class="lines"><thead><tr><th>Heat no</th><th>Grade</th>${EL.map(e=>`<th class="num">${e}</th>`).join("")}<th class="num">Ceq</th><th class="num">Yield MPa</th><th class="num">Tensile MPa</th><th class="num">El %</th><th class="num">Tests</th></tr></thead><tbody>
    ${hs.map(r=>{const c=r.chem||{},m=(r.mech||[])[0]||{};return `<tr><td>${esc(r.heat_no??"")}</td><td>${esc(r.grade??"")}</td>${EL.map(e=>`<td class="num">${esc(c[e]??"")}</td>`).join("")}<td class="num">${esc(c.Ceq??"")}</td>
      <td class="num">${esc(m.yield_mpa??"")}</td><td class="num">${esc(m.tensile_mpa??"")}</td><td class="num">${esc(m.elongation_pct??"")}</td><td class="num">${(r.mech||[]).length}</td></tr>`}).join("")}</tbody></table></div>`;
  if(f.old_columns)h+=`<div class="sec">Old library columns <span class="muted small">(captured by hand: a check, not the record)</span></div><div class="sp small" style="white-space:pre-wrap">${esc(f.old_columns)}</div>`;
  return h}
function sagePoHtml(x){
  const s=x.sage_po;if(!s)return "";let t;
  if(s.status=="found"){const b=s.best;
    t=`<b>${esc(s.po)}</b> found in Polymer's Sage: ${esc(b.source)} <b>${esc(b.number||"")}</b>${b.order&&!b.own?" (order "+esc(b.order)+")":""} · <span style="${s.supplier_ok?"":"color:var(--bad)"}">${esc(b.vendor||"?")}</span> · ${esc(b.date||"")}${b.amount!=null?" · R "+money(b.amount)+" excl":""}${s.count>1?` · <span class="muted">${s.count-1} more document(s) quote it</span>`:""}${s.pl_in_sage?` · <span class="muted">PL number on it too</span>`:""}`}
  else if(s.status=="not_found")t=`<span style="color:var(--bad)">${esc(s.po)} is not in Polymer's Sage</span> <span class="muted">(Sage copy as at ${esc(s.data_at||"?")}). Check the number on the form, or the order hasn't been captured.</span>`;
  else if(s.status=="no_po")t=`<span class="muted">No Polymer PO number read from the form.</span>`;
  else t=`<span class="muted">No Sage copy yet: the PC sync writes sage_po.csv after MPSBusiness D140 is installed.</span>`;
  return `<div class="sec">Sage (Polymer)</div><div class="sp small">${t}</div>`}
function sageHtml(x){
  const s=x.sage;if(!s)return "";
  let t;
  if(["match","date","amount","date_amount"].includes(s.status))
    t=`Sage <b>${esc(s.account)}</b> · ref ${esc(s.ref)} · <span style="${s.date_ok?"":"color:var(--bad)"}">${esc(s.date)}</span> · <span style="${s.amount_ok?"":"color:var(--bad)"}">R ${money(s.amount)}</span>${s.order?" · order "+esc(s.order):""}${s.multiple?" · <span class='muted'>(same number more than once in Sage)</span>":""}`;
  else if(s.status=="not_found")t=`<span class="muted">Not in Sage yet under ${esc((s.accounts||[]).join(", "))} (Sage copy as at ${esc(s.data_at||"?")}): normal for an invoice that hasn't been captured.</span>`;
  else if(s.status=="no_account")t=`<span class="muted">Can't check against Sage: ${esc(s.why)}.</span>`;
  else t=`<span class="muted">Can't check against Sage: no invoice number.</span>`;
  return `<div class="sec">Sage</div><div class="sp small">${t}</div>`}
function spHtml(x,q){
  const p=x.sp;
  if(!p)return `<div class="sp small muted">SharePoint preview not available yet: it appears once the PC has run the updated sync script (it publishes its SharePoint settings to the share).</div>`;
  const where=p.folder?`${esc(p.library)} / ${esc(p.folder)} /`:`${esc(p.library)} <span class="muted">(library root)</span> /`;
  let dest;
  if(p.mode=="upload")dest=`<span class="muted">Uploads to</span> ${where} <b>${esc(p.name)}</b>
    <div class="small muted">Site ${esc((p.site||"").replace(/^https:\/\/[^/]+/,""))} · ${p.folder?`folder from ${esc(p.date_from.replace("fields.",""))} · `:""}${p.link_orders?"linked to its order at sync (date ±2 days + amount + supplier, or same original folder) · ":""}if that name is already taken SharePoint adds a number${p.dedupe?` · if a file with the same ${esc(p.dedupe)} is already in the library it is linked, not uploaded again`:""}</div>`;
  else dest=`<span class="muted">Links to the existing file</span> <b>${esc(p.link_name)}</b> <span class="muted">in ${esc(p.library)}</span>
    <div class="small muted">${p.mode=="link-or-upload"?`If it isn't there (or under its invoice number), uploads to ${where} ${esc(p.fallback_name)}`:"If it isn't found, it's recorded in SQL without a SharePoint link"}. Columns are only filled where the existing file's column is still empty.</div>`;
  const cols=p.columns.length?`<div class="kv" style="margin-top:6px">
    ${p.columns.map(c=>`<div class="k" title="from ${esc(c.source)}">${esc(c.column)}</div><div class="v">${c.value==null?`<span class="muted">blank</span>`:typeof c.value=="number"&&!/count|lines$/i.test(c.column)?money(c.value):typeof c.value=="boolean"?(c.value?"Yes":"No"):esc(c.value)}</div>`).join("")}</div>`
    :`<div class="small muted">No SharePoint columns are filled for this type (file only).</div>`;
  const ln=p.lines?`<div class="small muted" style="margin-top:6px">Lines: ${x.item_count} item${x.item_count==1?"":"s"} → list <b>${esc(p.lines.list||"")}</b>, linked to this file (replaced if the sheet syncs again)</div>`:"";
  const sql=p.sql?`<div class="small muted" style="margin-top:6px">SQL: MPSDocs.dbo.${esc(p.sql.table)} + ${x.item_count} line${x.item_count==1?"":"s"} in ${esc(p.sql.lines_table)}</div>`
    :p.skip_sql?`<div class="small muted" style="margin-top:6px">Not recorded in SQL (no MPSDocs table for this type yet).</div>`:"";
  return `<div class="sec">SharePoint</div><div class="sp"><div class="path">${dest}</div>${cols}${ln}${sql}</div>`}
function docRows(t,list,held){
  const cols=COLS[DT[t]]||COLS.supplier_invoice;
  return list.map(x=>{const f=x.fields||{},id=t+"|"+(held?"h|":"r|")+x.name,open=outOpen.has(id);
    const pdf=x.pdf?`<a class="btn" target="_blank" rel="noopener" href="/api/pdf?type=${encodeURIComponent(t)}&name=${encodeURIComponent(x.file)}${held?"&held=1":""}" onclick="event.stopPropagation()">PDF</a>`:"";
    const btn=`<button data-act="${held?"release":"hold"}" data-type="${esc(t)}" data-name="${esc(x.name)}">${held?"Release":"Hold"}</button>`
      +(held?` <button class="danger" data-act="delete" data-type="${esc(t)}" data-name="${esc(x.name)}" title="Take it out of the pipeline (kept in pipeline/deleted for recovery)">Delete</button>`:"");
    const fv=k=>k=="_folder"?x.source_folder:k=="_hold"?(HOLD[x.hold_reason]||x.hold_reason):f[k]??f[ALIAS[k]];
    const cell=(k,n,isId,kind)=>kind=="tick"?`<input type="checkbox" class="tick" title="Tick once you have checked this order against the PDF (fills Checked in SharePoint)" data-type="${esc(t)}" data-name="${esc(x.name)}" data-held="${held?1:0}" ${f.checked?"checked":""}>`
      :n?esc(money(fv(k))):(fv(k)!=null&&fv(k)!=="")?esc(fv(k)):isId?`<span class="muted" title="no number read; file name shown">${esc((x.file||x.name).replace(/\.(pdf|json)$/i,""))}</span>`:"";
    let r=`<tr class="doc" data-open="${esc(id)}">${cols.map(([l,k,n,isId,kind])=>`<td class="${n?"num":""}${kind=="wrap"?" wrap":""}${kind=="tick"?" tickc":""}">${cell(k,n,isId,kind)}</td>`).join("")}
      <td class="num">${x.item_count}</td><td class="chk">${checks(x)}</td><td class="act">${pdf} ${btn}</td></tr>`;
    if(open){
      const q0=null,ed=editing==id;
      r+=`<tr class="det"><td colspan="${cols.length+3}">
        <div class="detgrid${ed?" editing":""}" data-edit="${esc(id)}" data-type="${esc(t)}" data-name="${esc(x.name)}" data-held="${held?1:0}">
        ${reviewHtml(x)}
        <div>
          <div class="editbar"><span class="small muted grow">${esc(x.file||x.name)} · from ${esc(x.source_file||"?")} · read by ${esc(x.extractor||"?")}${x.processed_at?" · "+esc(x.processed_at.replace("T"," ").slice(0,16)):""}</span>
          ${x.uploaded?`<span class="small muted">uploaded: edit in SharePoint</span>`:ed?`<button class="primary" data-act="save">Save</button><button data-act="cancel">Cancel</button>`:`<button data-act="edit">Edit</button>`}</div>
          ${headHtml(x,ed)}
        </div>
        <div>${sagePoHtml(x)}${sageHtml(x)}${spHtml(x,q0)}</div>
        <div class="dl">${linesHtml(x,ed)}${heatsHtml(x)}</div>
        </div></td></tr>`}
    return r}).join("")}
function syncStatus(d,ty){
  const sy=d.sync||{},every=d.sync_every_minutes||15,stale=sy.age==null||sy.age>every*120+120,L=ty.sync_last;
  const next=sy.age!=null?Math.max(0,Math.ceil((every*60-sy.age)/60)):null;
  if(!d.sync_all_on||!ty.sync_on)return `<div class="qstat warn">Sync is switched off for ${!d.sync_all_on?"everything":"this type"}.</div>`;
  if(ty.sync_running)return `<div class="qstat">Syncing on ${esc(sy.host||"the PC")}…</div>`;
  if(ty.sync_requested)return `<div class="qstat ${stale?"bad":""}">Requested: waiting for ${esc(sy.host||"the PC")} to pick it up (it checks every ${every} minutes${!stale&&next!=null?`; next check in about ${next||"<1"} min`:""}).
    ${stale?` It hasn't checked in for ${ago(sy.age)}: it must be on, with you signed in and the on-request task installed.`:""}</div>`;
  if(L)return `<div class="qstat ${L.result=="ok"?"":"warn"}">Last sync ${esc((L.at||"").replace("T"," ").slice(0,16))}: ${esc(L.result)} · ${L.synced} synced${L.failed?` · ${L.failed} failed`:""}${L.gave_up?` · ${L.gave_up} moved to sync failed`:""}${L.message?` <span class="err">${esc(L.message)}</span>`:""}</div>`;
  return `<div class="qstat muted">Not synced from the dashboard yet.</div>`}
function renderOut(force){
  const d=last;if(!d||tab=="overview"||tab=="train")return;
  if(editing&&!force)return;           // keep the inputs; the list refreshes after Save / Cancel
  const types=outKey.split(",");
  const keep={};$("#outq").querySelectorAll(".qscroll[data-qs]").forEach(el=>{if(!outScrollReset.has(el.dataset.qs))keep[el.dataset.qs]=[el.scrollTop,el.scrollLeft]});
  outScrollReset.clear();const pageY=window.scrollY;
  $("#outq").innerHTML=types.map(t=>{
    const ty=d.types.find(x=>x.type==t),q=outData[t];if(!ty)return "";
    const cols=COLS[DT[t]]||COLS.supplier_invoice,n=q?q.ready_total:ty.awaiting_sync;
    const can=d.sync_all_on&&ty.sync_on&&!ty.sync_running&&!ty.sync_requested&&n>0;
    const head=lk=>{const so=outSort[t+"|"+lk],th=(l,k,num)=>{const on=so&&so.k==k;
        return `<th class="${num?"num":""} sortable${on?" sorted":""}" data-sort="${esc(k)}" data-list="${lk}" data-type="${esc(t)}" title="Sort by ${esc(l)}${on?" (click again to reverse)":""}">${l}${on?(so.d=="desc"?" ▼":" ▲"):""}</th>`};
      return `<tr>${cols.map(([l,k,num])=>th(l,k,num)).join("")}${th("Lines","_lines",1)}<th>Checks</th><th></th></tr>`};
    return `<section class="card"><div class="qhead"><h2>Out queue: ${esc(ty.label)} (${n} ready${q&&q.held_total?`, ${q.held_total} on hold`:""})</h2>
      ${q&&q.ready.length?`<button data-act="expand" data-type="${esc(t)}">${q.ready.every(x=>outOpen.has(t+"|r|"+x.name))?"Collapse all":"Expand all"}</button>`:""}
      ${ty.sync_requested&&!ty.sync_running?`<button data-act="cancel_sync" data-type="${esc(t)}">Cancel request</button>`:""}
      <button class="primary" data-act="sync_now" data-type="${esc(t)}" ${can?"":"disabled"}>Sync ${n} to SharePoint &amp; SQL</button></div>
      ${syncStatus(d,ty)}
      ${!q?`<div class="small muted">Loading…</div>`:q.ready.length?`<div class="scroll qscroll" data-qs="${esc(t)}|r"><table class="q"><thead>${head("r")}</thead><tbody>${docRows(t,q.ready,false)}</tbody></table></div>
        ${q.ready_total>q.ready.length?`<div class="small muted">Showing the ${outSort[t+"|r"]?"first":"oldest"} ${q.ready.length} of ${q.ready_total}${outSort[t+"|r"]?" (sorted over all of them)":""}.</div>`:""}`
        :`<div class="small muted">Nothing waiting to sync.</div>`}
      ${q&&q.held.length?`<details ${outOpen.has("held|"+t)?"open":""} data-held="${esc(t)}"><summary>On hold (${q.held_total}): skipped by Sync now until released${q.held_total>q.held.length?` · showing the ${outSort[t+"|h"]?"first":"oldest"} ${q.held.length}`:""}</summary>
        <div class="scroll qscroll" data-qs="${esc(t)}|h"><table class="q"><thead>${head("h")}</thead><tbody>${docRows(t,q.held,true)}</tbody></table></div></details>`:""}
      <div class="small muted" style="margin-top:8px">Click a row to see what goes to SharePoint and SQL, with its line items. Hold keeps a document back from the next sync.</div></section>`}).join("");
  $("#outq").querySelectorAll(".qscroll[data-qs]").forEach(el=>{const k=keep[el.dataset.qs];if(k){el.scrollTop=k[0];el.scrollLeft=k[1]}});
  if(window.scrollY!=pageY)window.scrollTo(window.scrollX,pageY);
}
$("#outq").addEventListener("click",e=>{
  const h=e.target.closest("th[data-sort]");if(!h)return;
  e.stopPropagation();if(editing){toast("Save or cancel the edit first");return}
  const key=h.dataset.type+"|"+h.dataset.list,cur=outSort[key];
  outScrollReset.add(key);outSort[key]=cur&&cur.k==h.dataset.sort?{k:cur.k,d:cur.d=="asc"?"desc":"asc"}:{k:h.dataset.sort,d:"asc"};
  loadOut()},true);
$("#outq").addEventListener("click",async e=>{
  const tk=e.target.closest("input.tick");
  if(tk){e.stopPropagation();tk.disabled=true;
    await act({action:"check",type:tk.dataset.type,name:tk.dataset.name,held:tk.dataset.held=="1",value:tk.checked},tk.checked?"Ticked as checked":"Tick removed");
    loadOut();return}
  const b=e.target.closest("button[data-act]");
  if(b&&["edit","save","cancel","addline","delline"].includes(b.dataset.act)){
    e.stopPropagation();const box=b.closest("[data-edit]"),a=b.dataset.act;
    if(a=="edit"){editing=box.dataset.edit;renderOut(true);return}
    if(a=="cancel"){editing=null;renderOut(true);return}
    const tb=box.querySelector("table.lines tbody");
    if(a=="delline"){b.closest("tr").remove();return}
    if(a=="addline"){
      let t2=tb;if(!t2){toast("Save first, then add lines");return}
      const proto=t2.querySelector("tr[data-line]");const n=t2.querySelectorAll("tr[data-line]").length;
      const tr=document.createElement("tr");tr.dataset.line=n;
      const cols=(LINECOLS[(outData[box.dataset.type]?.ready.concat(outData[box.dataset.type]?.held||[]).find(x=>x.name==box.dataset.name)||{}).doc_type]||LINECOLS.po);
      tr.innerHTML=`<td>${n+1}</td>`+cols.map(([l,k,num,ro,kind])=>kind=="unit"?`<td>${unitSelect("each")}</td>`:ro?`<td></td>`:k=="description"?`<td><textarea data-c="description" rows="1"></textarea></td>`:`<td class="${num?"num":""}"><input data-c="${k}" ${num?'inputmode="decimal" style="text-align:right"':""}></td>`).join("")+`<td><button data-act="delline">✕</button></td>`;
      t2.appendChild(tr);tr.querySelector("input")?.focus();return}
    if(a=="save"){
      const fields={};box.querySelectorAll("[data-f]").forEach(el=>fields[el.dataset.f]=el.value);
      let items=null;if(tb){items=[...tb.querySelectorAll("tr[data-line]")].map(tr=>{const o={};tr.querySelectorAll("[data-c]").forEach(el=>o[el.dataset.c]=el.value);return o})
        .filter(o=>Object.entries(o).some(([k,v])=>k!="price_unit"&&String(v).trim()))}
      b.disabled=true;
      await act({action:"edit",type:box.dataset.type,name:box.dataset.name,held:box.dataset.held=="1",fields,items},"Saved");
      editing=null;await loadOut();return}
  }
  if(b&&b.dataset.act=="delete"){e.stopPropagation();
    if(!confirm("Delete "+b.dataset.name+" from the pipeline? It won't be synced. (Kept in pipeline/deleted, so it can be recovered.)"))return;
    b.disabled=true;await act({action:"delete",type:b.dataset.type,name:b.dataset.name},"Deleted");loadOut();return}
  if(b){e.stopPropagation();const a=b.dataset.act,t=b.dataset.type;
    if(a=="expand"){const q=outData[t];if(!q)return;const ids=q.ready.map(x=>t+"|r|"+x.name),all=ids.every(i=>outOpen.has(i));
      ids.forEach(i=>all?outOpen.delete(i):outOpen.add(i));renderOut();return}
    if(a=="sync_now"&&!confirm("Upload everything in the "+t+" out queue to SharePoint and SQL?"))return;
    b.disabled=true;await act({action:a,type:t,name:b.dataset.name},{sync_now:"Sync requested",cancel_sync:"Request cancelled",hold:"On hold",release:"Released"}[a]);loadOut();return}
  const r=e.target.closest("tr.doc");if(r){const id=r.dataset.open;outOpen.has(id)?outOpen.delete(id):outOpen.add(id);renderOut()}});
$("#outq").addEventListener("change",async e=>{
  const us=e.target.closest("select[data-c=price_unit]");
  if(us&&us.value=="__add__"){
    const name=(prompt("New unit (as written on the forms, e.g. PBAG):")||"").trim();if(!name){us.value="";return}
    try{const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF":CSRF},body:JSON.stringify({action:"add_unit",name})});
      const j=await r.json();if(!j.ok)throw new Error(j.error||"failed");UNITS=j.units;
      if(![...us.options].some(o=>o.value==j.value)){const o=document.createElement("option");o.value=j.value;o.textContent=j.value;us.insertBefore(o,us.querySelector('option[value="__add__"]'))}
      us.value=j.value;toast("Added unit "+j.value)}catch(err){toast("Error: "+err.message);us.value=""}
    return}
  const sel=e.target.closest("select[data-f]");if(!sel||sel.value!="__add__")return;
  const name=(prompt("New writer's name:")||"").trim();
  if(!name){sel.value="";return}
  try{const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF":CSRF},body:JSON.stringify({action:"add_writer",name})});
    const j=await r.json();if(!j.ok)throw new Error(j.error||"failed");WRITERS=j.writers;
    const o=document.createElement("option");o.textContent=j.name;sel.insertBefore(o,sel.querySelector('option[value="__add__"]'));sel.value=j.name;toast("Added "+j.name)}
  catch(err){toast("Error: "+err.message);sel.value=""}});
$("#outq").addEventListener("toggle",e=>{const dt=e.target.closest("details[data-held]");if(dt)(dt.open?outOpen.add("held|"+dt.dataset.held):outOpen.delete("held|"+dt.dataset.held))},true);
// ---- Training tab: verified examples, reader accuracy, quick / full training jobs
let trTimer=null,trData=null,trReader="po";
const FLABEL={heat_old_site:"Heat numbers vs old site",cert_no:"Cert no",mill:"Mill",mps_po:"MPS PO",ronin_po:"Ronin PO",family:"Family",grade:"Grade",
  line_size_nb:"Line sizes (NB)",line_heat:"Line heat numbers",jt_number:"JT number",mps_job_no:"Job number",transferred_from:"Transferred from",transferred_to:"Transferred to",received_by:"Received by",remarks:"Remarks",order_number:"Order number",polymer_po:"Polymer PO",supplier:"Supplier",date:"Date",order_method:"Order method",att_of:"Att of",
  allocation:"Allocation",job_number:"Job number",other_po_ref:"Other PO ref",quote_ref:"Quote ref",payment_terms:"Payment terms",signed_by:"Signed by",cancelled:"Cancelled",
  lines_count:"Number of lines",line_description:"Line descriptions",line_qty:"Line quantities",line_price:"Line prices",
  sheet_no:"Sheet no",sheet_of:"Sheet of",project:"Project",corrosion_int:"Corrosion int",corrosion_ext:"Corrosion ext",destination:"Destination",
  spec:"Spec",customer_dwg_no:"Customer DWG No",rev:"Rev",mps_dwg_no:"MPS DWG No",batch_no:"Batch No",
  line_item_no:"Line item numbers",line_size:"Line sizes",line_dwg_no:"Line DWG numbers",line_dims:"Line dimensions",
  stamped:"QA stamped",ifc_signed:"IFC signed",stamp_date:"Stamp date"};
async function loadTrain(){
  clearTimeout(trTimer);
  try{const r=await fetch("/api/training?reader="+trReader);if(r.status==401)return location.reload();trData=await r.json()}catch(e){}
  renderTrain();trTimer=setTimeout(()=>{trTimer=null;if(tab=="train")loadTrain()},trData?.job?.state=="running"?4000:15000)}
function renderSched(sc){
  const el=$("#trsched");if(!el)return;if(!sc){el.innerHTML=`<h2>Scheduled &amp; running</h2><div class="small muted">Not available.</div>`;return}
  const fmt=s=>{if(!s)return"–";const d=new Date(s),n=new Date();const day=d.toDateString()==n.toDateString()?"today":d.toDateString()==new Date(+n+864e5).toDateString()?"tomorrow":d.toLocaleDateString(undefined,{weekday:"short",day:"numeric",month:"short"});return day+" "+d.toTimeString().slice(0,5)};
  const dur=s=>{if(!s)return"";const m=Math.round((Date.now()-new Date(s))/6e4);return m<60?m+" min":Math.floor(m/60)+" h "+(m%60)+" min"};
  const j=(trData&&trData.job)||{},run=j.state=="running";
  const now=run?`<div><b>${esc(j.kind=="finetune"?"Full training (fine-tune)":j.kind=="quick"?"Quick training":j.kind)}</b> · running for ${dur(j.started)}
      <div class="small">${esc(j.step||"")}${j.total?` · ${j.done} / ${j.total}`:""}</div>${j.total?`<div class="bar"><i style="width:${Math.round(100*j.done/j.total)}%"></i></div>`:""}</div>`
    :sc.timers.filter(t=>t.state=="activating").map(t=>`<div><b>${esc(t.what)}</b> · starting (since ${fmt(t.started)})<div class="small muted">${esc(t.note||"waiting for the GPU to be free")}</div></div>`).join("")
    ||`<div class="small muted">No training running.${j.kind?` Last: ${esc(j.kind)} ${esc(j.state||"")} ${fmt(j.finished||j.started)}${j.summary?` – ${esc(j.summary)}`:""}`:""}</div>`;
  const tm=sc.timers.length?sc.timers.map(t=>`<div style="margin-bottom:6px"><b style="white-space:nowrap">${t.next?fmt(t.next):"not scheduled again"}</b> · ${esc(t.what)}${t.reader?` <span class="muted">(${esc(t.reader)})</span>`:""}
      <div class="small muted">${t.last?`last ran ${fmt(t.last)}: ${t.result=="success"?"finished":esc(t.result||"")}`:"not run yet"}</div></div>`).join("")
    :`<div class="small muted">Nothing scheduled. (Scheduled training is a systemd timer on mps-frigate; it shows here as soon as one exists.)</div>`;
  const gpu=[];
  if(sc.paused)gpu.push(`<div class="small"><span class="tag">worker paused</span> OCR reading is paused for training</div>`);
  (sc.runner||[]).forEach(r=>gpu.push(`<div class="small"><span class="tag ${r.state=="running"?"warn":""}">${r.state}</span> <span class="mono">${esc(r.name)}</span>${r.state=="running"?` · ${dur(r.since)}`:""}${r.about?` <span class="muted">– ${esc(r.about)}</span>`:""}</div>`));
  if(sc.ollama)sc.ollama.forEach(m=>gpu.push(`<div class="small"><span class="tag">loaded</span> ${esc(m.name)} · ${m.vram_gb} GB on the GPU</div>`));
  else gpu.push(`<div class="small err">Ollama not answering</div>`);
  el.innerHTML=`<h2>Scheduled &amp; running</h2>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px">
      <div><div class="sec">Now</div>${now}</div>
      <div><div class="sec">Scheduled</div>${tm}</div>
      <div><div class="sec">Other GPU work</div>${gpu.join("")||`<div class="small muted">None</div>`}
        <div class="small muted" style="margin-top:4px">Runner scripts and benchmarks share the GPU with training; scheduled training waits for them to finish.</div></div>
    </div><div class="small muted" style="margin-top:6px">Checked ${new Date(sc.at).toLocaleTimeString()}</div>`}
function renderTrain(){
  const x=trData;if(!x)return;renderSched(x.schedule);
  if(x.error){$("#trjob").innerHTML=`<div class="err">${esc(x.error)}</div>`;return}
  const ws=trReader=="ws",jt0=trReader=="jt",tc=trReader=="tc",jt=jt0||tc,N=ws?"sheets":tc?"certificates":jt?"forms":"orders",N1=ws?"sheet":tc?"certificate":jt?"form":"order";
  const s=x.status||{},po=(ws?s.ws:tc?s.tc:jt?s.jt:s.po)||{},th=s.thresholds||{},act=s.active||{},j=x.job||{};
  const pct=v=>v==null?"–":Math.round(v*100)+"%";
  const sw=`<div class="row" style="align-items:center;margin-bottom:10px"><span class="small muted">Reader:</span>
    <button class="${ws||jt?"":"primary"}" data-rd="po">Purchase orders</button><button class="${ws?"primary":""}" data-rd="ws" ${x.ws_available===false?"disabled":""}>Workshop sheets</button><button class="${jt0?"primary":""}" data-rd="jt" ${x.jt_available===false?"disabled":""}>Job transfers</button><button class="${tc?"primary":""}" data-rd="tc" ${x.tc_available===false?"disabled":""}>Test certificates</button></div>`;
  $("#trcards").innerHTML=
    `<div class="card"><h2>Verified ${N}</h2><div class="big">${po.verified||0}</div><div class="small muted">${tc?`labelled: ${po.checked||0} ticked Checked + ${po.old_site||0} with heat numbers from the old site · ${po.read||0} certificates read`:jt?`${po.from_review||0} corrected in Label Studio + ${po.from_dashboard||0} ticked Checked on the dashboard`:"ticked Checked (synced, in the queue or on hold)"}${ws&&po.verified_cad?` · plus ${po.verified_cad} CAD sheets read from their text (not trained)`:""}</div></div>`+
    `<div class="card"><h2>Corrected</h2><div class="big">${po.corrected||0}</div><div class="small muted">edited before checking: what the reader got wrong</div></div>`+
    `<div class="card"><h2>Train / test</h2><div class="big">${po.train||0} / ${po.test||0}</div><div class="small muted">${tc?`1 in 5 certificates is a test cert, never shown to the reader; a quick job scores up to 25 of them (${po.test_all||0} labelled test certs) · ${po.examples_in_use||0} worked examples in use, ${po.untried||0} good one-page certs not tried yet`:jt?`the 25 Label Studio test forms (+1 in 5 dashboard forms) score every change, never shown to the reader · ${po.examples_in_use||0} worked examples in use, ${po.untried||0} forms not tried yet`:"1 in 5 is kept back to score every change, never trained on"}</div></div>`+
    `<div class="card"><h2>Reader in use</h2><div class="small"><b>${esc(act.model||"?")}</b> · ${esc(String(act.fewshot_dir||"").split("/").pop()||"")} · ${act.px||""} px</div>
      <div class="small muted" style="margin-top:4px">${x.installed?`installed by ${esc(x.installed.job||"a training job")} · ${esc((x.installed.installed_at||"").replace("T"," ").slice(0,16))}`:"original setup (config.json)"}</div></div>`+
    (ws||jt?[]:Object.entries(s.by_type||{}).filter(([k])=>k!="po")).map(([k,v])=>`<div class="card"><h2>${esc({pop:"Proofs of payment",supplier_invoice:"Supplier invoices",jt:"Job transfers"}[k]||k)}</h2><div class="big">${v.verified}</div><div class="small muted">verified · ${v.corrected} corrected (kept for later)</div></div>`).join("");
  const need=Math.max(0,(th.quick_min_corrected||20)-(po.corrected_train||0)),needF=Math.max(0,(th.finetune_min_verified||300)-(po.verified||0));
  const running=j.state=="running";
  const prog=running&&j.total?`<div class="bar"><i style="width:${Math.round(100*j.done/j.total)}%"></i></div><div class="small muted">${j.done} / ${j.total}</div>`:"";
  $("#trjob").innerHTML=sw+`<h2>Training jobs</h2>
    <div class="row" style="align-items:center">
      <button class="primary" data-tr="quick" ${running||!s.quick_ready?"disabled":""}>Run quick training</button>
      ${!s.quick_ready&&!running?`<button data-tr="quick" data-force="1" title="Run it with fewer corrected ${N} (for a trial)">Run anyway</button>`:""}
      ${jt?"":`<button class="primary" data-tr="finetune" ${running||!s.finetune_ready?"disabled":""}>Run full training</button>`}
      ${!ws&&s.finetune_resume&&!running?`<button class="primary" data-tr="resume" title="${esc(s.finetune_resume)}: fine-tuning finished but the result was never imported and tested">Finish the last full training</button>`:""}
      ${running?`<button class="danger" data-tr="cancel">Cancel job</button>`:""}
      <button data-tr="revert" ${running||!(x.history||x.installed)?"disabled":""}>Go back to the previous setup</button>
    </div>
    <div class="small muted" style="margin-top:8px">
      ${tc?`<b>Quick</b> (about an hour): tries a one-page certificate per family (pipe, flanges, fittings) as a worked example - one you checked, or one whose heats all matched the old site - then scores old vs new on the test certificates (heat numbers against the old site, and the fields of checked certs) and switches only if the new one is better. ${s.quick_ready?"Ready.":`Needs ${th.quick_min_corrected?"":""}8 labelled test certificates and an untried example.`}<br><b>Full</b> training is parked for test certificates.`:""}<span style="${tc?"display:none":""}">
      <b>Quick</b> (${jt?"about 30 minutes":"minutes"}): adds ${jt?"forms from writers the reader has no example of yet, then the forms it got most wrong,":`your most-corrected ${N}`} as worked examples${ws||jt?"":" and uses your confirmed supplier names"}, then scores old vs new on the test ${N} and switches only if the new one is better.
      ${s.quick_ready?"Ready.":jt?"Nothing new to try.":`Needs ${need} more corrected ${need==1?N1:N} (of ${th.quick_min_corrected}).`}<br>${jt?`<b>Full</b> training is parked for job transfers: the 24 Oct test showed fine-tuned models losing accuracy through Ollama 0.40.`:`
      <b>Full</b> (hours, GPU): fine-tunes ${ws?"a workshop-sheet":"the reader"} model on every verified ${N1}, same test-then-switch rule; the worker pauses meanwhile.
      ${s.finetune_ready?"Ready.":!s.finetune_env?`Needs the training environment (setup_training.sh on mps-frigate)${needF?` and ${needF} more verified ${N}`:""}.`:`Needs ${needF} more verified ${N} (of ${th.finetune_min_verified}).`}`}
      ${ws?`<br><span class="muted">Only scanned sheets train the model: CAD-printed sheets are read from their own text.</span>`:""}</span>
    </div>
    ${j.kind?`<div class="sec" style="margin-top:12px">${running?"Running":"Last job"}: ${esc(j.kind)} · ${esc((j.started||"").replace("T"," ").slice(0,16))}</div>
      <div class="small ${j.state=="failed"||j.state=="stopped"?"err":""}">${esc(running?j.step:(j.state+": "+(j.summary||"")))}</div>${prog}
      ${x.log_tail?`<details ${running?"open":""}><summary>Log</summary><pre class="small">${esc(x.log_tail)}</pre></details>`:""}`:""}`;
  const A=(ws||jt?s.accuracy:s.po_accuracy)||{},acc=A.fields||{};
  const rows=Object.keys(FLABEL).filter(k=>acc[k]).map(k=>{const a=acc[k],v=a.acc??0;
    return `<tr><td>${FLABEL[k]}</td><td style="width:45%"><div class="bar" style="margin:0"><i style="width:${Math.round(v*100)}%;background:${v>=.95?"var(--ok)":v>=.8?"var(--warn)":"var(--bad)"}"></i></div></td><td class="num">${pct(a.acc)}</td><td class="num muted">${a.right}/${a.counted}</td></tr>`}).join("");
  $("#tracc").innerHTML=`<h2>How accurate the reader is (${N} you checked)</h2>
    ${rows?`<table class="lines"><tbody>${rows}</tbody></table><div class="small muted" style="margin-top:6px">Overall ${pct(A.overall)} over ${A.docs||0} ${N}${A.whole_form!=null?` · whole form right ${pct(A.whole_form)}`:""}: ${jt&&s.accuracy_note?esc(s.accuracy_note):"what the reader read vs what you checked."} Fields blank on both are left out.${ws&&s.accuracy_all&&s.accuracy_all.docs>A.docs?` With CAD sheets: ${pct(s.accuracy_all.overall)} over ${s.accuracy_all.docs}.`:""}</div>`
      :`<div class="small muted">Nothing checked yet. Tick Checked on ${N} (after correcting them with Edit) and they count here.</div>`}`;
  const reps=x.reports||[];
  $("#trrep").innerHTML=`<h2>Job results</h2>${reps.length?`<table class="lines"><thead><tr><th>Job</th><th>When</th><th class="num">Before</th><th class="num">After</th><th>Result</th></tr></thead><tbody>
    ${reps.map(r=>`<tr><td>${esc(r.kind)}</td><td>${esc((r.at||"").replace("T"," ").slice(0,16))}</td><td class="num">${pct(r.current)}</td><td class="num">${pct(r.candidate)}</td>
      <td><span class="tag ${r.installed?"ok":r.valid===false?"warn":""}">${r.installed?"switched":r.valid===false?"test failed":"kept"}</span> <span class="small muted">${esc(r.why||"")}</span></td></tr>`).join("")}</tbody></table>`
    :`<div class="small muted">No training jobs run yet.</div>`}`}
$("#tab-train").addEventListener("click",async e=>{
  const rd=e.target.closest("button[data-rd]");if(rd){trReader=rd.dataset.rd;trData=null;clearTimeout(trTimer);trTimer=null;loadTrain();return}
  const b=e.target.closest("button[data-tr]");if(!b)return;const a=b.dataset.tr;
  if(a=="cancel"){if(!confirm("Cancel the running training job?"))return;await act({action:"train_cancel"},"Cancelling");}
  else if(a=="revert"){if(!confirm("Go back to the previous reader setup?"))return;await act({action:"train_revert",reader:trReader},"Reverted");}
  else{if(a=="resume"&&!confirm("Import the fine-tuned model from the last full training into Ollama and test it against the current reader (about 15-20 minutes; order reading pauses meanwhile)?"))return;
    if(a=="finetune"&&!confirm("Start the full training job? It takes hours and pauses GPU reading while it uses the GPU."))return;
    await act({action:"train_start",job:a,force:!!b.dataset.force,reader:trReader},"Training job started")}
  setTimeout(loadTrain,800)});
// ---- missing VAT numbers (Supplier Invoices tab): loaded when the tab opens and on Refresh, not every 5 s
let vatLoaded=false;
async function loadVat(){
  vatLoaded=true;const b=$("#vatrefresh");b.disabled=true;b.textContent="Checking…";
  try{const r=await fetch("/api/missing_vat");if(r.status==401)return location.reload();const v=await r.json();if(v.error)throw new Error(v.error);
    const when=v.sage_list_at?new Date(v.sage_list_at).toLocaleString():"never";
    let h=`<div class="small" style="color:var(--ink)">Sage list: <b>${v.sage_count}</b> usable VAT numbers (updated ${esc(when)}) · invoices checked: <b>${v.checked}</b></div>`;
    if(v.note)h+=`<div class="err">${esc(v.note)}</div>`;
    h+=v.missing.length?`<div class="small muted" style="margin:6px 0 4px"><b style="color:var(--bad)">Not in Sage (${v.missing.length})</b>: add the VAT number to the supplier in Sage, or fix it if Sage has a different one.</div>
      <div class="scroll"><table class="vat"><thead><tr><th>VAT no</th><th class="num">Invoices</th><th>Latest</th><th>Supplier as read on the invoice</th></tr></thead><tbody>
      ${v.missing.map(m=>`<tr><td class="mono">${esc(m.vat)}</td><td class="num">${m.invoices}</td><td class="mono">${esc(m.latest||"-")}</td><td>${esc(m.name)}</td></tr>`).join("")}</tbody></table></div>`
      :`<div class="small" style="margin-top:6px;color:var(--ok)">Every VAT number read on an invoice is on the Sage list.</div>`;
    if(v.no_vat.length)h+=`<details><summary>No VAT number read: ${v.no_vat_total} invoice(s) where the supplier can't be identified by VAT (logo-only, foreign, or not read)</summary>
      <div class="scroll"><table><thead><tr><th class="num">Invoices</th><th>Supplier as read</th></tr></thead><tbody>
      ${v.no_vat.map(n=>`<tr><td class="num">${n.invoices}</td><td>${esc(n.name)}</td></tr>`).join("")}</tbody></table></div></details>`;
    h+=`<div class="small muted" style="margin-top:8px">Checked ${new Date(v.at).toLocaleTimeString()}. The Sage list on the box refreshes after each nightly Sage read once the PC sync runs.</div>`;
    $("#vat").innerHTML=h;$("#vat").classList.remove("muted","small");}
  catch(e){$("#vat").innerHTML=`<div class="err">Check failed: ${esc(e.message)}</div>`}
  finally{b.disabled=false;b.textContent="Refresh"}}
$("#vatrefresh").onclick=loadVat;
async function refresh(){if(busy)return;try{const r=await fetch("/api/status");if(r.status==401)return location.reload();render(await r.json())}catch(e){$("#upd").textContent="connection lost — retrying"}}
$("#loadlogs").onclick=async()=>{const r=await fetch("/api/logs?svc="+encodeURIComponent($("#svc").value));$("#logs").textContent=await r.text();$("#logs").classList.remove("muted");const p=$("#logs");p.scrollTop=p.scrollHeight};
$("#svc").onchange=()=>$("#loadlogs").click();
$("#logout").onclick=async()=>{await fetch("/logout",{method:"POST",headers:{"X-CSRF":CSRF}});location.reload()};
refresh();setInterval(refresh,5000);
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-password", action="store_true")
    args = ap.parse_args()
    if args.set_password:
        return set_password()
    cfg = load_cfg()
    d = cfg.get("dashboard", {})
    if not d.get("password_hash"):
        sys.exit("No dashboard password set. Run:  python3 dashboard.py --set-password")
    addr = (d.get("bind", "0.0.0.0"), int(d.get("port", 8090)))
    print(f"MPS OCR dashboard on http://{addr[0]}:{addr[1]}", flush=True)
    ThreadingHTTPServer(addr, Handler).serve_forever()


if __name__ == "__main__":
    main()

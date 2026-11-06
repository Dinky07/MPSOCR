#!/usr/bin/env python3
"""
MPS document pipeline worker  (runs on mps-frigate as the systemd service mps-ocr-worker)

Drop PDFs (or JPG/PNG) into   <root>/in/<Type>/
Results appear in             <root>/out/<Type>/   as  <name>.pdf + <name>.json  (JSON written last)
Originals are kept in         <root>/done/<Type>/<yyyy-mm>/
Problem files go to           <root>/failed/<Type>/  with a .error.txt beside them

Behaviour
- One job at a time, so the GPU is never shared between two OCR runs.
- A file is only picked up once its size has stopped changing (safe for slow SMB copies).
- If Ollama is down, the file goes back to in/ and the worker waits; nothing is marked failed.
- If <root>/PAUSE exists, the worker finishes the current page and then idles (use this for LoRA training).
- Duplicate files (same SHA-256 as something already processed) go to done/<Type>/duplicates/.
- <root>/status.json shows queue counts, the current job and the last error.

Standard library only, plus poppler-utils (pdfinfo, pdfseparate, pdftoppm, pdftotext).
"""
import base64, hashlib, json, logging, os, re, shlex, shutil, signal, sqlite3, subprocess, sys, tempfile, time
import urllib.error, urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import cpu_invoice
import sidecar_lock                       # one lock per sidecar for every writer (review F10, 28 Oct)
import mps_jobs                           # job numbers, allocations, scan-date checks: one rule for every reader
try:
    import po_reader                      # the trained purchase-order reader (po_ocr_v2): prompt, worked examples, tidy
except ImportError:                       # pragma: no cover - the worker still runs, with the generic PO prompt
    po_reader = None
try:
    import pop_reader                     # proofs of payment (Standard Bank audit reports): OCR cross-check + layout
except ImportError:                       # pragma: no cover
    pop_reader = None
try:
    import dn_reader                      # delivery notes: barcode -> Sage invoice, CPU only (ocrmypdf + zbarimg)
except ImportError:                       # pragma: no cover
    dn_reader = None
try:
    import jt_reader                      # job transfers: the JT prompt from the review work + worked examples (jt_fewshot)
except ImportError:                       # pragma: no cover - the worker still runs, with the generic JT prompt
    jt_reader = None
try:
    import tc_reader                      # test certificates: page by page, chemistry + mechanicals per heat (qwen3-vl)
except ImportError:                       # pragma: no cover
    tc_reader = None
try:
    import tc_orders                      # test certificates: order numbers + L/C from the cert's own text (26 Oct)
except ImportError:
    tc_orders = None
try:
    import ws_reader                      # workshop sheets: CAD text layer on the CPU, scans with the vision model
except ImportError:                       # pragma: no cover
    ws_reader = None
try:
    import rn_reader                      # release notes (28 Oct): split at each release note, read it with qwen3-vl
except ImportError:                       # pragma: no cover
    rn_reader = None
try:
    import co_reader                      # customer orders (30 Oct): job sticker, VAT -> account, lines CPU then GPU
except ImportError:                       # pragma: no cover
    co_reader = None
try:
    import presort                        # pre-sort of the purchase-order queue, SHADOW (27 Oct): verdict recorded only
except Exception:                         # pragma: no cover
    presort = None
from concurrent.futures import ThreadPoolExecutor

CONFIG_PATH = Path(os.environ.get("MPS_OCR_CONFIG", "/srv/ocr/pipeline/config.json"))
ACCEPT = {".pdf", ".jpg", ".jpeg", ".png"}
IGNORE = re.compile(r"^(~\$|\.|Thumbs\.db$|desktop\.ini$)|\.(tmp|part|crdownload)$", re.I)

log = logging.getLogger("mps-ocr")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log.info("stop requested, finishing current page")


class OllamaDown(Exception):
    pass


class PageReadFailed(Exception):
    """One page could not be read, even after the retry without worked examples (332): it is held, the file's other
    pages are kept."""


def http_error_text(e):
    try:
        b = e.read().decode("utf-8", "replace")
        return str(json.loads(b).get("error") or b)[:200]
    except Exception:
        return str(getattr(e, "reason", e))[:200]


class GpuUnavailable(Exception):
    """Needs the GPU but it's paused or Ollama is down: the file waits in the queue, CPU work carries on."""


# --------------------------------------------------------------------------- field schemas
def _s(desc):   return {"type": ["string", "null"], "description": desc}
def _n(desc):   return {"type": ["number", "null"], "description": desc}

SCHEMAS = {
    "jt": {
        "type": "object",
        "properties": {
            "jt_number":   _s("Printed (usually red) J.T. form number, digits only"),
            "date":        _s("Date written on the form, exactly as written"),
            "job_number":  _s("MPS job number, e.g. 32814E (4-5 digits + letter); CONS for consumables"),
            "from":        _s("Transferred from (person, department or company)"),
            "to":          _s("Transferred to (person, department or company)"),
            "written_by":  _s("Name of the person who wrote/signed the form"),
            "received_by": _s("Name of the person who signed for receipt"),
            "remarks":     _s("Any remarks or notes not part of a line item"),
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "item_no":     _s("Item / line number as written"),
                "quantity":    _s("Quantity as written"),
                "unit":        _s("Unit if written (m, ea, kg ...)"),
                "description": _s("Item description exactly as written"),
            }, "required": ["description"]}},
        },
        "required": ["jt_number", "date", "job_number", "items"],
    },
    "po": {
        "type": "object",
        "properties": {
            "po_number":     _s("Purchase order number"),
            "date":          _s("Order date as written"),
            "supplier_name": _s("Supplier the order is placed with"),
            "job_number":    _s("MPS job number if written (4-5 digits + letter)"),
            "ordered_by":    _s("Person who placed / signed the order"),
            "delivery_to":   _s("Delivery address or instruction"),
            "total":         _n("Order total if written"),
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "quantity":    _s("Quantity as written"),
                "description": _s("Item description"),
                "unit_price":  _n("Unit price"),
                "amount":      _n("Line amount"),
            }, "required": ["description"]}},
        },
        "required": ["po_number", "date", "supplier_name", "items"],
    },
    "supplier_invoice": {
        "type": "object",
        "properties": {
            "supplier_name":   _s("Company that issued the invoice (the seller, not MPS)"),
            "supplier_vat_no": _s("Supplier VAT registration number"),
            "invoice_number":  _s("Invoice / tax invoice number"),
            "invoice_date":    _s("Invoice date as printed"),
            "due_date":        _s("Payment due date if printed"),
            "order_ref":       _s("Customer order / PO number referenced on the invoice"),
            "job_number":      _s("MPS job number if referenced (4-5 digits + letter)"),
            "currency":        _s("Currency code, ZAR if Rand"),
            "subtotal":        _n("Total excluding VAT"),
            "vat":             _n("VAT amount in Rand, not the VAT rate"),
            "total":           _n("Total including VAT"),
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "item_code":   _s("Supplier item code"),
                "description": _s("Line description"),
                "quantity":    _n("Quantity"),
                "unit_price":  _n("Unit price excl. VAT"),
                "amount":      _n("Line total excl. VAT"),
            }, "required": ["description"]}},
        },
        "required": ["supplier_name", "invoice_number", "invoice_date", "total", "items"],
    },
}

PROMPTS = {
    "jt": ("This is a handwritten Mining Pressure Systems (MPS) Job Transfer (J.T.) form. "
           "Read every field and every line item exactly as written. Do not invent values; use null when blank or unreadable. "
           "Job numbers are 4-5 digits followed by a letter (usually E). Pipe sizes like 50NB, 2\", 150# are part of descriptions."),
    "po": ("This is a Mining Pressure Systems (MPS) purchase order, often handwritten. "
           "Read every field and line exactly as written; use null when blank or unreadable. Do not invent values."),
    "supplier_invoice": ("This is a supplier tax invoice issued TO Mining Pressure Systems (MPS). "
                         "The supplier is the issuing company, never MPS. Amounts are numbers in Rand with a dot before the cents and no "
                         "currency symbol or thousands separator: 15 522,52 or 15,522.52 or R15522.52 is 15522.52, and a "
                         "handwritten 12893-80 is 12893.80; keep the cents. vat is the VAT amount in Rand, never the VAT rate (15 %). "
                         "The invoice number may be labelled Invoice No, Tax Invoice No, Document No or Doc No, sometimes as a column "
                         "heading with the value underneath. Use null for anything not printed. Include every line item."),
}

REQUIRED_FOR_CLEAN = {
    "jt": ["jt_number", "job_number", "date"],
    "po": ["po_number", "supplier_name", "date"],
    "supplier_invoice": ["supplier_name", "invoice_number", "invoice_date", "total"],
}
if pop_reader:
    SCHEMAS["pop"], PROMPTS["pop"] = pop_reader.SCHEMA, pop_reader.PROMPT
    REQUIRED_FOR_CLEAN["pop"] = ["date", "amount", "account_name"]
DATE_FIELDS = {"date", "invoice_date", "due_date"}
NUMBER_FIELDS = {"total", "subtotal", "vat", "unit_price", "amount"}


# --------------------------------------------------------------------------- helpers
def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run(cmd, timeout=300):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({r.returncode}): {r.stderr.strip()[:400]}")
    return r.stdout


def read_json_file(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def safe_name(s, maxlen=80):
    s = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(s)).strip(" ._")
    s = re.sub(r"[_ ]{2,}", "_", s)
    return s[:maxlen] or "document"


def unique_path(folder, stem, ext):
    n = 1
    while True:
        s = stem if n == 1 else f"{stem}_{n}"
        p = folder / f"{s}{ext}"
        if not p.exists() and not (folder / f"{s}.json").exists():
            return p
        n += 1


def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def move(src, dst_dir, name=None):
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / (name or src.name)
    if dst.exists():
        dst = dst_dir / f"{dst.stem}_{int(time.time())}{dst.suffix}"
    shutil.move(str(src), str(dst))
    return dst


# --------------------------------------------------------------------------- normalisation
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(s, day_first=True):
    """Returns ISO yyyy-mm-dd or None. South African forms are day-first; year-first (2026/10/12) is detected."""
    if not s:
        return None
    t = str(s).strip().lower().replace(",", " ")
    m = re.search(r"\b(\d{4})[./-](\d{1,2})[./-](\d{1,2})\b", t)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"\b(\d{1,2})[./ -](\d{1,2})[./ -](\d{2,4})\b", t)
        if m:
            a, b, y = map(int, m.groups())
            d, mo = (a, b) if day_first else (b, a)
            if mo > 12 and d <= 12:
                d, mo = mo, d
        else:
            m = re.search(r"\b(\d{1,2})\s*([a-z]{3})[a-z]*\.?\s*(\d{2,4})\b", t)
            if not m or m.group(2) not in _MONTHS:
                return None
            d, mo, y = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def parse_number(v):
    if v is None or isinstance(v, (int, float)):
        return v
    t = re.sub(r"[^\d,.\-]", "", str(v))
    if not t:
        return None
    m = re.fullmatch(r"(-?[\d,.]*\d)-(\d{2})", t)        # handwritten 12893-80: the dash is the decimal point (334)
    if m:
        t = f"{m.group(1)}.{m.group(2)}"
    if "," in t and "." in t:
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{2}$", t) else t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


_NULLISH = {"", "null", "none", "n/a", "na", "nil", "-", "unknown", "not visible", "illegible"}


def _denull(v):
    if isinstance(v, str) and v.strip().lower() in _NULLISH:
        return None
    if isinstance(v, dict):
        return {k: _denull(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_denull(x) for x in v]
    return v


def normalise(doc_type, raw, cfg, own_vat=()):
    f = _denull(json.loads(json.dumps(raw)))      # deep copy; the model sometimes writes "null" as text
    flags = []
    day_first = cfg.get("day_first_dates", True)
    for k in list(f):
        if k in DATE_FIELDS:
            f[k + "_as_written"] = f[k]
            iso = parse_date(f[k], day_first)
            if f[k] and not iso:
                flags.append(f"{k}_unparsed")
            f[k] = iso
        elif k in NUMBER_FIELDS:
            f[k] = parse_number(f[k])
    if f.get("job_number"):
        f["job_number"] = re.sub(r"\s+", "", str(f["job_number"])).upper()
    for k in ("jt_number", "po_number", "invoice_number"):
        if f.get(k):
            f[k] = re.sub(r"\s+", "", str(f[k]))
    items = []
    for it in f.get("items") or []:
        if not isinstance(it, dict):
            continue
        for k in ("unit_price", "amount"):
            if k in it:
                it[k] = parse_number(it[k])
        if doc_type == "supplier_invoice" and "quantity" in it:
            it["quantity"] = parse_number(it["quantity"])
        if any(v not in (None, "") for v in it.values()):
            items.append(it)
    f["items"] = items

    for k in REQUIRED_FOR_CLEAN[doc_type]:
        if f.get(k) in (None, ""):
            flags.append(f"missing_{k}")
    today = date.today()
    for k in DATE_FIELDS & set(f):
        if f.get(k):
            d = date.fromisoformat(f[k])
            if d > today and k != "due_date":
                flags.append(f"{k}_future")
            if d.year < today.year - 15:
                flags.append(f"{k}_too_old")
    if doc_type == "supplier_invoice":
        vat_no = re.sub(r"\D", "", str(f.get("supplier_vat_no") or ""))
        if re.search(r"mining\s*pressure", str(f.get("supplier_name") or ""), re.I) or (vat_no and vat_no in set(own_vat)):
            flags.append("supplier_is_mps")      # MPS's own document (e.g. a purchase order), not a supplier invoice
        sub, vat, tot = f.get("subtotal"), f.get("vat"), f.get("total")
        # 334: the total against the VAT (VAT / rate x (1 + rate)). When it fails but subtotal + VAT passes, the read
        # total is a misread digit (Anderbolt 85379: 5606.35 for 5806.35) and subtotal + VAT replaces it; when neither
        # passes, the invoice is flagged for review (mixed or zero-rated VAT lands there too)
        rates = ((cfg.get("doc_types", {}).get("SupplierInvoices", {}) or {}).get("cpu", {}) or {}).get("vat_rates") or [0.15, 0.14]
        if vat and tot is not None:
            by_vat = {r: round(vat / r * (1 + r), 2) for r in rates}
            if not any(abs(tot - x) <= 1.0 for x in by_vat.values()):
                r_ok = [r for r, x in by_vat.items() if sub is not None and abs(sub + vat - x) <= 1.0]
                if r_ok:
                    f["total_as_read"], f["total"] = tot, round(sub + vat, 2)
                    f.setdefault("text_fixes", []).append({"field": "total", "value": f["total"], "by": "worker",
                                                           "how": f"subtotal + VAT, which agrees with VAT at {r_ok[0]:.0%}; read {tot}"})
                    flags.append("total_from_subtotal_vat")
                    tot = f["total"]
                else:
                    flags.append("total_vat_mismatch")
        if None not in (sub, vat, tot) and abs(sub + vat - tot) > 1.0:
            flags.append("totals_mismatch")
        amounts = [it.get("amount") for it in items if it.get("amount") is not None]
        if sub is not None and amounts and abs(sum(amounts) - sub) > 1.0:
            flags.append("lines_dont_sum")
    if not items:
        flags.append("no_items")
    if doc_type == "jt":
        # the same job rule as orders: a bare number is completed from the job list by the form's date; CONS stays
        jn = str(f.get("job_number") or "").strip().upper()
        f["job_number_read"] = jn or None
        if jn and jn not in ("CONS", "CONSUMABLES"):
            m = re.fullmatch(r"(?:JOB\s*)?0*(\d{3,5})\s?\.?\s?([A-E])?", jn)
            if m:
                chk = []
                job, rec = mps_jobs.complete_job(m.group(1), m.group(2), f.get("date"), chk, written=jn)
                if job:
                    f["job_number"] = job
                f["job_customer"] = rec.get("customer") if rec else None
                flags += ["check: " + c for c in chk]
            else:
                flags.append("check: job number not understood: " + jn)
        elif jn:
            f["job_number"] = "CONS"
        def lt(i):
            q = str(i.get("quantity") or "").strip()
            u = str(i.get("unit") or "").strip()
            return (f"{q} {u}".strip() + " x " if q else "") + str(i.get("description") or "").strip()
        f["lines_text"] = "\n".join(lt(i) for i in items) or None
        f["line_count"] = len(items)
        f["item_codes"] = "; ".join(str(i["item_no"]).strip() for i in items if str(i.get("item_no") or "").strip()) or None
        f["review"] = "; ".join(x[7:] for x in flags if x.startswith("check: ")) or None
    return f, flags


# --------------------------------------------------------------------------- extraction
def ollama_up(cfg):
    try:
        with urllib.request.urlopen(cfg["ollama_url"].rstrip("/") + "/api/tags", timeout=5):
            return True
    except Exception:
        return False


def ollama_loaded(cfg):
    """Names of the models Ollama has in memory right now (empty when it is down)."""
    try:
        with urllib.request.urlopen(cfg["ollama_url"].rstrip("/") + "/api/ps", timeout=5) as r:
            return [m.get("name") for m in json.load(r).get("models", [])]
    except Exception:
        return []


def render_pages(pdf, tmp, px, max_pages):
    """Pages at `px` on the long side (like the readers: 1600; review F13, 28 Oct - was 200 dpi, A4 = 2339 px)."""
    run(["pdftoppm", "-scale-to", str(px), "-png", "-l", str(max_pages), str(pdf), str(tmp / "pg")])
    return sorted(tmp.glob("pg*.png"))


def pdf_text(pdf, max_pages):
    try:
        return run(["pdftotext", "-layout", "-l", str(max_pages), str(pdf), "-"], timeout=60).strip()
    except Exception:
        return ""


def extract_builtin(doc_type, path, cfg, tcfg, hint=None, _small=False):
    """_small: retry mode after Ollama rejects the request (HTTP 400, usually too many/too large images)."""
    max_pages = 1 if _small else int(tcfg.get("max_pages", cfg.get("max_pages_per_doc", 4)))
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        if path.suffix.lower() == ".pdf":
            images = render_pages(path, td, 1000 if _small else int(cfg.get("px", 1600)), max_pages)
            text = pdf_text(path, max_pages)
        else:
            images, text = [path], ""
        if not images:
            raise RuntimeError("no pages could be rendered")
        prompt = PROMPTS[doc_type] + " " + tcfg.get("extra_prompt", "")
        own = [str(v) for v in tcfg.get("cpu", {}).get("own_vat_numbers", ())]
        if doc_type == "supplier_invoice" and own:
            prompt += (f" MPS's own VAT number is {', '.join(own)}: it is printed in the customer / billing block and is "
                       "never the supplier's VAT number. supplier_vat_no is the VAT number printed with the supplier's details.")
        if len(text) > 200:          # born-digital PDF: give the model the exact text too
            prompt += "\n\nThe PDF also has this text layer (trust it for numbers and spelling):\n" + text[:12000]
        elif hint and hint[0]:       # Tesseract text from the CPU attempt: a helper, not the truth
            prompt += ("\n\nOCR text from the same page (may contain OCR errors; read the image, "
                       "use this only as a hint):\n" + hint[0][:12000])
        prompt += "\n\nReturn only JSON matching the schema."
        body = {
            "model": tcfg.get("model", cfg.get("model")),
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(p.read_bytes()).decode() for p in images]}],
            "format": SCHEMAS[doc_type],
            "stream": False,
            "keep_alive": cfg.get("keep_alive", "15m"),
            "options": {"temperature": 0, "num_ctx": int(cfg.get("num_ctx", 16384))},
        }
        req = urllib.request.Request(cfg["ollama_url"].rstrip("/") + "/api/chat",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=int(cfg.get("ollama_timeout", 900))) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 400 and not _small:
                log.warning("Ollama rejected %s (HTTP 400); retrying with first page at lower resolution", path.name)
                return extract_builtin(doc_type, path, cfg, tcfg, None, _small=True)
            if not ollama_up(cfg):
                raise OllamaDown(str(e))
            raise
        except urllib.error.URLError as e:
            if not ollama_up(cfg):
                raise OllamaDown(str(e))
            raise
        content = resp.get("message", {}).get("content", "")
        try:
            return json.loads(content), len(images)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise RuntimeError(f"model returned non-JSON: {content[:200]!r}")
            return json.loads(m.group(0)), len(images)


def extract_command(doc_type, path, cfg, tcfg):
    """Hand the file to an existing script (e.g. batch_run.py). The command must write a JSON object
    using the same field names as SCHEMAS[doc_type] to {out_json}."""
    with tempfile.TemporaryDirectory() as td:
        out_json = Path(td) / "result.json"
        cmd = tcfg["command"].format(pdf=shlex.quote(str(path)), out_json=shlex.quote(str(out_json)),
                                     model=shlex.quote(tcfg.get("model", cfg.get("model", ""))))
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=int(cfg.get("ollama_timeout", 900)))
        if r.returncode != 0:
            if not ollama_up(cfg):
                raise OllamaDown(r.stderr[-300:])
            raise RuntimeError(f"command failed ({r.returncode}): {r.stderr.strip()[-400:]}")
        return json.loads(out_json.read_text(encoding="utf-8")), None


# --------------------------------------------------------------------------- worker
class Worker:
    def __init__(self, cfg, lane="all"):
        self.cfg = cfg
        self.lane = lane             # "all", or "cpu": delivery notes + the CPU pass of CPU-first invoices (332)
        self.root = Path(cfg["root"])
        self.types = cfg["doc_types"]          # folder name -> settings
        self.db = sqlite3.connect(cfg.get("state_db", "/srv/ocr/pipeline/state.db"), timeout=30)
        for i in range(30):      # 332: two worker lanes may start together; the journal-mode check needs the db briefly
            try:
                self.db.execute("PRAGMA journal_mode=WAL")      # shared with the NAS collector
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or i == 29:
                    raise
                time.sleep(1)
        self.db.execute("""CREATE TABLE IF NOT EXISTS processed(
            sha256 TEXT PRIMARY KEY, folder TEXT, source TEXT, status TEXT, outputs TEXT, at TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS extractions(
            at TEXT, folder TEXT, source TEXT, via TEXT, reasons TEXT)""")
        self.db.commit()
        self.sizes = {}
        self.deferred = set()        # files waiting for the GPU while CPU work continues
        self.cpu_cache = {}          # CPU results kept while a file waits for the GPU
        self.suppliers = None
        self.supplier_csv = self.supplier_mtime = None
        for t in self.types.values():
            if t.get("cpu_first"):
                c = t.get("cpu", {})
                self.suppliers = cpu_invoice.SupplierMap(self.db, c.get("supplier_csv"), c.get("own_vat_numbers"))
                self.supplier_csv = Path(c["supplier_csv"]) if c.get("supplier_csv") else None
                self.supplier_mtime = self.supplier_csv.stat().st_mtime if self.supplier_csv and self.supplier_csv.exists() else None
                n = self.db.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
                log.info("CPU-first enabled; %d supplier VAT numbers known", n)
                break
        self.status = {"current": None, "last_done": None, "last_error": None}
        self.last_folder = None      # the queue the last document came from: the worker stays on it while it has files
        self._run = 0                # documents in a row from last_folder (config queue_batch, 332)
        self._ahead = {}             # in/ path -> (size, mtime), presort future started early (332)
        self.loaded = ([], 0.0)      # (models Ollama has in memory, when that was checked)
        mps_jobs.JOBS_DIR = str(self.root)            # jobs_*.csv, sage_invoices.csv, orders_index.csv, scan_times.csv
        if po_reader:      # supplier names confirmed on checked orders (training.py) are used to correct misreadings
            po_reader.EXTRA_VOCAB = str(self.root / "training" / "suppliers_verified.json")
            po_reader.JOBS_DIR = str(self.root)       # jobs_*.csv + sage_invoices.csv: completes "6939" to 6939D / 6939E
        # delivery notes: the Sage copy (sage_invoices.csv, written by the PC sync) and the held-for-Sage recheck
        self.dn_sage, self.dn_checked = None, None
        dn_types = [t for t in self.types.values() if t.get("doc_type") == "delivery_note"]
        if dn_types and dn_reader:
            csv_name = dn_types[0].get("dn", {}).get("sage_csv", dn_reader.DEFAULTS["sage_csv"])
            self.dn_sage = dn_reader.SageInvoices(self.root / csv_name)
        elif dn_types:
            log.error("delivery_note type configured but dn_reader.py is missing beside worker.py")
        # customer orders: Sage accounts, sales-order history and Job File orders (CSVs the PC sync writes to the share)
        self.co_lk = co_reader.Lookups(self.root) if co_reader and any(
            t.get("doc_type") == "customer_order" for t in self.types.values()) else None
        for folder in self.types:
            for sub in ("in", "out", "done", "failed", "synced", "sync_failed"):
                (self.root / sub / folder).mkdir(parents=True, exist_ok=True)
        (self.root / "work").mkdir(parents=True, exist_ok=True)
        (self.root / "flags").mkdir(parents=True, exist_ok=True)
        self.recover_work()

    def recover_work(self):
        """Anything left in work/ after a crash or reboot goes back to its in/ folder."""
        mine = "cpu~" if self.lane == "cpu" else ""          # each lane takes back only its own files (332)
        for p in (self.root / "work").glob("*__*"):
            folder, _, name = p.name.partition("__")
            if folder.startswith("cpu~") != bool(mine):
                continue
            folder = folder[len(mine):]
            if folder in self.types:
                log.info("recovering %s", p.name)
                move(p, self.root / "in" / folder, name)

    def reload_suppliers(self):
        """The PC sync rewrites suppliers.csv from Sage (via MPSDocs) daily; pick it up without a restart."""
        if not (self.suppliers and self.supplier_csv):
            return
        try:
            m = self.supplier_csv.stat().st_mtime
        except FileNotFoundError:
            return
        if m != self.supplier_mtime:
            self.supplier_mtime = m
            try:
                n = self.suppliers.load_csv(self.supplier_csv)
                log.info("supplier list reloaded: %d VAT numbers from %s", n, self.supplier_csv.name)
            except Exception as e:
                log.warning("supplier list reload failed: %s", e)

    def lane_wants(self, folder, p):
        """Which worker takes a file (332). Without config "cpu_lane" one worker takes everything, as before. With it,
        `worker.py --lane cpu` takes delivery notes and the CPU pass of CPU-first invoices; an invoice that pass rejects
        gets a <file>.cpu.json note in _meta and stays queued for the main worker, which reads only noted invoices (on
        the GPU, with the saved OCR text) and no delivery notes."""
        if not self.cfg.get("cpu_lane"):
            return True
        t = self.types[folder]
        dn = t.get("doc_type") == "delivery_note"
        if not t.get("cpu_first"):
            return dn if self.lane == "cpu" else not dn
        noted = self.cpu_note(folder, p.name).exists()
        return (not noted) if self.lane == "cpu" else noted

    def cpu_note(self, folder, name):
        return self.root / "in" / folder / "_meta" / (name + ".cpu.json")

    def write_cpu_note(self, folder, name, digest):
        """The CPU lane's rejected pass, for the GPU worker (written before the file goes back to in/)."""
        for key in [k for k in self.cpu_cache if k.startswith(digest + ":")]:
            fields, reasons, text, text_src = self.cpu_cache.pop(key)
            p = self.cpu_note(folder, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(p, json.loads(json.dumps({"key": key, "fields": fields, "reasons": reasons, "text": text,
                                                        "text_src": text_src, "at": now_iso()}, default=str)))

    def read_page(self, fn, fewshot, extra):
        """fn(fewshot_dir) reads one page. An Ollama 5xx (29 Oct: "prediction aborted, token repeat limit reached" - the
        model loops on some pages when shown the worked examples) is read once more without them; a page that still
        fails, or times out, raises PageReadFailed so the file's other pages are kept (332)."""
        try:
            try:
                return fn(fewshot)
            except urllib.error.HTTPError as e:
                if e.code < 500 or not fewshot:
                    raise
                why = http_error_text(e)
                log.warning("Ollama HTTP %s (%s): reading the page again without worked examples", e.code, why)
                extra["retried_without_examples"] = f"HTTP {e.code}: {why}"
                return fn(None)
        except urllib.error.HTTPError as e:
            raise PageReadFailed(f"HTTP {e.code}: {http_error_text(e)}")
        except TimeoutError as e:
            raise PageReadFailed(f"timed out ({e})")
        except (urllib.error.URLError, ConnectionError) as e:
            if not ollama_up(self.cfg):
                raise OllamaDown(str(e))
            raise PageReadFailed(str(e)[:200])

    def presort_ahead(self, folder, src):
        """Starts the presort of the file this queue will most likely give the worker next (its oldest other file), so
        that file's Tesseract pass runs on the CPU while the GPU reads this one (332: ~3 s of GPU idle per order page
        before). Single-page files only: split files prefetch their own pages."""
        if not presort:
            return
        for p in [p for p in self._ahead if not p.exists()]:
            self._ahead.pop(p, None)
        if len(self._ahead) >= 2:
            return
        try:
            nxt = min((p for p in (self.root / "in" / folder).iterdir() if p.is_file() and p != src
                       and p.suffix.lower() == ".pdf" and not IGNORE.search(p.name) and p not in self._ahead),
                      key=lambda p: p.stat().st_mtime, default=None)
            if nxt is None or int(re.search(r"Pages:\s+(\d+)", run(["pdfinfo", str(nxt)], 30)).group(1)) != 1:
                return
            st = nxt.stat()
        except Exception:
            return
        meta = read_json_file(self.root / "in" / folder / "_meta" / (nxt.name + ".json")) or {}
        fut = self.presort_start(nxt, meta, nxt)
        if fut:
            self._ahead[nxt] = ((st.st_size, st.st_mtime), fut)

    def take_ahead(self, src):
        """The presort started early for this file, once finished (it reads the in/ path, which is about to move)."""
        got = self._ahead.pop(src, None)
        if not got:
            return None
        sig, fut = got
        try:
            st = src.stat()
            if sig != (st.st_size, st.st_mtime):
                return None
            fut.result(timeout=120)
        except Exception:
            return None
        return fut

    def paused(self):
        return (self.root / "PAUSE").exists()

    def type_off(self, folder):
        """Per-type switch set from the dashboard: flags/process_off_<Type> stops new files of that type."""
        return (self.root / "flags" / f"process_off_{folder}").exists()

    def write_status(self, ollama=None):
        q = {f: sum(1 for p in (self.root / "in" / f).iterdir() if p.is_file() and not IGNORE.search(p.name))
             for f in self.types}
        st = dict(self.status, queue=q, paused=self.paused(), ollama_up=ollama, updated_at=now_iso(),
                  types_off=[f for f in self.types if self.type_off(f)], pid=os.getpid(),
                  waiting_for_gpu=len(self.deferred))
        try:
            atomic_write_json(self.root / ("status_cpu.json" if self.lane == "cpu" else "status.json"), st)
        except OSError as e:
            log.warning("status write failed: %s", e)

    def queue_model(self, folder):
        """The vision model a queue's documents are read with, or None for CPU-only work (delivery notes) and
        CPU-first invoices (the GPU is only a fallback there)."""
        t = self.types[folder]
        dt = t.get("doc_type")
        if dt == "delivery_note" or t.get("cpu_first"):
            return None
        if dt == "po" and po_reader and t.get("po_reader", True) is not False:
            pc = dict(t.get("po_reader")) if isinstance(t.get("po_reader"), dict) else {}
            act = read_json_file(self.root / "training" / "active.json") or {}
            pc.update({k: act[k] for k in ("model",) if act.get(k)})
            return pc.get("model", "qwen2.5vl:7b")
        if dt == "ws":
            return self.ws_settings()["model"]
        if dt == "jt" and jt_reader and t.get("jt_reader", True) is not False:
            return self.jt_settings(folder)["model"]
        if dt == "test_cert":
            return self.tc_settings(folder)["model"]
        if dt == "pop":
            return self.pop_model()
        return t.get("model", self.cfg.get("model"))

    def jt_settings(self, folder="JobTransfers"):
        """JT reader settings: config doc_types.<folder>.jt_reader ({model, fewshot_dir, px, prompt, number_prefix}) over
        JobTransfers' own; qwen2.5vl and the jt_fewshot worked examples beside this file unless set (benchmark 23 Oct
        2026: eval_jt.py). RoninJobTransfers (29 Oct, 331): jt_prompt_ronin.txt and the DF prefix."""
        t = self.types.get("JobTransfers") or {}
        c = dict(t.get("jt_reader")) if isinstance(t.get("jt_reader"), dict) else {}
        own = (self.types.get(folder) or {}).get("jt_reader")
        if folder != "JobTransfers" and isinstance(own, dict):
            c.update(own)
        c.setdefault("model", "qwen2.5vl:7b")
        c.setdefault("fewshot_dir", str(Path(__file__).resolve().with_name("jt_fewshot")))
        c.setdefault("px", 1600)
        return c

    def tc_settings(self, folder="TestCerts"):
        """Test-certificate reader settings: config doc_types.<folder>.tc_reader. qwen3-vl:8b-instruct without worked
        examples unless set (50-file test 24 Oct 2026: qwen3-vl zero-shot ahead of qwen2.5vl + one example on 5 of 6
        fields)."""
        t = self.types.get(folder) or {}
        c = dict(t.get("tc_reader")) if isinstance(t.get("tc_reader"), dict) else {}
        c.setdefault("model", "qwen3-vl:8b-instruct")
        c.setdefault("fewshot_dir", "")                # "" = no worked examples
        c.setdefault("px", 1600)
        c.setdefault("chem_px", 2000)
        c.setdefault("max_pages", 30)
        act = read_json_file(self.root / "training" / "tc" / "active.json") or {}     # set by tc_training.py quick
        for k in ("model", "fewshot_dir", "px"):
            if act.get(k) not in (None, ""):
                c[k] = act[k]
        return c

    def tc_part(self, work, page_nos, out):
        """500: the pages page_nos of work as their own PDF."""
        with tempfile.TemporaryDirectory() as t:
            files = []
            for p in page_nos:
                f = os.path.join(t, f"p{p:04d}.pdf")
                subprocess.run(["pdfseparate", "-f", str(p), "-l", str(p), str(work), f], check=True, capture_output=True, timeout=120)
                files.append(f)
            if len(files) == 1:
                shutil.copyfile(files[0], out)
            else:
                subprocess.run(["pdfunite", *files, str(out)], check=True, capture_output=True, timeout=300)
        return out

    def read_tc(self, folder, work, src, meta, digest, loaded_before, td=None):
        """One test-certificate file -> one document per test certificate number in it (500, Dino 5 Nov: a scanned
        batch); a file with one cert is one document as before (extra pages are recorded, not split off)."""
        if not tc_reader:
            raise RuntimeError("tc_reader.py is missing beside worker.py")
        c = self.tc_settings(folder)
        name = meta.get("original_name") or src.name
        fam = meta.get("family") if meta.get("family") in tc_reader.FAMILIES else None    # the old library (backfill)
        t0 = time.time()
        reuse = read_json_file(self.root / str(meta.get("reuse_tc_pages"))) if meta.get("reuse_tc_pages") else None
        if reuse and reuse.get("source_sha256") == digest and reuse.get("tc_pages"):
            pages, n = reuse["tc_pages"], int(reuse.get("pages_read") or len(reuse["tc_pages"]))     # 500: read already
            log.info("%s %s: page readings taken from %s", folder, src.name, meta["reuse_tc_pages"])
        else:
          try:
            pages, n = tc_reader.read(work, self.cfg["ollama_url"], c["model"], c["fewshot_dir"], fam, int(c["max_pages"]),
                                      int(c["px"]), int(c["chem_px"]), int(self.cfg.get("ollama_timeout", 900)),
                                      self.cfg.get("keep_alive", "15m"))
          except (urllib.error.URLError, ConnectionError) as e:
            if not ollama_up(self.cfg):
                raise OllamaDown(str(e))
            raise
        real = [p for p in pages if p.get("page")]
        errs = [p for p in real if p.get("how") == "error"]
        if real and len(errs) == len(real):            # nothing was read: not a reading (Ollama down, or a bad file)
            if not ollama_up(self.cfg):
                raise OllamaDown(errs[0].get("error") or "every page failed")
            raise RuntimeError("every page failed: " + str(errs[0].get("error"))[:300])
        groups = tc_reader.split_certs(pages) if td is not None and hasattr(tc_reader, "split_certs") and not meta.get("rejoined_from") else []   # 533: not a rejoined file
        if len(groups) > 1:                              # 500: one document per test certificate number
            out = []
            for i, g in enumerate(groups, 1):
                sub = [dict(p, page=j, batch_page=p["page"]) for j, p in enumerate((q for q in pages if q.get("page") in g), 1)]
                part = self.tc_part(work, g, Path(td) / f"cert{i:02d}.pdf")
                pd = hashlib.sha256(part.read_bytes()).hexdigest()
                fields, flags, extra = self.tc_document(sub, part, pd, name, fam, meta, c, loaded_before, src)
                fields["split_from"] = f"{name}, pages {tc_reader.page_ranges([(x, 'x') for x in g]).replace(' x', '')} of {n}"
                out.append((g[0], part, fields, flags, pd[:16], "tc", len(g), (time.time() - t0) / len(groups), extra))
            log.info("%s %s %.1fs via tc_reader (%d pages): %d certs %s", folder, src.name, time.time() - t0, n, len(groups),
                     [f.get("cert_no") for _, _, f, *_ in out])
            return out
        fields, flags, extra = self.tc_document(pages, work, digest, name, fam, meta, c, loaded_before, src)
        log.info("%s %s %.1fs via tc_reader (%d pages) flags=%s", folder, src.name, time.time() - t0, n, flags)
        return [(None, work, fields, flags, digest[:16], "tc", n, time.time() - t0, extra)]

    def tc_document(self, pages, work, digest, name, fam, meta, c, loaded_before, src):
        """One test certificate's page readings -> (fields, flags, extra)."""
        errs = [p for p in pages if p.get("page") and p.get("how") == "error"]
        fields, flags = tc_reader.to_fields(tc_reader.merge(pages, name, fam), name, fam)
        fields["content_hash"] = digest.upper()
        if errs:
            flags.append(f"check: {len(errs)} page(s) could not be read")
        for k in ("old_columns", "old_url", "sp_item_id", "sp_url", "sync_mode", "replaces"):   # backfill: the library item it came from; 513: the combined item a part replaces
            if meta.get(k) not in (None, ""):
                fields[k] = meta[k]
        for it in fields.get("items") or []:          # the Test Cert Items "Unit" choice: pcs, m, kg
            u = str(it.get("unit") or "").strip().lower().rstrip(".")
            it["unit"] = ({"pc": "pcs", "pcs": "pcs", "piece": "pcs", "pieces": "pcs", "nos": "pcs", "no": "pcs", "ea": "pcs",
                           "each": "pcs", "m": "m", "mtr": "m", "mtrs": "m", "meter": "m", "meters": "m", "metre": "m",
                           "metres": "m", "kg": "kg", "kgs": "kg"}.get(u, it.get("unit"))) if u else it.get("unit")
        if fields.get("old_columns") and hasattr(tc_reader, "check_old_heats"):
            tc_reader.check_old_heats(fields, flags)     # the old site's heat numbers are right (Dino 25 Oct)
        if tc_orders:                                    # import orders and the L/C from the cert's own text (Dino 26 Oct)
            try:
                tc_orders.apply(fields, flags, str(work), digest[:32], int(c["max_pages"]))
            except Exception as e:
                log.warning("tc_orders on %s: %s", src.name, e)
        extra = {"tc_model": c["model"], "tc_pages": pages, "models_loaded_before": loaded_before}
        return fields, flags, extra

    def pop_model(self):
        """Proofs of payment are read with the order model unless ProofOfPayments names its own, so a payment
        page inside an order batch never swaps models."""
        t = self.types.get("ProofOfPayments") or {}
        if t.get("model"):
            return t["model"]
        po_folder = next((f for f, c in self.types.items() if c.get("doc_type") == "po"), None)
        return self.queue_model(po_folder) if po_folder else self.cfg.get("model")

    def models_loaded(self, max_age=10.0):
        names, at = self.loaded
        if time.time() - at > max_age:
            names = ollama_loaded(self.cfg)
            if set(names) != set(self.loaded[0]):
                log.info("Ollama has loaded: %s", ", ".join(names) or "nothing")
            self.loaded = (names, time.time())
        return names

    def next_ready(self, gpu_ok=True):
        """The next file to read. Files are grouped by queue so the vision model is not swapped document by
        document: the worker stays on the queue it last read from while that queue has ready files, then prefers a
        queue whose model Ollama already has loaded, then the oldest file. CPU-only work (delivery notes, CPU-first
        invoices) is taken in age order alongside, since it never touches the GPU.
        Without the GPU, only CPU-first types are picked, and not files already waiting for the GPU."""
        stable = float(self.cfg.get("stable_seconds", 10))
        ready = {}      # folder -> [(mtime, folder, path)]
        for folder in self.types:
            cpu = self.types[folder].get("cpu_first") or self.types[folder].get("doc_type") == "delivery_note"
            if self.type_off(folder) or (not gpu_ok and not cpu):
                continue
            for p in (self.root / "in" / folder).iterdir():
                if not p.is_file() or IGNORE.search(p.name) or (not gpu_ok and p in self.deferred):
                    continue
                if not self.lane_wants(folder, p):
                    continue
                if p.suffix.lower() not in ACCEPT:
                    log.warning("unsupported file type, moving to failed: %s", p)
                    self.fail(folder, p, f"unsupported file type {p.suffix}")
                    continue
                st = p.stat()
                sig = (st.st_size, st.st_mtime)
                prev = self.sizes.get(p)
                if prev is None or prev[0] != sig:
                    self.sizes[p] = (sig, time.time())
                    continue
                if time.time() - prev[1] >= stable and time.time() - st.st_mtime >= max(
                        stable, float(self.types[folder].get("min_age_seconds", 0))):    # customer orders wait for Job File
                    ready.setdefault(folder, []).append((st.st_mtime, folder, p))
        if not ready:
            return None
        cpu_folders = [f for f in ready if self.queue_model(f) is None]
        gpu_folders = [f for f in ready if f not in cpu_folders]
        group = None
        if gpu_folders:
            # the worker stays on the queue it last read from - but after queue_batch documents in a row, a queue read
            # with the SAME model that has files waiting gets its turn (332: 9 Ronin JTs waited behind 1,660 orders;
            # moving between qwen2.5vl queues costs no model load)
            same = [f for f in gpu_folders if f != self.last_folder and self.queue_model(f) == self.queue_model(self.last_folder)] \
                if self.last_folder in gpu_folders else []
            if same and self._run >= int(self.cfg.get("queue_batch", 10)):
                group = min(same, key=lambda f: min(ready[f]))
            elif self.last_folder in gpu_folders:
                group = self.last_folder
            else:
                loaded = set(self.models_loaded()) if gpu_ok else set()
                same = [f for f in gpu_folders if self.queue_model(f) in loaded]
                pool = same or gpu_folders
                group = min(pool, key=lambda f: min(ready[f]))      # the queue holding the oldest file
        cands = [c for f in cpu_folders for c in ready[f]] + (ready[group] if group else [])
        return min(cands)

    def fail(self, folder, path, msg, name=None):
        if not path.exists():       # already moved (e.g. re-queued by a worker that was stopping): log it, don't crash
            log.error("failed: %s (%s) - the file is no longer at %s", name or path.name, msg[:200], path)
            self.status["last_error"] = {"file": name or path.name, "error": msg[:300], "at": now_iso()}
            return None
        dst = move(path, self.root / "failed" / folder, name)
        dst.with_name(dst.name + ".error.txt").write_text(f"{now_iso()}\n{msg}\n", encoding="utf-8")
        self.status["last_error"] = {"file": path.name, "error": msg[:300], "at": now_iso()}

    def split(self, path, td, tcfg):
        """Returns [(page_no or None, pdf_path)]."""
        if path.suffix.lower() != ".pdf":
            return [(None, path)]
        pages = int(re.search(r"Pages:\s+(\d+)", run(["pdfinfo", str(path)], 60)).group(1))
        if not tcfg.get("split_pages") or pages == 1:
            return [(None, path)]
        run(["pdfseparate", str(path), str(td / "page-%03d.pdf")])
        return [(i, td / f"page-{i:03d}.pdf") for i in range(1, pages + 1)]

    def fix_invoice_from_text(self, fields, flags, part, text, tcfg, extra):
        """The model often misses the supplier's VAT number (every invoice to MPS also prints MPS's own) and invoice
        numbers printed under a "Document No" column. Fill empty ones from the PDF text; record each fill in
        extra["text_fixes"] so it shows in the sidecar."""
        own = set(str(v) for v in tcfg.get("cpu", {}).get("own_vat_numbers", ()))
        fixes = []
        vat = re.sub(r"\D", "", str(fields.get("supplier_vat_no") or ""))
        if vat and vat in own and not re.search(r"mining\s*pressure", str(fields.get("supplier_name") or ""), re.I):
            fields["supplier_vat_no"], vat = None, ""             # MPS's number from the customer block, not the supplier's
            flags = [x for x in flags if x != "supplier_is_mps"]
            fixes.append({"field": "supplier_vat_no", "value": None, "how": "model returned MPS's own VAT number; dropped"})
        need_vat = not re.fullmatch(r"4\d{9}", vat)
        need_no = not fields.get("invoice_number")
        if (need_vat or need_no) and "supplier_is_mps" not in flags:
            if text is None and part.suffix.lower() == ".pdf":
                text = pdf_text(part, int(tcfg.get("max_pages", self.cfg.get("max_pages_per_doc", 4))))
                extra.setdefault("text_source", "text_layer" if len(text or "") > 200 else None)
            # OCR text (Tesseract, or a text layer an OCR tool added to a scan) can misread digits: Sage list only
            ocr = extra.get("text_source") == "tesseract" or (part.suffix.lower() == ".pdf" and cpu_invoice.ocr_made_text_layer(part))
            if not cpu_invoice.mentions_invoice(text):
                text = None                                          # a letter, certificate, bill of lading...: leave it
            if need_vat and text:
                v, how = cpu_invoice.supplier_vat(text, own, self.suppliers, known_only=ocr)
                if v:
                    fields["supplier_vat_no"] = v
                    fixes.append({"field": "supplier_vat_no", "value": v, "how": how})
            if need_no and text and not ocr:
                n, how = cpu_invoice.invoice_number_column(text)
                if n:
                    fields["invoice_number"] = n
                    flags = [x for x in flags if x != "missing_invoice_number"]
                    fixes.append({"field": "invoice_number", "value": n, "how": how})
        if fixes:
            extra["text_fixes"] = fixes
            log.info("filled from the PDF text: %s", "; ".join(f"{x['field']}={x['value']} ({x['how']})" for x in fixes))
        return fields, flags

    def presort_start(self, part, meta, src):
        """Pre-sort (27 Oct shadow; gate from 28 Oct, review F1): presort.verdict() on the CPU, in a thread."""
        if not presort or (self.types.get("PurchaseOrders") or {}).get("presort") is False:
            return None
        if getattr(self, "_presort_pool", None) is None:
            self._presort_pool = ThreadPoolExecutor(max_workers=1)
        try:
            return self._presort_pool.submit(presort.verdict, part, meta.get("original_name") or src.name, meta.get("source_folder"))
        except Exception as e:
            log.warning("presort not started: %s", e)
            return None

    def presort_prefetch(self, parts, meta, src):
        """Presorts for every page of a split file, started up front: page n's verdict is computed on the CPU while the
        GPU reads page n-1 (one worker thread, so they run in order)."""
        return {part: self.presort_start(part, meta, src) for _, part in parts}

    @staticmethod
    def presort_collect(fut, extra):
        """The verdict into extra["presort"]; returns the page text the presort read (not kept in the sidecar)."""
        if fut is None:
            return None
        try:
            v = fut.result(timeout=600)
            text = v.pop("_page_text", None) if isinstance(v, dict) else None
            extra["presort"] = v
            return text
        except Exception as e:
            extra["presort"] = {"error": str(e)[:200]}
            return None

    def presort_route(self, part, page, verdict, meta, src, folder):
        """A page the presort is sure about goes to its own queue without a GPU read (review F1, 28 Oct): copied to
        in/<dest>/ with the usual _meta note, so the other queue's reader sees it like a re-routed document."""
        dest = verdict["dest"]
        d, md = self.root / "in" / dest, self.root / "in" / dest / "_meta"
        d.mkdir(parents=True, exist_ok=True)
        md.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|#%]+', " ", Path(meta.get("original_name") or src.name).stem)[:120]
        name = stem + (f"_p{page}" if page else "") + ".pdf"
        if (d / name).exists() and sha256(d / name) == sha256(part):
            log.info("%s %s%s already in in/%s/ (same file, earlier run)", folder, src.name, f" p{page}" if page else "", dest)
            return f"{dest}/in/{name}"
        k = 2
        while (d / name).exists() or (md / (name + ".json")).exists():
            name = f"{stem}{f'_p{page}' if page else ''}_{k}.pdf"
            k += 1
        note = {"original_name": meta.get("original_name") or src.name, "rerouted_from": f"in/{folder}/{src.name} (presort)",
                "presort": verdict, "routed_at": now_iso()}
        for key in ("source_folder", "source_modified"):
            if meta.get(key):
                note[key] = meta[key]
        (md / (name + ".json")).write_text(json.dumps(note, indent=1), encoding="utf-8")
        shutil.copy2(part, d / (name + ".tmp"))
        os.replace(d / (name + ".tmp"), d / name)
        log.info("%s %s%s -> in/%s/%s by presort (%s), no GPU read", folder, src.name, f" p{page}" if page else "", dest, name,
                 "; ".join(verdict.get("signals") or []))
        return f"{dest}/in/{name}"

    def payment_pages(self, part, tcfg):
        """(pages that are payment confirmations, their OCR text per page, page count) for a multi-page PDF; single-page
        files are left to the PO reader's own judgement (it says proof_of_payment) to keep Tesseract off every order."""
        if not pop_reader or part.suffix.lower() != ".pdf":
            return [], [], 1
        try:
            n = int(re.search(r"Pages:\s+(\d+)", run(["pdfinfo", str(part)], 60)).group(1))
        except Exception:
            return [], [], 1
        if n < 2:
            return [], [], n
        texts = pop_reader.ocr_pages(part, min(n, int(tcfg.get("pop_scan_pages", 8))))
        return [i + 1 for i, t in enumerate(texts) if pop_reader.is_payment_page(t)], texts, n

    @staticmethod
    def order_pages(first, npdf, pop_pages, limit):
        """How many pages from `first` the PO reader gets: stop at the next payment page."""
        n, i = 0, first
        while i <= npdf and i not in pop_pages and n < limit:
            n, i = n + 1, i + 1
        return max(n, 1)

    def read_pop(self, part, tcfg):
        """Proof of payment: the vision model reads it, Tesseract reads it again; disagreements are flagged."""
        texts = pop_reader.ocr_pages(part, int(tcfg.get("max_pages", 2)))
        ocr = pop_reader.parse_standard_bank(texts[0]) if texts else {}
        ptcfg = dict(self.types.get("ProofOfPayments", tcfg))
        ptcfg["model"] = self.pop_model()
        try:
            raw, npages = extract_builtin("pop", part, self.cfg, ptcfg)
        except (urllib.error.URLError, ConnectionError) as e:
            if not ollama_up(self.cfg):
                raise OllamaDown(str(e))
            raise
        fields, flags = pop_reader.merge(raw, ocr)
        return fields, flags, npages

    def ws_route_ok(self):
        return bool(ws_reader) and "WorkshopSheets" in self.types

    def ws_settings(self):
        """Workshop sheet reader settings: config.json doc_types.WorkshopSheets.ws_reader, overridden by a training job
        (training/ws/active.json)."""
        c = dict((self.types.get("WorkshopSheets") or {}).get("ws_reader") or {})
        act = read_json_file(self.root / "training" / "ws" / "active.json") or {}
        c.update({k: act[k] for k in ("model", "fewshot_dir", "px") if act.get(k)})
        c.setdefault("model", "qwen2.5vl:7b")
        c.setdefault("fewshot_dir", str(Path(__file__).resolve().with_name("ws_fewshot")))
        c.setdefault("px", 1600)
        return c

    def read_ws_pages(self, pdf, td, src_name, digest, routed_from=None, why=None):
        """One workshop sheet per page -> staged entries (page, part, fields, flags, doc_id, "ws", 1, secs, extra)."""
        td = Path(td)
        td.mkdir(parents=True, exist_ok=True)
        pages = [(None, pdf)]
        if pdf.suffix.lower() == ".pdf":
            n = int(re.search(r"Pages:\s+(\d+)", run(["pdfinfo", str(pdf)], 60)).group(1))
            if n > 1:
                run(["pdfseparate", str(pdf), str(td / "sheet-%03d.pdf")])
                pages = [(i, td / f"sheet-{i:03d}.pdf") for i in range(1, n + 1)]
        c = self.ws_settings()
        nex = len(ws_reader.fewshot_msgs(c["fewshot_dir"])) // 2
        out = []
        for i, pp in pages:
            while self.paused() and not _stop:
                time.sleep(5)
            if _stop:
                raise OllamaDown("shutdown")
            t0 = time.time()
            try:
                ans, how = ws_reader.read(pp, self.cfg["ollama_url"], c["model"], c["fewshot_dir"], int(c["px"]),
                                          int(self.cfg.get("ollama_timeout", 1200)), self.cfg.get("keep_alive", "15m"))
            except (urllib.error.URLError, ConnectionError) as e:
                if not ollama_up(self.cfg):
                    raise OllamaDown(str(e))
                raise
            fields, flags = ws_reader.to_fields(ans, src_name, self.root)
            fields["content_hash"] = sha256(pp).upper()      # = the library's Content Hash (SHA-256 of the sheet's PDF)
            extra = {"doc_type": "ws", "ws_how": how, "ws_model": "text-layer+rules" if how == "text_layer" else c["model"],
                     "ws_examples": 0 if how == "text_layer" else nex}
            if routed_from:
                extra.update(routed_from=routed_from, ws_check=why)
            doc_id = f"{digest[:16]}" + (f"-p{i:02d}" if i else "")
            out.append((i, pp, fields, flags, doc_id, "ws", 1, time.time() - t0, extra))
            log.info("WorkshopSheets %s%s %.1fs via %s flags=%s", src_name, f" p{i}" if i else "", time.time() - t0, how, flags)
        return out

    def out_path(self, folder, name):
        """outputs[] entries are a file name in out/<folder>, "hold/<name>" when it was held there, or
        "<OtherType>/<name>" / "<OtherType>/hold/<name>" when the document was routed."""
        if name.startswith("hold/"):
            return self.root / "out" / folder / name
        return self.root / "out" / (name if "/" in name else f"{folder}/{name}")

    def route(self, folder, doc_type, fields):
        """Out folder for a finished document. Polymer Lining Systems orders (PL order form) read from the PurchaseOrders
        queue go to PolymerOrders when that type is configured (its own SharePoint library); everything else stays put.
        A type can also list its own rules: "routes": [{"field": "form", "equals": "polymer_order", "to": "PolymerOrders"}]."""
        if doc_type == "pop" and folder != "ProofOfPayments" and "ProofOfPayments" in self.types:
            return "ProofOfPayments"
        if doc_type == "ws" and folder != "WorkshopSheets" and "WorkshopSheets" in self.types:
            return "WorkshopSheets"
        rules = self.types[folder].get("routes")
        if rules is None and doc_type == "po":
            rules = [{"field": "form", "equals": "polymer_order", "to": "PolymerOrders"},
                     {"field": "order_number", "startswith": "PL", "to": "PolymerOrders"}]
        for r in rules or []:
            v = str(fields.get(r.get("field")) or "")
            if ("equals" in r and v == r["equals"]) or ("startswith" in r and v.startswith(r["startswith"])):
                if r.get("to") in self.types:
                    return r["to"]
        return folder

    def hold_reason(self, dest, doc_type, fields, flags):
        """Why a finished document goes to out/<dest>/hold/ instead of the queue the PC syncs. A type's "hold_unless"
        lists the shapes a document may have; one that matches none waits for review. PurchaseOrders:
          [{"order_number": "^A\\d{5}$"}, {"form": "^internal_group_transfer$", "order_number": "^\\d{5}$"}]
        = an MPS order with its A-number, or an IGT with its 5-digit number; a quote approval, an attached page or an
        order whose number was not read is held."""
        if fields.get("read_error"):                    # 332: the page failed even after the retry
            return "page could not be read: " + str(fields["read_error"])[:150]
        if doc_type == "test_cert":
            if "not_a_test_cert" in flags:
                return "no test certificate page found"
            if not fields.get("item_count"):
                return "no items read"
            if fields.get("family") in (None, "", "Other"):
                return "family Other: not pipe, flanges or fittings"
            if "no_heats" in flags and not fields.get("heat_count"):
                return "no heats read"
            # Dino 28 Oct (review F14): only those structural problems hold a cert; every other check stays in the
            # Check column and the cert may be uploaded (25 Oct rule "any check holds" put 82 % of certs on hold)
            return None
        rules = (self.types.get(dest) or {}).get("hold_unless")
        if not rules:
            return None
        if isinstance(rules, dict):
            rules = [rules]
        for alt in rules:
            if all(re.fullmatch(pat, str(fields.get(k) or "").strip(), re.I) for k, pat in alt.items()):
                return None
        if doc_type == "jt":
            jn = str(fields.get("jt_number") or "").strip()
            return f"JT number '{jn}' is not in the expected form" if jn else "no JT number read"
        on = str(fields.get("order_number") or "").strip()
        return f"order number '{on}' is not an A-number" if on else "no order number read"

    @staticmethod
    def keep_pop_fields(fields, flags, pop_pages, pop_texts, all_pop, meta):
        """A payment confirmation kept in the PO library: Document Type "Proof of Payment" (with "Purchase Order" too when
        the file also holds the order), payee from the bank page or the scan's supplier folder, not flagged as
        "not an order"."""
        order_read = not all_pop and "not_an_order" not in flags
        base = (fields.get("library_doc_type") or "Purchase Order").split(";") if order_read else []
        fields["library_doc_type"] = ";".join(dict.fromkeys([x for x in base if x != "Other"] + ["Proof of Payment"]))
        fields["payment_pages"] = pop_pages
        pay = pop_reader.parse_standard_bank(pop_texts[pop_pages[0] - 1]) if pop_texts and len(pop_texts) >= pop_pages[0] else {}
        if pay:
            fields["payment_ocr"] = pay          # Tesseract only (unchecked; it misreads 1 as 4)
        if order_read:
            return fields, flags
        chk = list(fields.get("checks") or [])
        folder_sup = re.split(r"[\\/]", str(meta.get("source_folder") or "").rstrip("\\/"))[-1].strip()
        folder_sup = "" if re.fullmatch(r"(?:\d{4}|[A-Z]|ORDERS?.*|MELYSSA.*|IVOR.*|)", folder_sup.upper()) else folder_sup.upper()
        sup = str(fields.get("supplier") or "")
        if not sup or sup == "UNKNOWN SUPPLIER" or "PROOF OF PAYMENT" in sup.upper():
            new = folder_sup or str(pay.get("account_name") or "").upper() or None
            chk = [c for c in chk if not str(c).startswith("supplier not read")]
            flags = [f for f in flags if not str(f).startswith("check: supplier not read")]
            if new:
                chk.append(f"payee taken from the {'scan folder' if folder_sup else 'bank page'}: {new}")
                flags.append("check: " + chk[-1])
            fields["supplier"] = fields["supplier_name"] = new
        if pay.get("amount") is not None and fields.get("order_value") is None:
            fields["payment_amount"] = pay["amount"]
        fields["form"], fields["form_label"], fields["po_doc_type"] = "proof_of_payment", "Proof of payment", "proof_of_payment"
        # a bank page never carries the order number: the one from the file name is expected, not a check
        chk = [c for c in chk if not str(c).startswith("order number not read")]
        flags = [f for f in flags if not str(f).startswith("check: order number not read")]
        fields["checks"] = chk
        fields["review"] = "; ".join(chk) or None
        flags = [f for f in flags if f not in ("not_an_order", "missing_supplier")]
        if not fields.get("supplier"):
            flags.append("missing_supplier")
        return fields, flags

    def save_supporting(self, staged, folder, meta):
        """The reading of a supporting page (fed by supporting.py) to work/supporting/results/<scan>_p<page>.json."""
        s = meta["supporting_of"]
        d = self.root / "work" / "supporting" / "results"
        d.mkdir(parents=True, exist_ok=True)
        docs = [{"page": e[0], "doc_type": e[8].get("doc_type") or self.types[folder].get("doc_type"), "fields": e[2],
                 "flags": e[3], "extractor": e[5]} for e in staged]
        rec = {"supporting_of": s, "type": folder, "read_at": now_iso(), "docs": docs}
        atomic_write_json(d / f"{str(s.get('source_sha'))[:16]}_p{int(s.get('source_page') or 0):03d}.json", rec)
        log.info("%s supporting page %s p%s (behind %s) read: %d result(s), not published", folder, str(s.get("source_sha"))[:12],
                 s.get("source_page"), s.get("order") or s.get("parent"), len(docs))

    def group_pages(self, staged, folder, src, work, meta, digest, td, outputs):
        """One file = one document (Dino 29 Oct 2026). staged: one entry per page of a split file. Returns the documents
        to publish: each order form with its supporting pages (one PDF, order page first), or - no order form - the
        whole file as one document, unless its first page belongs to another queue (then the whole file goes there)."""
        import subprocess as _sp
        forms = {"mps_order", "polymer_order", "internal_group_transfer", "ronin_order"}
        ents = sorted(staged, key=lambda e: e[0] or 0)

        def is_order(e):
            f = e[2] or {}
            return e[5] == "po_v2" and f.get("form") in forms and bool(f.get("order_number"))

        def kind(e):
            x, f = e[8], e[2] or {}
            if x.get("_divert"):
                return x["_divert"].get("kind")
            if x.get("_ws"):
                return "workshop_sheet"
            if x.get("doc_type") == "pop":
                return "proof_of_payment"
            if is_order(e):
                return f.get("form")
            return f.get("library_doc_type") or f.get("po_doc_type") or "other"

        def summary(e):
            f = e[2] or {}
            keep = {k: f.get(k) for k in ("order_number", "supplier", "supplier_name", "account_name", "invoice_number",
                                          "date", "invoice_date", "total", "amount", "order_value")
                    if f.get(k) not in (None, "", [], {})}
            return {"page": e[0], "kind": kind(e), **keep}

        orders = [i for i, e in enumerate(ents) if is_order(e)]
        if not orders:
            first, x = ents[0], ents[0][8]
            ps, dest = dict(x.get("_divert") or x.get("presort") or {}), None
            if x.get("_divert"):
                dest = ps.get("dest")
            elif x.get("_ws") and self.ws_route_ok():
                dest = "WorkshopSheets"
                ps = {"kind": "workshop_sheet", "strength": "likely", "signals": [str(x["_ws"])]}
            elif (first[5] != "pop" and ps.get("strength") in ("sure", "likely") and ps.get("dest")
                  and ps["dest"] != folder and ps["dest"] in self.types):
                dest = ps["dest"]
            if dest and dest in self.types and dest != folder:
                outputs.append(self.presort_route(work, None, dict(ps, dest=dest), meta, src, folder))
                log.info("%s %s: no order form; page 1 is %s -> the whole file (%d pages) to %s", folder, src.name,
                         ps.get("kind"), len(ents), dest)
                return []
            segs, mains = [list(range(len(ents)))], [0]
        else:
            mains = []
            for oi in orders:                  # the same order number again (page 2 of a form) is not a new document
                if mains and ents[mains[-1]][2].get("order_number") == ents[oi][2].get("order_number"):
                    continue
                mains.append(oi)
            segs = [[] for _ in mains]
            for i in range(len(ents)):         # each page goes with the last order at or before it; leading pages: first
                k = max([j for j, m in enumerate(mains) if m <= i], default=0)
                segs[k].append(i)
        out = []
        for n, (seg, mi) in enumerate(zip(segs, mains)):
            page, part, fields, flags, doc_id, extractor, npages, secs, extra = ents[mi]
            others = [i for i in seg if i != mi]
            if others:
                merged = td / f"doc-{n + 1:02d}.pdf"
                r = _sp.run(["pdfunite", str(part)] + [str(ents[i][1]) for i in others] + [str(merged)],
                            capture_output=True, text=True, timeout=300)
                if r.returncode != 0 or not merged.exists():
                    raise RuntimeError(f"pdfunite failed: {r.stderr[:200]}")
                fields = dict(fields)
                sup = [summary(ents[i]) for i in others]
                fields["supporting"] = sup
                fields["supporting_text"] = "; ".join(
                    f"p{x['page']} {str(x['kind']).replace('_', ' ')}" + (f" {x.get('invoice_number') or x.get('order_number') or ''}".rstrip())
                    for x in sup)[:250]
                fields["pages"] = 1 + len(others)
                fields["source_pages"] = ", ".join(str(ents[i][0]) for i in [mi] + others)
                fields["content_hash"] = sha256(merged).upper()
                part = merged
                extra = dict(extra, grouped_pages=[ents[i][0] for i in [mi] + others])
                secs += sum(ents[i][7] for i in others)
                log.info("%s %s: p%s %s + supporting pages %s -> one document", folder, src.name, page,
                         fields.get("order_number") or kind(ents[mi]), ", ".join(str(ents[i][0]) for i in others))
            extra = {k: v for k, v in extra.items() if not k.startswith("_")}
            out.append((page, part, fields, flags, doc_id, extractor, npages, secs, extra))
        return out

    def output_stem(self, doc_type, fields, src, page):
        if fields.get("library_doc_type") == "Proof of Payment" and fields.get("account_name"):
            d = str(fields.get("date") or "")
            dd = "-".join(reversed(d.split("-"))) if re.fullmatch(r"\d{4}-\d\d-\d\d", d) else ""
            name = "-".join(x for x in (str(fields["account_name"]).upper(), "POP", dd) if x)
            return re.sub(r'\s+', " ", re.sub(r'[\\/:*?"<>|#%]+', " ", name)).strip(" .")[:150]
        if doc_type == "ws" and ws_reader:
            return safe_name(ws_reader.library_name(fields, src.name, "")[:150]) or safe_name(src.stem)
        if doc_type == "test_cert":       # keeps its own name (the library item's name in the backfill), Windows-safe
            real = tc_reader and next((x for x in re.split(r"\s*[;,]\s*", str(fields.get("cert_no") or ""))
                                       if tc_reader._cert_key(x)), None)       # 500: a batch's cert, by its number; 516: not its L/C no.
            stem = src.stem + ((f" - {real}" if real else f" p{page}") if page else "")
            return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#%]+', " ", stem)).strip(" .")[:150] or "document"
        if doc_type == "jt" and fields.get("jt_number"):
            n = str(fields["jt_number"])
            stem = f"JT{n}" if n.isdigit() else n              # Ronin: DF16593, as the AR - Ronin JTs library names them
        elif doc_type == "po" and po_reader and "form_label" in fields:
            # trained PO reader: name the file the way the PO library names it (SUPPLIER-ORDER-dd-MM-yyyy)
            return po_reader.library_name(fields, src.name, "")[:150] or safe_name(src.stem)
        elif doc_type == "po" and fields.get("po_number"):
            stem = f"PO{fields['po_number']}"
        elif doc_type == "supplier_invoice" and fields.get("supplier_name") and fields.get("invoice_number"):
            stem = f"{fields['supplier_name']}_{fields['invoice_number']}"
        else:
            stem = src.stem + (f"_p{page}" if page else "")
        return safe_name(stem)

    def process(self, folder, src, gpu_ok=True):
        tcfg = self.types[folder]
        doc_type = tcfg["doc_type"]
        digest = sha256(src)
        row = self.db.execute("SELECT status, folder, outputs FROM processed WHERE sha256=?", (digest,)).fetchone()
        # a workshop sheet read earlier in another queue (e.g. Purchase Orders, as "not an order") may be read again here
        rerouted = bool(row) and row[1] != folder and "rerouted_from" in (
            read_json_file(self.root / "in" / folder / "_meta" / (src.name + ".json")) or {})
        reread = bool(row) and "reread_of" in (
            read_json_file(self.root / "in" / folder / "_meta" / (src.name + ".json")) or {})
        if row and row[0] == "ok" and not rerouted and not reread and not (doc_type == "ws" and row[1] != folder and "WorkshopSheets/" not in str(row[2])):
            log.info("duplicate, skipping: %s", src.name)
            move(src, self.root / "done" / folder / "duplicates")
            return

        meta_path = self.root / "in" / folder / "_meta" / (src.name + ".json")      # optional: {"source_folder": ...}
        meta = read_json_file(meta_path) or {}
        self._run = (self._run + 1) if self.last_folder == folder else 1
        self.last_folder = folder
        loaded_before = self.models_loaded(max_age=0) if gpu_ok and self.queue_model(folder) else []
        ahead_fut = self.take_ahead(src)          # presort started while the GPU read the file before (332)
        work = move(src, self.root / "work", f"{'cpu~' if self.lane == 'cpu' else ''}{folder}__{src.name}")
        self.status["current"] = {"file": src.name, "type": folder, "started": now_iso()}
        self.write_status(True)
        outputs = []
        try:
            with tempfile.TemporaryDirectory() as td:
                if doc_type == "delivery_note":
                    # one scanned batch -> one document per delivery note (CPU only), published by process_delivery_notes
                    self.process_delivery_notes(folder, src, work, digest, meta, tcfg, outputs)
                    parts = []
                elif doc_type == "customer_order":
                    # one scan -> one document per customer PO (30 Oct); GPU only for orders whose lines don't add up
                    self.process_customer_orders(folder, src, work, digest, meta, tcfg, outputs, gpu_ok)
                    parts = []
                elif doc_type == "release_note":
                    # one scanned batch -> one document per release note + its supporting pages (28 Oct)
                    while self.paused() and not _stop:
                        time.sleep(5)
                    if _stop:
                        raise OllamaDown("shutdown")
                    self.process_release_notes(folder, src, work, digest, meta, tcfg, outputs)
                    parts = []
                elif doc_type in ("ws", "test_cert"):
                    parts = []
                else:
                    parts = self.split(work, Path(td), tcfg)
                presort_futs = self.presort_prefetch(parts, meta, src) if doc_type == "po" and len(parts) > 1 else {}
                # one file = one document around its order form (Dino 29 Oct): pages grouped before publishing
                grouping = bool(tcfg.get("group_pages")) and doc_type == "po" and len(parts) > 1
                staged = []
                if doc_type == "ws":
                    if not ws_reader:
                        raise RuntimeError("ws_reader.py is missing beside worker.py")
                    staged += self.read_ws_pages(work, Path(td) / "ws", meta.get("original_name") or src.name, digest)
                if doc_type == "test_cert":
                    while self.paused() and not _stop:
                        time.sleep(5)
                    if _stop:
                        raise OllamaDown("shutdown")
                    staged += self.read_tc(folder, work, src, meta, digest, loaded_before, Path(td))
                for page, part in parts:
                    while self.paused() and not tcfg.get("cpu_first") and not _stop:
                        time.sleep(5)
                    if _stop:
                        raise OllamaDown("shutdown")         # puts the file back in the queue
                    t0 = time.time()
                    extra = {}
                    text = None
                    if tcfg.get("cpu_first"):
                        key = f"{digest}:{page}"
                        if key not in self.cpu_cache:
                            n = read_json_file(self.cpu_note(folder, src.name)) or {}
                            if n.get("key") == key:          # the CPU lane's pass (332): no second Tesseract run here
                                self.cpu_cache[key] = (n.get("fields"), n.get("reasons") or [], n.get("text") or "", n.get("text_src"))
                        if key not in self.cpu_cache:
                            try:
                                self.cpu_cache[key] = cpu_invoice.extract(
                                    part, self.suppliers, parse_date, tcfg.get("cpu", {}),
                                    int(tcfg.get("max_pages", self.cfg.get("max_pages_per_doc", 4))))
                            except Exception as e:
                                log.warning("CPU OCR failed on %s: %s", src.name, e)
                                self.cpu_cache[key] = (None, [f"cpu_error: {str(e)[:120]}"], "", None)
                        cpu_fields, reasons, text, text_src = self.cpu_cache[key]
                        extra["text_source"] = text_src
                        if not reasons:
                            raw, npages, extractor = cpu_fields, None, "cpu"
                        else:
                            if not gpu_ok:
                                raise GpuUnavailable(", ".join(reasons))
                            extra["cpu_rejected"] = reasons
                            hint = (text, text_src) if text_src == "tesseract" else None
                            raw, npages = extract_builtin(doc_type, part, self.cfg, tcfg, hint)
                            extractor = "gpu_after_cpu"
                    elif tcfg.get("command"):
                        raw, npages = extract_command(doc_type, part, self.cfg, tcfg)
                        extractor = "command"
                    elif doc_type == "jt" and jt_reader and tcfg.get("jt_reader", True) is not False:
                        jc = self.jt_settings(folder)
                        try:
                            ans, npages, _ = self.read_page(lambda fs: jt_reader.read(
                                part, self.cfg["ollama_url"], jc["model"], fs, int(jc["px"]),
                                int(self.cfg.get("ollama_timeout", 900)), self.cfg.get("keep_alive", "15m"),
                                prompt=jc.get("prompt")), jc["fewshot_dir"], extra)
                            raw = jt_reader.to_worker(ans, jc.get("number_prefix"))
                        except PageReadFailed as e:
                            log.warning("%s %s%s could not be read (%s): held, the file's other pages are kept",
                                        folder, src.name, f" p{page}" if page else "", e)
                            ans, npages = {}, 1
                            raw = {"items": [], "read_error": str(e)}
                            extra["read_error"] = str(e)
                        if tcfg.get("company"):
                            raw["company"] = tcfg["company"]
                        extractor = "jt_v1"
                        extra["jt_model"] = jc["model"]
                        extra["jt_answer"] = ans                # the reader's own answer (training format)
                    elif doc_type == "pop" and pop_reader:
                        raw, flags_pop, npages = self.read_pop(part, tcfg)
                        extractor = "pop"
                    elif doc_type == "po" and po_reader and tcfg.get("po_reader", True) is not False:
                        # proof-of-payment pages inside the file: PoP-only files become their own document (Proof of
                        # Payments library); a PO with PoP pages stays one file, Document Type gets "Proof of Payment" too
                        presort_fut = presort_futs.get(part) or (ahead_fut if len(parts) == 1 else None) \
                            or self.presort_start(part, meta, src)
                        ptext = self.presort_collect(presort_fut, extra)   # waits for the CPU verdict (started earlier for split pages)
                        ps = extra.get("presort") or {}
                        if (tcfg.get("presort_gate", True) and ps.get("strength") == "sure" and ps.get("dest")
                                and ps["dest"] != folder and ps["dest"] in self.types):
                            if grouping:          # decided with the rest of the file: supporting page or the whole file
                                staged.append((page, part, {"content_hash": sha256(part).upper()}, [], f"{digest[:16]}-p{page:02d}",
                                               "presort", 0, time.time() - t0, dict(extra, _divert=ps)))
                                continue
                            outputs.append(self.presort_route(part, page, ps, meta, src, folder))
                            continue
                        if page == parts[-1][0]:
                            self.presort_ahead(folder, src)      # the next order's Tesseract runs while the GPU reads this one
                        pop_pages, pop_texts, npdf = self.payment_pages(part, tcfg)
                        first = next((i for i in range(1, npdf + 1) if i not in pop_pages), None)
                        # "proof_of_payment": "keep" -> payment confirmations stay in this library as Document Type
                        # "Proof of Payment" (order number, date from the scan as usual) instead of the PoP library
                        keep_pop = str(tcfg.get("proof_of_payment") or "").lower() == "keep"
                        all_pop = first is None
                        if keep_pop and all_pop:
                            first = 1
                        # the reader trained on the PO training batch (same prompt, worked examples and tidy-up)
                        pc = dict(tcfg.get("po_reader")) if isinstance(tcfg.get("po_reader"), dict) else {}
                        act = read_json_file(self.root / "training" / "active.json") or {}   # set by a training job
                        pc.update({k: act[k] for k in ("model", "fewshot_dir", "px") if act.get(k)})
                        pmodel = pc.get("model", "qwen2.5vl:7b")
                        try:
                            if first is None:            # every page is a payment confirmation: no order to read
                                raw, npages = {}, 0
                            else:
                                raw, npages, _ = self.read_page(lambda fs: po_reader.read(
                                    part, self.cfg["ollama_url"], pmodel, fs,
                                    self.order_pages(first, npdf, pop_pages, int(pc.get("pages", 2))), int(pc.get("px", 1600)),
                                    int(self.cfg.get("ollama_timeout", 1200)), self.cfg.get("keep_alive", "15m"),
                                    first_page=first, source_folder=meta.get("source_folder"), source_pdf=work,
                                    source_modified=meta.get("source_modified"), source_sha256=digest),
                                    pc.get("fewshot_dir", str(Path(__file__).resolve().with_name("po_fewshot"))), extra)
                        except PageReadFailed as e:
                            log.warning("%s %s%s could not be read (%s): held, the file's other pages are kept",
                                        folder, src.name, f" p{page}" if page else "", e)
                            raw, npages = {}, 1
                            extra["read_error"] = str(e)
                        extractor = "po_v2"
                        extra["po_model"] = pmodel
                        if getattr(po_reader, "LAST_TIMING", None) and not extra.get("read_error"):
                            extra["ollama"] = dict(po_reader.LAST_TIMING)     # prompt_eval / eval tokens and ms (review F22)
                        extra["po_examples"] = len(po_reader.fewshot_msgs(
                            pc.get("fewshot_dir", str(Path(__file__).resolve().with_name("po_fewshot"))))) // 2
                        if not extra["po_examples"]:
                            log.warning("PO reader ran WITHOUT its worked examples: po_fewshot folder missing or empty")
                    else:
                        raw, npages = extract_builtin(doc_type, part, self.cfg, tcfg)
                        extractor = "builtin"
                    if extractor == "po_v2":
                        fields, flags = po_reader.to_fields(raw)
                        if extra.get("read_error"):
                            fields["read_error"] = extra["read_error"]
                        if keep_pop and pop_reader:
                            if not pop_pages and npdf == 1 and "not_an_order" in flags:
                                # the presort already OCR'd this page (review F7): no second render + Tesseract
                                pop_texts = [ptext] if (ptext or "").strip() else (pop_reader.ocr_pages(part, 1, upright=True) or [""])
                                if pop_reader.is_payment_confirmation(pop_texts[0]):
                                    pop_pages = [1]
                                    all_pop = True
                            if pop_pages:
                                fields, flags = self.keep_pop_fields(fields, flags, pop_pages, pop_texts, all_pop, meta)
                        if first is not None and "not_an_order" in flags and self.ws_route_ok() and not extra.get("read_error"):
                            is_ws, ws_score, ws_why = ws_reader.looks_like_ws(part, meta.get("original_name") or src.name,
                                                                              text=ptext if (ptext or "").strip() else None, po_answer=raw)
                            if is_ws and grouping:
                                extra["_ws"] = ws_why            # decided with the rest of the file (one file = one document)
                                is_ws = False
                            if is_ws:
                                log.info("%s %s is a workshop sheet (%s): read as one, for Workshop Sheets", folder, src.name, ws_why)
                                staged += self.read_ws_pages(part, Path(td) / f"ws{page or 0}", meta.get("original_name") or src.name,
                                                             digest, routed_from=folder, why=ws_why)
                                continue
                        whole_pop = first is None or (npdf == 1 and raw.get("doc_type") == "proof_of_payment"
                                                      and pop_reader and pop_reader.is_payment_page(
                                                          (pop_texts or ([ptext] if (ptext or "").strip() else None)
                                                           or pop_reader.ocr_pages(part, 1) or [""])[0]))
                        if keep_pop:
                            pass                         # handled above: stays here as Proof of Payment
                        elif whole_pop and pop_reader and "ProofOfPayments" in self.types:
                            fields, flags, npages = self.read_pop(part, tcfg)
                            extractor = "pop"
                            extra["doc_type"] = "pop"
                        elif pop_pages:
                            base = [] if first is None else (fields.get("library_doc_type") or "Purchase Order").split(";")
                            fields["library_doc_type"] = ";".join(dict.fromkeys(base + ["Proof of Payment"]))
                            fields["payment_pages"] = pop_pages
                            pay = pop_reader.parse_standard_bank(pop_texts[pop_pages[0] - 1]) if pop_texts else {}
                            if pay:
                                fields["payment_ocr"] = pay  # Tesseract only (unchecked; it misreads 1 as 4): who / how much / when
                        if extractor == "po_v2" or extractor == "pop":
                            fields["content_hash"] = sha256(part).upper()    # = the library's Content Hash (SHA-256 of the file)
                    elif extractor == "pop":
                        fields, flags = raw, flags_pop
                        fields["content_hash"] = sha256(part).upper()
                    else:
                        fields, flags = normalise(doc_type, raw, self.cfg, tcfg.get("cpu", {}).get("own_vat_numbers", ()))
                        fields.setdefault("content_hash", sha256(part).upper())   # the library's Content Hash (dedupe, links)
                    if extractor != "cpu" and doc_type == "supplier_invoice":
                        fields, flags = self.fix_invoice_from_text(fields, flags, part, text, tcfg, extra)
                    if extractor != "cpu" and doc_type == "supplier_invoice" and self.suppliers \
                            and not {"missing_supplier_name", "totals_mismatch"} & set(flags):
                        self.suppliers.learn(fields.get("supplier_vat_no"), fields.get("supplier_name"))
                    doc_id = f"{digest[:16]}" + (f"-p{page:02d}" if page else "")
                    if extractor not in ("cpu", "dn_cpu"):
                        extra["models_loaded_before"] = loaded_before
                    staged.append((page, part, fields, flags, doc_id, extractor, npages, time.time() - t0, extra))
                    tm = extra.get("ollama") or {}
                    log.info("%s %s%s %.1fs via %s%s%s flags=%s", folder, src.name, f" p{page}" if page else "",
                             time.time() - t0, extractor,
                             f" (CPU rejected: {', '.join(extra['cpu_rejected'])})" if extra.get("cpu_rejected") else "",
                             f" [prompt {tm.get('prompt_tokens')} tok {tm.get('prompt_eval_ms')} ms, answer {tm.get('eval_tokens')} tok {tm.get('eval_ms')} ms]" if tm else "",
                             flags)

                if meta.get("supporting_of"):     # a supporting page read for its data only (supporting.py): not published
                    self.save_supporting(staged, folder, meta)
                    staged = []
                if grouping and staged:
                    staged = self.group_pages(staged, folder, src, work, meta, digest, Path(td), outputs)
                # all pages extracted: now publish to out/ (pdf first, json last)
                for page, part, fields, flags, doc_id, extractor, npages, secs, extra in staged:
                    eff_type = extra.get("doc_type", doc_type)
                    stem = self.output_stem(eff_type, fields, src, page)
                    dest = self.route(folder, eff_type, fields)      # Polymer (PL) orders -> PolymerOrders, PoPs -> ProofOfPayments
                    hold_reason = self.hold_reason(dest, eff_type, fields, flags)
                    out_dir = self.root / "out" / dest / ("hold" if hold_reason else "")
                    out_dir.mkdir(parents=True, exist_ok=True)
                    pdf_dst = unique_path(out_dir, stem, part.suffix.lower())
                    rel = f"hold/{pdf_dst.name}" if hold_reason else pdf_dst.name
                    outputs.append(rel if dest == folder else f"{dest}/{rel}")
                    if hold_reason:
                        flags = [f for f in flags if f != "held"] + ["held: " + hold_reason]
                    shutil.copy2(part, pdf_dst.with_name(pdf_dst.name + ".tmp"))
                    os.replace(pdf_dst.with_name(pdf_dst.name + ".tmp"), pdf_dst)
                    sidecar = {
                        "schema_version": 1,
                        "doc_id": doc_id,
                        "doc_type": eff_type,
                        "folder": dest,
                        **({"queued_as": folder} if dest != folder else {}),
                        "file": pdf_dst.name,
                        "source_file": meta.get("original_name") or src.name,
                        **({"source_folder": meta["source_folder"]} if meta.get("source_folder") else {}),
                        **({"source_modified": meta["source_modified"]} if meta.get("source_modified") else {}),
                        "source_sha256": digest,
                        "source_page": page,
                        "pages_read": npages,
                        "processed_at": now_iso(),
                        "seconds": round(secs, 1),
                        "model": tcfg.get("model", self.cfg.get("model")),
                        "extractor": extractor,
                        **({"hold_reason": hold_reason} if hold_reason else {}),
                        **extra,
                        "needs_review": bool(flags),
                        "flags": flags,
                        "fields": fields,
                    }
                    if extractor == "cpu":
                        sidecar["model"] = "tesseract+rules" if extra.get("text_source") == "tesseract" else "text-layer+rules"
                    elif extractor == "ws":
                        sidecar["model"] = sidecar.pop("ws_model", sidecar.get("model"))
                        sidecar["reader"] = ("text layer + rules (CAD sheet)" if extra.get("ws_how") == "text_layer" else
                                             "ws_reader (prompt + %d worked examples)" % extra.get("ws_examples", 0))
                    elif extractor == "tc":
                        sidecar["model"] = sidecar.pop("tc_model", sidecar.get("model"))
                        sidecar["reader"] = "tc_reader (page by page, no worked examples)"
                    elif extractor == "jt_v1":
                        sidecar["model"] = sidecar.pop("jt_model", sidecar.get("model"))
                        sidecar["reader"] = "jt_reader (review-set prompt + worked examples)"
                    elif extractor == "po_v2":
                        sidecar["model"] = sidecar.pop("po_model", sidecar.get("model"))
                        sidecar["reader"] = "po_ocr_v2 (trained prompt + %d worked examples)" % extra.get("po_examples", 0)
                    atomic_write_json(pdf_dst.with_suffix(".json"), sidecar)
                    self.db.execute("INSERT INTO extractions VALUES(?,?,?,?,?)",
                                    (now_iso(), folder, src.name, extractor, json.dumps(extra.get("cpu_rejected", []))))
            move(work, self.root / "done" / folder / datetime.now().strftime("%Y-%m"), src.name)
            meta_path.unlink(missing_ok=True)
            self.cpu_note(folder, src.name).unlink(missing_ok=True)
            self.db.execute("INSERT OR REPLACE INTO processed VALUES(?,?,?,?,?,?)",
                            (digest, folder, src.name, "ok", json.dumps(outputs), now_iso()))
            self.db.commit()
            self.status["last_done"] = {"file": src.name, "outputs": outputs, "at": now_iso()}
        except GpuUnavailable as e:
            if self.lane == "cpu":
                self.write_cpu_note(folder, src.name, digest)
                log.info("%s: CPU pass rejected (%s); queued for the GPU worker with its OCR text", src.name, e)
            else:
                log.info("%s needs the GPU (CPU: %s); waiting while other CPU work continues", src.name, e)
            back = move(work, self.root / "in" / folder, src.name)
            self.deferred.add(back)
            raise
        except OllamaDown as e:
            log.warning("Ollama unavailable (%s); %s goes back in the queue", e, src.name)
            move(work, self.root / "in" / folder, src.name)
            raise
        except Exception as e:
            if _stop:      # interrupted by shutdown, not a bad file: put it back in the queue
                log.warning("interrupted, re-queuing %s", src.name)
                for name in outputs:
                    for p in (self.out_path(folder, name), self.out_path(folder, name).with_suffix(".json")):
                        p.unlink(missing_ok=True)
                move(work, self.root / "in" / folder, src.name)
                return
            log.exception("failed: %s", src.name)
            # remove any half-published outputs from this source so the PC never syncs a partial set
            for name in outputs:
                for p in (self.out_path(folder, name), self.out_path(folder, name).with_suffix(".json")):
                    p.unlink(missing_ok=True)
            self.fail(folder, work, f"{type(e).__name__}: {e}", src.name)
            self.db.execute("INSERT OR REPLACE INTO processed VALUES(?,?,?,?,?,?)",
                            (digest, folder, src.name, "failed", json.dumps({"error": str(e)[:500]}), now_iso()))
            self.db.commit()
        finally:
            self.status["current"] = None
            if not (self.root / "in" / folder / src.name).exists():     # finished (ok or failed): drop cached CPU results
                for k in [k for k in self.cpu_cache if k.startswith(digest)]:
                    self.cpu_cache.pop(k, None)

    # ----------------------------------------------------------------------- delivery notes
    def process_delivery_notes(self, folder, src, work, digest, meta, tcfg, outs):
        """Split the batch on the barcode, look each delivery note up in Sage and publish it to out/<folder>/, or to
        out/<folder>/hold/ when it isn't a delivery note or isn't in Sage yet. Each output is added to outs as it is
        published, so a failure part-way removes the ones already written."""
        if not dn_reader:
            raise RuntimeError("dn_reader.py is missing beside worker.py")
        t0 = time.time()
        prep, docs, tmp = dn_reader.read(work, self.root, tcfg.get("dn", {}), self.dn_sage, log, name=src.name)
        try:
            for d in docs:
                held = bool(d["hold"])
                out_dir = self.root / "out" / folder / ("hold" if held else "")
                out_dir.mkdir(parents=True, exist_ok=True)
                pdf_dst = unique_path(out_dir, safe_name(d["stem"], 120), ".pdf")
                outs.append(f"{folder}/hold/{pdf_dst.name}" if held else f"{folder}/{pdf_dst.name}")
                shutil.copy2(d["pdf"], pdf_dst.with_name(pdf_dst.name + ".tmp"))
                os.replace(pdf_dst.with_name(pdf_dst.name + ".tmp"), pdf_dst)
                f = d["fields"]
                sidecar = {
                    "schema_version": 1,
                    "doc_id": f"{digest[:16]}-p{d['pages'][0]:03d}",
                    "doc_type": "delivery_note" if f.get("dn_number") else "other",
                    "folder": folder,
                    "file": pdf_dst.name,
                    "source_file": meta.get("original_name") or src.name,
                    **({"source_folder": meta["source_folder"]} if meta.get("source_folder") else {}),
                    "source_sha256": digest,
                    "source_page": d["pages"][0],
                    "source_pages": d["pages"],
                    "pages_read": len(d["pages"]),
                    "processed_at": now_iso(),
                    "seconds": round(time.time() - t0, 1),
                    "model": "zbar+tesseract" if prep == "ocrmypdf" else "zbar",
                    "extractor": "dn_cpu",
                    "page_prep": prep,
                    **({"hold_reason": d["hold"]} if held else {}),
                    "needs_review": bool(d["flags"]),
                    "flags": d["flags"],
                    "fields": f,
                }
                atomic_write_json(pdf_dst.with_suffix(".json"), sidecar)
                self.db.execute("INSERT INTO extractions VALUES(?,?,?,?,?)",
                                (now_iso(), folder, src.name, "dn_cpu", json.dumps(d["flags"])))
                log.info("%s %s p%s -> %s%s flags=%s", folder, src.name, ",".join(map(str, d["pages"])), pdf_dst.name,
                         f" [held: {d['hold']}]" if held else "", d["flags"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def process_customer_orders(self, folder, src, work, digest, meta, tcfg, outs, gpu_ok):
        """Customer POs (co_reader): OCR + job sticker + rules on the CPU; orders whose lines don't add up are read
        with the vision model. In the CPU lane (or without the GPU) such a file gets a note with its OCR text and the
        upright PDF and goes back to the queue for the GPU worker. One PDF + sidecar per customer PO in out/<folder>/,
        or out/<folder>/hold/ when a person must look (paper problems, lines, an unsettled account)."""
        if not (co_reader and self.co_lk):
            raise RuntimeError("co_reader.py is missing beside worker.py")
        t0 = time.time()
        c = dict(co_reader.DEFAULTS, **(tcfg.get("co_reader") or {}))
        c.setdefault("ollama_url", self.cfg["ollama_url"])
        c["ollama_url"], c["num_ctx"] = self.cfg["ollama_url"], self.cfg.get("num_ctx", c["num_ctx"])
        c["keep_alive"] = self.cfg.get("keep_alive", c["keep_alive"])
        c["ollama_timeout"] = max(int(self.cfg.get("ollama_timeout", 900)), 900)
        name = meta.get("original_name") or src.name
        note_p = self.cpu_note(folder, src.name)
        up_note = note_p.with_name(src.name + ".cpu.up.pdf")
        note = read_json_file(note_p) or {}
        tmp = Path(tempfile.mkdtemp(prefix="co-"))
        try:
            up = tmp / "upright.pdf"
            if note.get("co_ocr") and up_note.is_file():
                shutil.copy2(up_note, up)
                texts, codes = note["co_ocr"]["texts"], note["co_ocr"]["barcodes"]
            else:
                u, texts, codes, otmp = co_reader.ocr(work, c, log)
                shutil.move(str(u), up)
                shutil.rmtree(otmp, ignore_errors=True)
            orders = co_reader.analyse(texts, codes, self.co_lk, c, name=name)
            if any(o["needs_gpu"] for o in orders):
                if self.lane == "cpu" or not gpu_ok:
                    note_p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(up, up_note)
                    atomic_write_json(note_p, {"co_ocr": {"texts": texts, "barcodes": codes}, "at": now_iso(),
                                               "reasons": ["lines do not add up on the CPU"]})
                    raise GpuUnavailable("customer order lines need the vision model")
                for o in orders:
                    if not o["needs_gpu"]:
                        continue
                    sub = co_reader.split_pdf(up, o["pages"], tmp / f"g{o['pages'][0]:03d}.pdf", len(texts))
                    try:
                        ans = co_reader.gpu_read(sub, [texts[p - 1] for p in o["pages"]], c, log)
                    except (urllib.error.URLError, ConnectionError) as e:
                        if not ollama_up(self.cfg):
                            raise OllamaDown(str(e))
                        raise
                    co_reader.apply_gpu(o, ans, self.co_lk, c)
                    o["gpu_secs"] = ans.get("_secs")
            for o in orders:
                stem = co_reader.finish(o, self.co_lk)
                held = bool(o["hold"])
                out_dir = self.root / "out" / folder / ("hold" if held else "")
                out_dir.mkdir(parents=True, exist_ok=True)
                part = co_reader.split_pdf(up, o["pages"], tmp / f"o{o['pages'][0]:03d}.pdf", len(texts))
                pdf_dst = unique_path(out_dir, safe_name(stem, 150), ".pdf")
                outs.append(f"hold/{pdf_dst.name}" if held else pdf_dst.name)
                shutil.copy2(part, pdf_dst.with_name(pdf_dst.name + ".tmp"))
                os.replace(pdf_dst.with_name(pdf_dst.name + ".tmp"), pdf_dst)
                f = o["fields"]
                f["content_hash"] = sha256(part).upper()
                gpu = o.get("lines_from") == "gpu"
                sidecar = {
                    "schema_version": 1,
                    "doc_id": f"{digest[:16]}-p{o['pages'][0]:03d}",
                    "doc_type": "customer_order",
                    "folder": folder,
                    "file": pdf_dst.name,
                    "source_file": name,
                    **({"source_folder": meta["source_folder"]} if meta.get("source_folder") else {}),
                    "source_sha256": digest,
                    "source_page": o["pages"][0],
                    "source_pages": o["pages"],
                    "pages_read": len(o["pages"]),
                    "processed_at": now_iso(),
                    "seconds": round(time.time() - t0, 1),
                    "model": c["gpu_model"] if gpu else "zbar+tesseract+rules",
                    "extractor": "co_gpu_after_cpu" if gpu else "co_cpu",
                    "reader": "co_reader (sticker barcode, VAT -> account, lines CPU then vision model)",
                    **({"hold_reason": o["hold"]} if held else {}),
                    "needs_review": bool(o["flags"]),
                    "flags": o["flags"],
                    "fields": f,
                }
                atomic_write_json(pdf_dst.with_suffix(".json"), sidecar)
                self.db.execute("INSERT INTO extractions VALUES(?,?,?,?,?)",
                                (now_iso(), folder, src.name, sidecar["extractor"], json.dumps(o["flags"])))
                log.info("%s %s p%s -> %s%s account=%s (%s) flags=%s", folder, src.name, ",".join(map(str, o["pages"])),
                         pdf_dst.name, f" [held: {o['hold']}]" if held else "", f.get("customer_account"),
                         f.get("account_how"), o["flags"])
            up_note.unlink(missing_ok=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def process_release_notes(self, folder, src, work, digest, meta, tcfg, outs):
        """Split the batch at each release note (rn_reader), read the release-note pages with the vision model and
        publish one PDF + sidecar per release note to out/<folder>/; pages before the first release note go to hold."""
        if not rn_reader:
            raise RuntimeError("rn_reader.py is missing beside worker.py")
        t0 = time.time()
        c = dict(tcfg.get("rn_reader") or {})
        c.setdefault("ollama_url", self.cfg["ollama_url"])
        c.setdefault("num_ctx", self.cfg.get("num_ctx", 49152))
        c.setdefault("keep_alive", self.cfg.get("keep_alive", "15m"))
        name = meta.get("original_name") or src.name
        prep, docs, tmp = rn_reader.read(work, c, log, name=name)
        try:
            for d in docs:
                held = bool(d["hold"])
                out_dir = self.root / "out" / folder / ("hold" if held else "")
                out_dir.mkdir(parents=True, exist_ok=True)
                pdf_dst = unique_path(out_dir, safe_name(d["stem"], 120), ".pdf")
                outs.append(f"hold/{pdf_dst.name}" if held else pdf_dst.name)
                shutil.copy2(d["pdf"], pdf_dst.with_name(pdf_dst.name + ".tmp"))
                os.replace(pdf_dst.with_name(pdf_dst.name + ".tmp"), pdf_dst)
                f = d["fields"]
                sidecar = {
                    "schema_version": 1,
                    "doc_id": f"{digest[:16]}-p{d['pages'][0]:03d}",
                    "doc_type": "release_note",
                    "folder": folder,
                    "file": pdf_dst.name,
                    "source_file": name,
                    **({"source_folder": meta["source_folder"]} if meta.get("source_folder") else {}),
                    "source_sha256": digest,
                    "source_page": d["pages"][0],
                    "source_pages": d["pages"],
                    "blank_pages_dropped": d["dropped"],
                    "pages_read": len(d["pages"]),
                    "processed_at": now_iso(),
                    "seconds": round(time.time() - t0, 1),
                    "model": c.get("model", rn_reader.DEFAULTS["model"]),
                    "extractor": "rn",
                    "reader": "rn_reader (text layer split + vision model per release-note page)",
                    "page_prep": prep,
                    **({"hold_reason": d["hold"]} if held else {}),
                    "needs_review": bool(d["flags"]),
                    "flags": d["flags"],
                    "fields": f,
                }
                atomic_write_json(pdf_dst.with_suffix(".json"), sidecar)
                self.db.execute("INSERT INTO extractions VALUES(?,?,?,?,?)",
                                (now_iso(), folder, src.name, "rn", json.dumps(d["flags"])))
                log.info("%s %s p%s -> %s%s flags=%s", folder, src.name, f.get("source_pages"), pdf_dst.name,
                         f" [held: {d['hold']}]" if held else "", d["flags"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def recheck_held_dns(self):
        """When the Sage copy changes, held delivery notes that weren't in Sage are looked up again and released."""
        if not self.dn_sage:
            return
        try:
            self.dn_sage.refresh()
        except Exception as e:
            log.warning("sage invoices reload failed: %s", e)
            return
        if self.dn_sage.mtime == self.dn_checked:
            return
        self.dn_checked = self.dn_sage.mtime
        if self.dn_sage.loaded:
            log.info("Sage invoices loaded: %d numbers from %s", len(self.dn_sage.idx), self.dn_sage.path.name)
        for folder, t in self.types.items():
            if t.get("doc_type") != "delivery_note":
                continue
            hold = self.root / "out" / folder / "hold"
            if not hold.is_dir():
                continue
            for j in sorted(hold.glob("*.json")):
                sc = read_json_file(j)
                if not sc or sc.get("hold_reason") not in ("not_in_sage", "no_sage_copy"):
                    continue
                new = dn_reader.recheck(sc, self.dn_sage, t.get("dn", {}))
                if not new:
                    continue
                pdf = hold / str(new.get("file") or "")
                if not pdf.is_file():
                    continue
                try:
                    with sidecar_lock.locked(j, "worker", wait=5):          # review F10: not while the dashboard edits it
                        new["rechecked_at"] = now_iso()
                        atomic_write_json(j, new)
                        dst = unique_path(self.root / "out" / folder, pdf.stem, pdf.suffix)
                        os.replace(pdf, dst)                          # PDF first, JSON last: the PC never sees half a pair
                        if dst.name != pdf.name:
                            new["file"] = dst.name
                            atomic_write_json(j, new)
                        os.replace(j, dst.with_suffix(".json"))
                except sidecar_lock.Locked as e:
                    log.info("%s: %s; rechecked next round", j.name, e)
                    continue
                log.info("%s %s found in Sage now: released from hold", folder, dst.name)

    def loop(self):
        poll = float(self.cfg.get("poll_seconds", 5))
        log.info("watching %s for %s", self.root / "in", ", ".join(self.types))
        while not _stop:
            self.reload_suppliers()
            if (self.lane == "cpu") == bool(self.cfg.get("cpu_lane")):
                self.recheck_held_dns()               # delivery notes: done by the lane that reads them (332)
            up = False if self.lane == "cpu" else ollama_up(self.cfg)
            gpu_ok = up and not self.paused()        # PAUSE stops GPU work; CPU-first invoices keep going
            if gpu_ok and self.deferred:
                self.deferred.clear()
            nxt = self.next_ready(gpu_ok)
            if not nxt:
                self.write_status(up)
                if time.time() - getattr(self, "_last_ckpt", 0) > 3600:       # review F19: keep state.db-wal small
                    self._last_ckpt = time.time()
                    try:
                        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.Error as e:
                        log.warning("wal checkpoint: %s", e)
                time.sleep(poll)
                continue
            _, folder, path = nxt
            self.sizes.pop(path, None)
            try:
                self.process(folder, path, gpu_ok)
            except OllamaDown:
                time.sleep(30)
            except GpuUnavailable:
                pass
            self.write_status(up)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    cfg = load_config()
    lane = sys.argv[sys.argv.index("--lane") + 1] if "--lane" in sys.argv[:-1] else "all"
    if lane not in ("all", "cpu"):
        sys.exit("--lane all|cpu")
    if lane == "cpu" and not cfg.get("cpu_lane"):
        log.info("--lane cpu: cpu_lane is off in config.json; idle")
        while not _stop:
            time.sleep(30)
        return
    Worker(cfg, lane).loop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
mps_jobs.py - one rule for MPS job numbers and order allocations, shared by every reader and the dashboard.

Job numbers: 4-5 digits + a series letter. Sage has A, B, C, D and E jobs, each numbered 0001-9999 (E went on to
5 digits), so the same digits can belong to five jobs and only the job's own dates can pick the letter when the
writer left it off. The job list (<JOBS_DIR>/jobs_sage.csv from MPSBusiness rpt.vw_Jobs, jobs_jobfile.csv from the
Quotes site "MPS Sales - Job File", sage_invoices.csv) is read by job_index() and completion is done by complete_job().

The text after the slash on an order number (the "allocation" box next to the date) is one of:
  * a job number        - 4-5 digits, letter optional        -> job_number (allocation cleared)
  * an allocation word  - CONS, STOCK, MAINT, WORKSHOP, VEHICLES, KUSASA, POLYMER, RONIN, IAC, a site/customer name
  * payment terms       - "30 DAYS", "COD"                     -> payment_terms
  * an order method     - "EMAIL", "PHONE"                     -> order_method
classify_token() decides; apply_allocation() rewrites a read order's fields accordingly and records what it did.

Also here: find_registration() (South African vehicle registrations, for VEHICLES orders -> other_po_ref) and
check_order_date() (an order's date against the dates of the orders numbered around it, from orders_index.csv).
"""
import csv, datetime as _dt, difflib, glob, io, os, re, time as _time

JOBS_DIR = None                  # set by the worker / dashboard: the pipeline root (holds jobs_*.csv, orders_index.csv)
SERIES = "ABCDE"
E_SERIES_START = "2018-10-23"    # the current E series restarted at 00001E on this date

# a job number anywhere in text: digits + optional series letter, not glued to other letters/digits
JOB_ANY = re.compile(r"(?<![A-Z0-9])0*(\d{3,5})\s?\.?\s?([A-E])?(?![A-Z0-9])")
JOB_LETTERED = re.compile(r"(?<![A-Z0-9])0*(\d{3,5})\s?\.?\s?([A-E])(?![A-Z0-9])")
JOB_STRICT = re.compile(r"(?<![\w])(\d{4,5}[A-E])(?![\w])")      # for printed text (invoices): letter required

# ---------------------------------------------------------------- allocation vocabulary
ALLOCATIONS = {
    "CONS": "CONS", "CONSUMABLES": "CONS", "CONSUMABLE": "CONS",
    "STOCK": "STOCK", "STOCKS": "STOCK",
    "MAINT": "MAINT", "MAINTENANCE": "MAINT", "MAINTANANCE": "MAINT",
    "WORKSHOP": "WORKSHOP", "W/SHOP": "WORKSHOP", "WSHOP": "WORKSHOP",
    "VEHICLES": "VEHICLES", "VEHICLE": "VEHICLES", "VEHICALS": "VEHICLES", "VEHICELS": "VEHICLES",
    "KUSASA": "KUSASA", "POLYMER": "POLYMER", "POLYMERS": "POLYMER", "RONIN": "RONIN",
    "IAC": "IAC",
}
KNOWN = sorted(set(ALLOCATIONS.values()))
TERMS_RX = re.compile(r"^\s*(?:DISC\b.*|(?:\d+(?:[.,]\d+)?\s*%?\s*[/:\-]?\s*)?\d{1,3}\s*DAYS?\b.*|COD|CASH|C\.O\.D\.?|PRO ?FORMA|PREPAID|PRE-PAID)\s*$", re.I)
METHOD_RX = re.compile(r"^\s*(?:E-?MAIL(?:ED)?|SALES\s+E-?MAIL|PHONE|TEL(?:EPHONE)?|CONF(?:IRMED)?|FAX|WHATSAPP|VERBAL)\s*$", re.I)

# digit look-alikes in a mostly-numeric token (Melyssa's 4 reads as L, 1 as I/T, 0 as O, 2 as Z, 5 as S)
_DIGIT_FIX = str.maketrans({"L": "4", "I": "1", "O": "0", "D": "0", "Z": "2", "S": "5", "T": "1", "G": "6", "Q": "0", "B": "8"})


# ---------------------------------------------------------------- job list
_JOBS, _JOBS_KEY = None, None
_JOB_COVER = {}


def _day(s):
    s = str(s or "").strip()
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)", s) or re.match(r"(\d\d)/(\d\d)/(\d{4})", s)
    if not m:
        return None
    y, mo, d = (m.group(1), m.group(2), m.group(3)) if len(m.group(1)) == 4 else (m.group(3), m.group(2), m.group(1))
    try:
        return _dt.date(int(y), int(mo), int(d))
    except ValueError:
        return None


def norm_job(digits, letter):
    n = int(digits)
    return (f"{n:04d}" if n < 10000 else str(n)) + letter


def job_index():
    """{digits (int): {job: {"job", "first", "last", "customer", "name", "src"}}} from the job list files, reloaded
    when they change. Empty when there is no job list."""
    global _JOBS, _JOBS_KEY
    files = sorted(glob.glob(os.path.join(JOBS_DIR, "jobs_*.csv"))) if JOBS_DIR else []
    if JOBS_DIR and os.path.exists(os.path.join(JOBS_DIR, "sage_invoices.csv")):
        files.append(os.path.join(JOBS_DIR, "sage_invoices.csv"))
    key = tuple((f, os.path.getmtime(f)) for f in files)
    if _JOBS is not None and key == _JOBS_KEY:
        return _JOBS
    idx = {}

    def add(job, dates, customer, name, src):
        m = re.fullmatch(r"\s*0*(\d{1,5})\s*([A-Z])\s*", str(job or "").upper())
        if not m:
            return
        n, letter = int(m.group(1)), m.group(2)
        k = norm_job(n, letter)
        r = idx.setdefault(n, {}).setdefault(k, {"job": k, "first": None, "last": None, "customer": None, "name": None, "src": set()})
        for dd in (x for x in (_day(v) for v in dates) if x):
            r["first"] = min(r["first"], dd) if r["first"] else dd
            r["last"] = max(r["last"], dd) if r["last"] else dd
        r["customer"] = r["customer"] or (customer or "").strip() or None
        r["name"] = r["name"] or (name or "").strip() or None
        r["src"].add(src)

    for f in files:
        try:
            with open(f, encoding="utf-8-sig", errors="replace") as fh:
                rows = csv.DictReader(io.StringIO(fh.read().replace("\0", "")))
                base = os.path.basename(f).lower()
                for r in rows:
                    if base == "sage_invoices.csv":
                        add(r.get("JobNumber"), [r.get("DocDate")], r.get("CustomerName"), r.get("JobName"), "Sage invoices")
                    else:
                        add(r.get("JobNumber") or r.get("Job"),
                            [r.get(c) for c in ("FirstDocDate", "LastDocDate", "OrderDate", "CompletedDate")] or [],
                            r.get("Customer"), r.get("JobName") or r.get("Description"),
                            r.get("Source") or ("Job File" if "jobfile" in base else "Sage"))
                        if not (_day(r.get("FirstDocDate")) or _day(r.get("OrderDate"))):
                            add(r.get("JobNumber") or r.get("Job"), [r.get("CreatedDate")], None, None, "Sage")
        except OSError:
            continue
    cover = {}
    for jobs in idx.values():
        for r in jobs.values():
            if r["first"]:
                lt = r["job"][-1]
                cover[lt] = min(cover.get(lt, r["first"]), r["first"])
    _JOBS, _JOBS_KEY = idx, key
    _JOB_COVER.clear(); _JOB_COVER.update(cover)
    return idx


def _covered(letter, day):
    c = _JOB_COVER.get(letter)
    return bool(c) and (day is None or day >= c)


def _job_gap(r, day):
    """Days between the order date and the job's active period (0 = inside it, with slack either side)."""
    if not r["first"]:
        return 10 ** 6
    lo, hi = r["first"] - _dt.timedelta(days=60), r["last"] + _dt.timedelta(days=180)
    return 0 if lo <= day <= hi else min(abs((day - lo).days), abs((day - hi).days))


def job_desc(r):
    rng = ""
    if r["first"]:
        rng = r["first"].isoformat() if r["first"] == r["last"] else f"{r['first']:%Y-%m} to {r['last']:%Y-%m}"
    bits = [x for x in (r.get("customer"), rng) if x]
    src = " / ".join(sorted(r["src"]))
    return f"{r['job']} ({', '.join(bits)}; {src})" if bits else f"{r['job']} ({src})"


def _near_jobs(idx, digits, letter, day):
    """817 (review 23 Nov): known jobs of the same series ONE digit away (changed, missing or extra) from a number
    that is not in the job list, and active on the document's date. The caller only takes a single match."""
    d = str(int(digits))
    var = set()
    for i in range(len(d)):
        var.add(d[:i] + d[i + 1:])
        for c in "0123456789":
            if c != d[i]:
                var.add(d[:i] + c + d[i + 1:])
    for i in range(len(d) + 1):
        for c in "0123456789":
            var.add(d[:i] + c + d[i:])
    out = {}
    for v in var:
        if not 3 <= len(v) <= 5 or v[0] == "0" or v == d:
            continue
        r = idx.get(int(v), {}).get(norm_job(v, letter))
        if r and r["first"] and _job_gap(r, day) == 0:
            out[r["job"]] = r
    return list(out.values())


def complete_job(digits, letter, date_iso, chk, written=None):
    """The full job number for digits (+ letter when written) on a document dated date_iso. Adds a check line when
    the letter was filled in, the job isn't in the job list, or the date fits another letter better.
    Returns (job or None, job record or None)."""
    day = _day(date_iso)
    idx = job_index()
    n = int(digits)
    cands = list(idx.get(n, {}).values())
    if day:
        cands = [r for r in cands if not (r["first"] and (r["first"] - day).days > 365)
                 and not (r["job"].endswith("E") and day.isoformat() < E_SERIES_START and _job_gap(r, day) > 0)]
    shown = written or digits
    if letter:
        mine = idx.get(n, {}).get(norm_job(digits, letter))
        if letter == "E" and day and day.isoformat() < E_SERIES_START and not (mine and _job_gap(mine, day) == 0) and \
                (not _covered("D", day) or any(r["job"].endswith("D") and _job_gap(r, day) <= 365 for r in cands)):
            chk.append(f"job read as {norm_job(digits, letter)} but the order is dated {day.isoformat()}, before the E jobs "
                       f"started (Oct 2018): taken as {norm_job(digits, 'D')}")
            letter = "D"
        job = norm_job(digits, letter)
        if idx:
            mine = idx.get(n, {}).get(job)
            better = [r for r in cands if r["job"] != job and day and _job_gap(r, day) == 0]
            if not mine and day and not better and _covered(letter, day):
                near = _near_jobs(idx, digits, letter, day)
                if len(near) == 1:               # 32814E read as 52814E / 328141E: the one real job a digit away
                    chk.append(f"job {job} not in the job list: taken as {job_desc(near[0])}, one digit different - check")
                    return near[0]["job"], near[0]
            if not mine and (better or _covered(letter, day)):
                chk.append(f"job {job} not in the job list" + (f"; the order date fits {job_desc(better[0])}" if better else ""))
            elif mine and day and _job_gap(mine, day) > 365 and better:
                chk.append(f"job {job}: the order date fits {job_desc(better[0])} better")
        return job, (idx.get(n, {}).get(job) if idx else None)
    if cands:
        if day:
            cands.sort(key=lambda r: (_job_gap(r, day), abs((day - r["first"]).days) if r["first"] else 10 ** 6))
            best = cands[0]
            fit = [r for r in cands if _job_gap(r, day) == _job_gap(best, day)]
            if _job_gap(best, day) <= 365:
                note = f"job {shown} written without its letter: {job_desc(best)}"
                if len(fit) > 1:
                    note += "; also " + ", ".join(r["job"] for r in fit[1:]) + " - check"
                chk.append(note)
                return best["job"], best
        elif len(cands) == 1:
            chk.append(f"job {shown} written without its letter: {job_desc(cands[0])} (no order date to confirm)")
            return cands[0]["job"], cands[0]
    if len(digits.lstrip("0")) == 5:
        known = " (not in the job list)" if _covered("E", day) else ""
        chk.append(f"job {shown} written without its letter: {norm_job(digits, 'E')} (5-digit jobs are E){known}")
        return norm_job(digits, "E"), None
    if day and day.isoformat() < E_SERIES_START:
        known = " (not in the job list)" if _covered("D", day) else ""
        chk.append(f"job {shown} written without its letter: {norm_job(digits, 'D')} (orders before Oct 2018 are D jobs){known}")
        return norm_job(digits, "D"), None
    chk.append(f"job {shown} written without its letter and not matched to a job: add the letter")
    return None, None


# ---------------------------------------------------------------- the token after the slash
def fix_digits(tok):
    """'1076LE' -> '10764E', 'T0625' -> '10625', '30689.E' -> '30689E'. Only when the token is mostly digits and at
    most two characters need correcting; the trailing series letter is kept. Returns (fixed, changed)."""
    t = re.sub(r"[\s.\-_]", "", str(tok or "").upper())
    if not t:
        return t, False
    body, letter = (t[:-1], t[-1]) if t[-1] in SERIES and len(t) > 1 else (t, "")
    if letter == "" and body and body[-1] in "DE" and re.fullmatch(r"[0-9LIOZSTGQ]+", body[:-1] or "x"):
        body, letter = body[:-1], body[-1]
    digits = sum(c.isdigit() for c in body)
    if digits < 2 or not re.fullmatch(r"[0-9LIODZSBTGQ]{4,5}", body):
        return t, False
    fixed = body.translate(_DIGIT_FIX)
    if not fixed.isdigit():
        return t, False
    return fixed + letter, fixed != body


def snap_allocation(word):
    """A misread allocation word -> the known value it clearly is ('VUSASO' -> 'KUSASA'), or None."""
    k = re.sub(r"[^A-Z/]", "", str(word or "").upper())
    if len(k) < 3:
        return None
    if k in ALLOCATIONS:
        return ALLOCATIONS[k]
    scored = sorted(((difflib.SequenceMatcher(None, k, n).ratio(), n) for n in ALLOCATIONS), reverse=True)[:2]
    if scored and scored[0][0] >= 0.6 and (len(scored) < 2 or scored[0][0] - scored[1][0] >= 0.12):
        return ALLOCATIONS[scored[0][1]]
    return None


def classify_token(text, date_iso=None, order_digits=()):
    """What the text after the slash is. Returns a dict:
       kind: 'job' | 'allocation' | 'terms' | 'method' | 'empty'
       value: the normalised value (job number for 'job', allocation word for 'allocation'...)
       known: True when an allocation is on the fixed list; fixed: True when digits were corrected
       checks: list of check lines
    order_digits: digit strings that are the order's own numbers (never a job)."""
    raw = str(text or "").strip()
    out = {"kind": "empty", "value": None, "raw": raw or None, "known": False, "fixed": False, "checks": []}
    if not raw:
        return out
    up = re.sub(r"\s+", " ", raw.upper()).strip(" .,;:-")
    if TERMS_RX.match(up):
        out.update(kind="terms", value=up)
        return out
    if METHOD_RX.match(up):
        out.update(kind="method", value={"EMAILED": "EMAIL", "E-MAIL": "EMAIL", "E-MAILED": "EMAIL", "TELEPHONE": "PHONE", "TEL": "PHONE",
                                         "CONFIRMED": "CONF"}.get(up, up))
        return out
    # a job number: an unbroken run of 4-5 digits (letter optional), possibly with a stray prefix like "J" or "JOB"
    cand = re.sub(r"^(?:JOB\s*(?:NO\.?)?|J)\s*[:#]?\s*", "", up)
    fixed, changed = fix_digits(cand)
    m = re.fullmatch(r"0*(\d{4,5})([A-E])?", fixed)
    if m and m.group(1) not in set(order_digits) and not (m.group(2) is None and 1990 <= int(m.group(1)) <= 2099):
        chk = []
        if changed:
            # a corrected token must be a real job whose dates fit the order; otherwise it stays as read and is flagged
            day = _day(date_iso)
            recs = list(job_index().get(int(m.group(1)), {}).values())
            if m.group(2):
                recs = [r for r in recs if r["job"].endswith(m.group(2))]
            fits = [r for r in recs if day and _job_gap(r, day) <= 365] or ([r for r in recs] if not day else [])
            if fits:
                chk.append(f"allocation read as {raw}: taken as job {fixed}")
            else:
                out.update(kind="allocation", value=up, checks=[f"allocation {raw} looks like a job number but no job {fixed} fits the order date: check"])
                return out
        job, rec = complete_job(m.group(1), m.group(2), date_iso, chk, written=raw)
        if job:
            out.update(kind="job", value=job, fixed=changed, checks=chk, record=rec)
            return out
        out.update(kind="allocation", value=up, checks=chk)
        return out
    known = snap_allocation(up)
    if known:
        out.update(kind="allocation", value=known, known=True)
        if known != up and up not in ALLOCATIONS:
            out["checks"].append(f"allocation read as {raw}, taken as {known}")
        return out
    out.update(kind="allocation", value=up, known=False, checks=[f"allocation {raw} is not on the list: check"])
    return out


# ---------------------------------------------------------------- vehicle registrations
# Gauteng: "BD 84 MT GP", "VXH 264 GP", "ZMG820GP"; other provinces by suffix (L, MP, NW, FS, EC, NC, ZN);
# KwaZulu-Natal "ND 123-456" / "NPN 12345"; Western Cape "CA 123-456" / "CJ 12345"
_REG = re.compile(r"(?<![A-Z0-9])(?:"
                  r"([A-Z]{2}\s?\d{2}\s?[A-Z]{2}\s?(?:GP|L|MP|NW|FS|EC|NC|ZN))"
                  r"|([A-Z]{3}\s?\d{3}\s?(?:GP|L|MP|NW|FS|EC|NC|ZN))"
                  r"|([A-Z]{2,3}\s?\d{3}\s?\d{3}\s?(?:GP|L|MP|NW|FS|EC|NC))"
                  r"|(N[A-Z]{1,2}\s?\d{3}[\s\-]?\d{3})|(C[A-Z]{1,2}\s?\d{3}[\s\-]?\d{3})"
                  r")(?![A-Z0-9])")


def find_registration(*texts):
    """South African vehicle registrations in the given texts, normalised without spaces ('BD84MTGP'), in order."""
    seen, out = set(), []
    for t in texts:
        for m in _REG.finditer(str(t or "").upper()):
            r = re.sub(r"[\s\-]", "", m.group(0))
            if r not in seen:
                seen.add(r); out.append(r)
    return out


# ---------------------------------------------------------------- the order date against its neighbours
_ORDERS, _ORDERS_KEY = None, None


def orders_index():
    """{series: sorted [(number, date, supplier)]} from <JOBS_DIR>/orders_index.csv (written by the PC sync from the
    Purchase Orders and Polymer Orders libraries: order_number, date, supplier) plus the orders read on this box
    (sidecars in out/ and synced/), so a batch is checked against itself as well. Refreshed every 60 s."""
    global _ORDERS, _ORDERS_KEY
    if not JOBS_DIR:
        return {}
    p = os.path.join(JOBS_DIR, "orders_index.csv")
    key = int(_time.time() // 60)
    if _ORDERS is not None and key == _ORDERS_KEY:
        return _ORDERS
    seen, by = {}, {}

    def add(num, date, sup, src):
        m = re.fullmatch(r"(A|PL|DF|PO)(\d{3,6})", str(num or "").upper().replace(" ", ""))
        d = _day(date)
        if m and d and (m.group(1), int(m.group(2)), src) not in seen:
            seen[(m.group(1), int(m.group(2)), src)] = 1
            by.setdefault(m.group(1), []).append((int(m.group(2)), d, str(sup or "").upper()))

    if os.path.exists(p):
        with open(p, encoding="utf-8-sig", errors="replace") as fh:
            for r in csv.DictReader(fh):
                add(r.get("order_number"), r.get("date"), r.get("supplier"), "csv")
    import json as _json
    for pat in ("out/PurchaseOrders/*.json", "out/PurchaseOrders/hold/*.json", "out/PolymerOrders/*.json", "out/PolymerOrders/hold/*.json",
                "synced/PurchaseOrders/*/*.json", "synced/PolymerOrders/*/*.json"):
        for j in glob.glob(os.path.join(JOBS_DIR, pat)):
            try:
                with open(j, encoding="utf-8-sig") as fh:
                    side = _json.load(fh)
            except (OSError, ValueError):
                continue
            f = side.get("fields") or {}
            dt, _ = apply_scan_date(f.get("date"), scan_date(side.get("source_folder"), side.get("source_file"),
                                                            modified=side.get("source_modified"), sha256=side.get("source_sha256")))
            add(f.get("order_number"), dt, f.get("supplier"), "box")
    for s in by:
        by[s].sort()
    _ORDERS, _ORDERS_KEY = by, key
    return by


def _neighbours(series, number, span=60, k=8):
    L = orders_index().get(series) or []
    if not L:
        return []
    import bisect
    i = bisect.bisect_left(L, (number, _dt.date.min, ""))
    nb = [x for x in L[max(0, i - k):i] + L[i:i + k + 1] if x[0] != number and abs(x[0] - number) <= span]
    return nb


def folder_year(source_folder=None, source_file=None):
    """The year a scan was filed under, from the year folder in its scan path ("Melyssa\\Orders-MPS\\2024\\A\\...")."""
    m = re.search(r"(?:^|[\\/])(20\d\d)(?:[\\/]|$)", str(source_folder or "").strip())
    return int(m.group(1)) if m else None


_SCAN_TIMES, _SCAN_TIMES_KEY = {}, None


def scan_times():
    """{sha256: date} of the scans as they sit on the NAS (<JOBS_DIR>/scan_times.csv: sha256, modified), written from
    the feed logs; a scan file's modified time is when the scanner wrote it."""
    global _SCAN_TIMES, _SCAN_TIMES_KEY
    p = os.path.join(JOBS_DIR, "scan_times.csv") if JOBS_DIR else None
    if not p or not os.path.exists(p):
        return _SCAN_TIMES
    key = os.path.getmtime(p)
    if key == _SCAN_TIMES_KEY:
        return _SCAN_TIMES
    m = {}
    with open(p, encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            d = _day(str(r.get("modified") or "")[:10])
            if d and r.get("sha256"):
                m[r["sha256"].upper()] = d
    _SCAN_TIMES, _SCAN_TIMES_KEY = m, key
    return m


def scan_date(source_folder=None, source_file=None, pdf=None, modified=None, sha256=None):
    """When the paper was scanned, from the most trustworthy evidence available:
       1. the scan file's modified time on the NAS (the scanner wrote it; `modified` from the queue note,
          or looked up by the file's sha256 in scan_times.csv)                          -> {"date", "source": "file"}
       2. the PDF's own creation stamp - only when its clock is plausible (the Canon
          MF440 says 2003 for everything)                                              -> {"date", "source": "pdf"}
       3. the scanner's date in the file name ("DOC150124-..." = 15 Jan 2024,
          "MPS-05112025144414.pdf" = 5 Nov 2025)                                       -> {"date", "source": "stamp"}
       4. the year folder the person filed it under (typed by hand: at the start of
          a year the previous year is often typed from habit)                          -> {"year", "source": "folder"}
    None when there is no evidence."""
    d = _day(str(modified or "")[:10]) if modified else None
    if not d and sha256:
        d = scan_times().get(str(sha256).upper())
    if d and 2005 <= d.year <= _dt.date.today().year + 1:
        return {"date": d, "year": d.year, "source": "file"}
    if pdf:
        try:
            import subprocess
            info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=20).stdout
            m = re.search(r"^CreationDate:\s+(.+)$", info, re.M)
            if m:
                t = m.group(1).strip()
                for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b %d %H:%M:%S %Y %Z", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        d = _dt.datetime.strptime(re.sub(r"\s+[A-Z]{3,4}$", "", t) if "%Z" not in fmt else t, fmt).date()
                        if 2005 <= d.year <= _dt.date.today().year + 1:
                            return {"date": d, "year": d.year, "source": "pdf"}
                    except ValueError:
                        continue
        except Exception:
            pass
    f = str(source_file or "").upper()
    # "DOC150124-..." and "MPS-05112025144414.pdf". Not the Canon MF440's "4974_040524204834_001.pdf": that stamp is
    # yymmddHHMMSS from a clock that was never set (it says 2003/2004), like its PDF creation stamps.
    m = re.search(r"DOC(\d{2})(\d{2})(\d{2})(?!\d)", f) or re.search(r"-(\d{2})(\d{2})(20\d{2})\d{6}", f)
    if m:
        dd, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            d = _dt.date(y, mo, dd)
            if 2005 <= d.year <= _dt.date.today().year + 1:
                return {"date": d, "year": d.year, "source": "stamp"}
        except ValueError:
            pass
    y = folder_year(source_folder)
    return {"date": None, "year": y, "source": "folder"} if y else None


def apply_scan_date(date_iso, scan, chk=None, before_days=120, after_days=3):
    """Check a read order date against when the paper was scanned. An order is scanned soon after it is written, so
    its date must fall in [scan - before_days, scan + after_days]. A machine date (PDF stamp, scanner stamp) that
    disagrees only in the year corrects the year; with a year folder as the only evidence the year is corrected
    except for a January / February order read as the following year (the folder may be last year's typed from habit)
    or a December order of the previous year: those are flagged. Returns (date_iso, corrected)."""
    d = _day(date_iso)
    if not d or not scan:
        return date_iso, False

    def note(msg):
        if chk is not None:
            chk.append(msg)

    if scan.get("date"):
        sd = scan["date"]
        lo, hi = sd - _dt.timedelta(days=before_days), sd + _dt.timedelta(days=after_days)
        if lo <= d <= hi:
            return date_iso, False
        src = {"stamp": "the scanner's stamp", "pdf": "the PDF's scan stamp", "file": "the scan file's date"}.get(scan["source"], "the scan date")
        if d.year != sd.year:
            for y in (sd.year, sd.year - 1):
                try:
                    cand = d.replace(year=y)
                except ValueError:
                    cand = d.replace(year=y, day=28)
                if lo <= cand <= hi:
                    note(f"date read as {d.isoformat()} but {src} says scanned {sd.isoformat()}: corrected to {cand.isoformat()} - check")
                    return cand.isoformat(), True
        note(f"date read as {d.isoformat()} but {src} says scanned {sd.isoformat()}: check")
        return date_iso, False
    year = scan.get("year")
    if not year or d.year == year:
        return date_iso, False
    if d.year == year - 1 and d.month == 12:
        note(f"date read as {d.isoformat()} but the scan is filed under {year}: check")
        return date_iso, False
    if d.year == year + 1 and d.month <= 2:
        note(f"date read as {d.isoformat()} but the scan is filed under {year} (a January/February order may be filed under "
             f"the old year by habit): check")
        return date_iso, False
    try:
        fixed = d.replace(year=year)
    except ValueError:
        fixed = d.replace(year=year, day=28)
    note(f"date read as {d.isoformat()} but the scan is filed under {year}: corrected to {fixed.isoformat()} - check")
    return fixed.isoformat(), True


def check_order_date(order_number, date_iso, chk, window_days=120, scan=None):
    """Two checks on a read order date. First when the paper was scanned (apply_scan_date, with the evidence from
    scan_date()). Then the orders numbered around this one: a single misread digit that lands inside the window of
    their median is corrected and flagged; anything else outside the window is flagged. Returns (date_iso, corrected)."""
    fixed_year = False
    if scan:
        date_iso, fixed_year = apply_scan_date(date_iso, scan, chk)
    year = (scan or {}).get("year")
    m = re.fullmatch(r"(A|PL|DF|PO)(\d{3,6})", str(order_number or "").upper().replace(" ", ""))
    d = _day(date_iso)
    if not m or not d:
        return date_iso, fixed_year
    nb = _neighbours(m.group(1), int(m.group(2)))
    if len(nb) < 4:
        return date_iso, fixed_year
    dates = sorted(x[1] for x in nb)
    med = dates[len(dates) // 2]
    if abs((d - med).days) <= window_days:
        return date_iso, fixed_year
    if year and d.year == year and abs(d.year - med.year) >= 1:
        chk.append(f"date {d.isoformat()} matches the scan folder year but orders {m.group(1)}{min(x[0] for x in nb)}-"
                   f"{m.group(1)}{max(x[0] for x in nb)} are dated around {med.isoformat()}: check")
        return date_iso, fixed_year
    # try every single-digit change of the written date; the one closest to the neighbours' median wins
    s = d.isoformat()
    best = None
    for i, ch in enumerate(s):
        if not ch.isdigit():
            continue
        for alt in "0123456789":
            if alt == ch:
                continue
            td = _day(s[:i] + alt + s[i + 1:])
            if td and abs((td - med).days) <= window_days:
                gap = abs((td - med).days)
                if best is None or gap < best[0]:
                    best = (gap, td.isoformat())
    where = f"orders {m.group(1)}{min(x[0] for x in nb)}-{m.group(1)}{max(x[0] for x in nb)} are dated around {med.isoformat()}"
    if best:
        chk.append(f"date read as {s} but {where}: corrected to {best[1]} - check")
        return best[1], True
    chk.append(f"date read as {s} but {where}: check")
    return date_iso, False


# ---------------------------------------------------------------- apply to a read order
_JOB_REF = re.compile(r"\s*(?:J(?:OB)?\s*(?:NO\.?)?\s*[:#]?\s*)?0*\d{4,5}\s?\.?\s?[A-E\u20ac]\s*", re.I)
_BARE_REF = re.compile(r"\s*(?:J(?:OB)?\s*(?:NO\.?)?\s*[:#]?\s*)?0*\d{4,5}\s*")


def own_numbers(d):
    """Digits of the document's own numbers (order, Polymer PO, other PO ref) - never a job. An other-PO-ref that is
    itself a lettered job number (30660E written in the reference box) is a job, not one of these."""
    keys = ["order_number", "polymer_po"]
    ref = str(d.get("other_po_ref") or "")
    polymer = d.get("form") == "polymer_order" or str(d.get("order_number") or "").upper().startswith("PL")
    # on MPS orders and IGTs the reference box holds the job (30115, J30060E); on Polymer orders the Polymer PO number
    if not (_JOB_REF.fullmatch(ref) or (_BARE_REF.fullmatch(ref) and not polymer)):
        keys.append("other_po_ref")
    return {re.sub(r"\D", "", str(d.get(k) or "")) for k in keys} - {""}


def apply_allocation(d, chk, date_key="date_iso"):
    """Rewrites a read order (po_reader's answer dict, before find_job) by the slash rule:
    a job in the allocation box moves to job_number, terms to payment_terms, a method to order_method, allocation
    words are normalised, unknown ones flagged; VEHICLES orders get the registration into other_po_ref; IAC orders
    are ordered by Ivor. Records each change in d['allocation_changes']."""
    changes = []
    order_digits = own_numbers(d)
    res = classify_token(d.get("allocation"), d.get(date_key), order_digits)
    chk.extend(res["checks"])
    if res["kind"] == "job":
        jn = str(d.get("job_number") or "").strip().upper()
        if not jn or re.sub(r"\D", "", jn) == re.sub(r"\D", "", res["value"]):
            if jn != res["value"]:
                changes.append({"field": "job_number", "old": d.get("job_number"), "new": res["value"], "from": "allocation"})
            d["job_number"] = res["value"]
        elif res["value"] not in jn:
            chk.append(f"allocation names job {res['value']} but the form's job box reads {jn}: check")
        changes.append({"field": "allocation", "old": d.get("allocation"), "new": None})
        d["allocation"] = None
    elif res["kind"] == "terms":
        if not d.get("payment_terms"):
            d["payment_terms"] = res["value"]
        elif res["value"] not in str(d["payment_terms"]).upper():
            d["payment_terms"] = f"{d['payment_terms']}; {res['value']}"
        changes.append({"field": "allocation", "old": d.get("allocation"), "new": None, "to": "payment_terms"})
        d["allocation"] = None
    elif res["kind"] == "method":
        if not d.get("order_method"):
            d["order_method"] = res["value"]
        changes.append({"field": "allocation", "old": d.get("allocation"), "new": None, "to": "order_method"})
        d["allocation"] = None
    elif res["kind"] == "allocation":
        if res["value"] != (d.get("allocation") or ""):
            changes.append({"field": "allocation", "old": d.get("allocation"), "new": res["value"]})
        d["allocation"] = res["value"]
        d["allocation_known"] = res["known"]
    alloc = d.get("allocation") or ""
    if alloc == "VEHICLES":
        regs = find_registration(d.get("notes"), d.get("other_po_ref"), d.get("delivery_address"),
                                 " | ".join(str(l.get("description") or "") for l in (d.get("lines") or []) if isinstance(l, dict)))
        if regs:
            cur = str(d.get("other_po_ref") or "")
            new = [r for r in regs if r not in cur.replace(" ", "").upper()]
            if new:
                d["other_po_ref"] = "; ".join(x for x in [cur.strip()] + new if x)
                changes.append({"field": "other_po_ref", "old": cur or None, "new": d["other_po_ref"], "from": "registration"})
                chk.append(f"vehicle registration {', '.join(new)} put in Other PO Ref")
        else:
            chk.append("VEHICLES order with no registration number found: check")
    if alloc == "IAC":
        if (d.get("ordered_by") or "").strip().upper() not in ("IVOR", "IAC"):
            changes.append({"field": "ordered_by", "old": d.get("ordered_by"), "new": "Ivor", "from": "IAC"})
        d["ordered_by"] = "Ivor"
        if not d.get("signed_by") and not d.get("writer"):
            d["signed_by"] = "Ivor"
    if changes:
        d["allocation_changes"] = changes
    return d


if __name__ == "__main__":      # quick self-test:  python3 mps_jobs.py [JOBS_DIR]
    import json, sys
    JOBS_DIR = sys.argv[1] if len(sys.argv) > 1 else None
    for t in ["CONS", "cons", "VUSASO", "YUSOSO", "30660E", "1076LE", "T0625", "10625 E", "30689.E", "30 DAYS", "DISC 2.5% 30 DAYS",
              "EMAIL", "SALES EMAIL", "HARMONY", "VEHICLE", "Maint", "B2400", "9176C", "105501", "J 5927 D", "4580231241"]:
        r = classify_token(t, "2025-01-20", ("77189",))
        print(f"{t:>20} -> {r['kind']:<10} {r['value']!s:<12} known={r['known']} fixed={r['fixed']} {r['checks']}")
    print(find_registration("KIA K2700 BD 84 MT GP service", "TOYOTA HINO VXH 264 GP", "ZMG820GP tyres", "CA 123-456"))
    d = {"order_number": "A76731", "allocation": "VEHICLES", "notes": "HYUNDAI H100 - BD 84 MT GP", "date_iso": "2025-01-22", "lines": []}
    chk = []; apply_allocation(d, chk); print(json.dumps(d, indent=1), chk)

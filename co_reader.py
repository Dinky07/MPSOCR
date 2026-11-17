#!/usr/bin/env python3
"""
MPS customer orders: job sticker barcode -> order header + lines -> proposed Sage account.

Design: Claude Cowork\\Customer Orders\\Customer Orders - OCR pipeline design.md (rev 9).
  1. ocrmypdf (upright + Tesseract text layer) and zbarimg per page, as dn_reader does.
  2. The "MPS JOB NUMBER" sticker barcode (^\\d{4,5}[A-E]$) ties the order to the job; a page with a new job barcode
     starts a new order. Two job barcodes on one page: the one matching the printed number wins (stray sticker).
  3. CPU: VAT number(s) (only real Sage VAT numbers count, one-digit OCR slips corrected), order number (labelled
     field, suffix kept: 151559-MPO), order date, totals (excl + 15% VAT = incl), lines (qty x price = line total,
     lines sum to the excl total), the handwritten FILE HANDED name top left.
  4. The CPU lines are accepted only when the checksums hold; otherwise the vision model reads the page images with
     the Tesseract text as a hint (gpu_read), and the same checks apply.
  5. Account (decision 13): VAT -> active MPS accounts; one -> it; several -> PO-number pattern history (>=3 orders,
     >=80%), split -> top + flag, none -> the most recently used of them + flag.

analyse() works on OCR output only (texts + barcodes per page), so the rules can be tested away from the box.
Lookups come from CSVs on the pipeline share: sage_customers.csv, sage_so_history.csv, jobs_sage.csv, jobfile_orders.csv.
"""
import base64, csv, json, re, shutil, subprocess, tempfile, time, urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

DEFAULTS = {
    "job_pattern": r"^\d{4,5}[A-E]$",
    "own_vat_numbers": ["4320123039", "4240207755", "4570132524"],     # MPS, Polymer, Ronin
    "file_handed": ["FRED", "IVOR", "CHARLES", "JOHN", "REBECCA", "WHITY", "LOUISE", "DINO"],
    "vat_rate": 0.15,
    "history_min_orders": 3,
    "history_min_share": 0.8,
    "ocr": True, "ocr_jobs": 2, "barcode_dpi": 150,
    "gpu_model": "qwen2.5vl:7b", "gpu_px": 1600, "gpu_max_pages": 4,
    "ollama_url": "http://127.0.0.1:11434", "num_ctx": 49152, "keep_alive": "4h", "ollama_timeout": 900,
}
JOB_RX = re.compile(DEFAULTS["job_pattern"])


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


_OCR_FOLD = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})


def ocr_key(s):
    """763: an order number with the letters OCR swaps for digits folded (PO0004468 / POQ0G4468 still differ by one)."""
    return norm(s).translate(_OCR_FOLD)


def own_quote_ref(v):
    """763: MPS's own quotation number (HYDRAULICO007a/AC/AA, PEL097a/AC/AA): the customer accepted MPS's quote; its
    Document No is not the customer's order number (that is handwritten on it, or in Job File)."""
    return bool(re.search(r"/[A-Z]{2}/[A-Z]{2}$", str(v or "").upper().strip()))


def near_job(v, jobs):
    """763: an 'order number' that is the job number read badly (33U-61 for the handwritten 33461E)."""
    a = norm(v)
    return any(len(a) >= 4 and edits(a, norm(j)) <= 2 for j in jobs if j)


# --------------------------------------------------------------------------- lookups
class Lookups:
    """Sage customers, sales-order history, Sage jobs and Job File orders, from CSVs (reloaded when they change)."""

    def __init__(self, root, cfg=None):
        self.root = Path(root)
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.sig = None

    def _rows(self, name):
        p = self.root / name
        if not p.exists():
            return []
        with open(p, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def refresh(self):
        names = ("sage_customers.csv", "sage_so_history.csv", "jobs_sage.csv", "jobfile_orders.csv")
        sig = tuple((n, (self.root / n).stat().st_mtime if (self.root / n).exists() else None) for n in names)
        if sig == self.sig:
            return
        self.sig = sig
        own = {digits(v) for v in self.cfg["own_vat_numbers"]}
        self.clients = {}
        self.by_vat = defaultdict(list)
        for r in self._rows("sage_customers.csv"):
            if str(r.get("IsRetired", "")).lower() in ("true", "1"):
                continue
            r["vat"] = digits(r.get("Tax_Number"))
            self.clients[r["Account"]] = r
            if len(r["vat"]) == 10 and r["vat"] not in own:
                self.by_vat[r["vat"]].append(r)
        self.vats = set(self.by_vat)
        self.history = []                # (autoindex, account, po_key, ext_order_norm)
        self.last_used = {}
        self.sage_orders = set()
        for r in self._rows("sage_so_history.csv"):
            acct = r.get("Account") or ""
            ai = int(float(r.get("AutoIndex") or 0))
            self.history.append((ai, acct, po_key(r.get("ExtOrderNum")), norm(r.get("ExtOrderNum"))))
            self.last_used[acct] = max(self.last_used.get(acct, 0), ai)
            if r.get("ExtOrderNum"):
                self.sage_orders.add((acct, norm(r.get("ExtOrderNum"))))
        self.history.sort()
        self.jobs = {norm(r.get("JobNumber")) for r in self._rows("jobs_sage.csv") if r.get("Company") == "MPS"}
        self.jobfile = {}
        self.jobfile_by_order = defaultdict(list)
        self.jobfile_by_order_ocr = defaultdict(list)     # 763: O/0, I/1, S/5, B/8 confusions folded
        for r in self._rows("jobfile_orders.csv"):
            j = norm(r.get("JobNumber"))
            self.jobfile[j] = r
            if norm(r.get("OrderNumber")):          # 763: "#####" (Excel overflow typed into Job File) normalises to ""
                self.jobfile_by_order[norm(r["OrderNumber"])].append(j)
                self.jobfile_by_order_ocr[ocr_key(r["OrderNumber"])].append(j)
        nums = sorted(int(j[:-1]) for j in self.jobs | set(self.jobfile) if re.fullmatch(r"\d{4,5}E", j))
        # 763: the newest real job, not a typo in Sage (36098E, 41021E, 55388E, 61659E made the job_out_of_range check
        # dead): the highest number with at least 20 jobs in the 100 below it
        import bisect
        self.max_job = next((n for n in reversed(nums) if bisect.bisect_right(nums, n) - bisect.bisect_left(nums, n - 100) >= 20),
                            nums[-1] if nums else 0)

    # VAT ---------------------------------------------------------------------------------------------------------
    def jobs_for_order(self, order_number):
        """763: the Job File jobs typed with this order number: exact, else with OCR look-alikes folded (GP0O13401 /
        GPO13401: one extra or wrong character allowed on 7+ characters), unique only."""
        k = norm(order_number)
        if not k:
            return [], None
        hits = self.jobfile_by_order.get(k, [])
        if hits:
            return hits, "exact"
        ok = ocr_key(order_number)
        hits = self.jobfile_by_order_ocr.get(ok, [])
        if hits:
            return hits, "ocr"
        if len(k) >= 4 and re.search(r"\d", k):    # the scan lost a site suffix Job File has (4580 / 4580-CWC)
            suf = {j for key, js in self.jobfile_by_order.items() if key.startswith(k) and re.fullmatch(r"[A-Z]{2,4}", key[len(k):]) for j in js}
            recent = {j for j in suf if re.fullmatch(r"\d{4,5}E", j) and self.max_job and int(j[:-1]) > self.max_job - 1000}
            if len(suf) == 1 or len(recent) == 1:       # an old job with the same number years ago doesn't count
                return list(recent if len(recent) == 1 else suf), "suffix"
        if len(ok) >= 7:      # one character too many or too few only: a changed digit is often the next order number
            near = {j for key, js in self.jobfile_by_order_ocr.items() if abs(len(key) - len(ok)) == 1
                    and key[:2] == ok[:2] and edits(key, ok) == 1 for j in js}
            if len(near) == 1:
                return list(near), "ocr_near"
        return [], None

    def match_vat(self, cand):
        """A read 10-digit number -> (Sage VAT number, how) or (None, None). One digit off from exactly one Sage VAT
        number counts as an OCR slip."""
        if cand in self.vats:
            return cand, "exact"
        near = [v for v in self.vats if sum(a != b for a, b in zip(v, cand)) == 1]
        return (near[0], "one_digit") if len(near) == 1 else (None, None)

    def is_own(self, cand):
        return any(sum(a != b for a, b in zip(digits(o), cand)) <= 1 for o in self.cfg["own_vat_numbers"])

    def account_from_po(self, order_number, before=None):
        """No VAT on the order: the account past orders with the same PO pattern went to (>=3, >=80%), else None."""
        key = po_key(order_number)
        if not key or key in ("pre:PO", "pre:P"):
            return None, None
        n = Counter(a for ai, a, k, _ in self.history if k == key and a and (before is None or ai < before))
        if not n:
            return None, None
        top, cnt = n.most_common(1)[0]
        tot = sum(n.values())
        if cnt >= self.cfg["history_min_orders"] and cnt / tot >= self.cfg["history_min_share"]:
            return top, f"po_history {key} (no VAT on the order): {top} {cnt} of {tot}"
        return None, None

    # account (decision 13) -----------------------------------------------------------------------------------------
    def propose_account(self, vat, order_number, before=None):
        """-> (account, how, note, candidates). before: only history before this AutoIndex (replay tests)."""
        cands = [c["Account"] for c in self.by_vat.get(vat, [])]
        if not cands:
            return None, None, "vat_not_on_any_account", []
        if len(cands) == 1:
            return cands[0], "single_vat", None, cands
        cs = set(cands)
        key = po_key(order_number)
        hist = [(ai, a) for ai, a, k, _ in self.history if a in cs and (before is None or ai < before)]
        if key:
            n = Counter(a for ai, a, k, _ in self.history if k == key and a in cs and (before is None or ai < before))
            if n:
                top, cnt = n.most_common(1)[0]
                tot = sum(n.values())
                note = f"po_history {key}: {top} {cnt} of {tot}"
                if cnt >= self.cfg["history_min_orders"] and cnt / tot >= self.cfg["history_min_share"]:
                    return top, "po_history", note, cands
                return top, "po_history_split", note, cands
        last = {}
        for ai, a in hist:
            last[a] = ai
        if last:
            top = max(last, key=last.get)
            return top, "most_recent", f"no_history{(' for ' + key) if key else ''}: most recently used of {len(cands)}", cands
        top = max(cands, key=lambda a: self.clients[a].get("CreatedDate") or "")
        return top, "most_recent", f"no orders on any of {len(cands)} accounts: latest created", cands


def edits(a, b):
    """Levenshtein distance (short strings)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def same_order(read, typed):
    """The scan's order number is the one typed in Job File: equal, the typed one plus a suffix the scan lost
    (269930 / 269930-MOB), or one or two OCR slips (37908 / 57908, POQ0G4468 / PO0004468)."""
    a, b = norm(read), norm(typed)
    if not a or not b:
        return False
    if a == b or (len(a) >= 5 and b.startswith(a) and re.fullmatch(r"[A-Z]{2,4}", b[len(a):])):
        return True
    t = str(typed).upper().replace(" ", "")
    if len(a) >= 5 and t.startswith(str(read).upper().replace(" ", "")) and t[len(str(read).replace(" ", "")):][:1] in ("/", "-"):
        return True
    return len(b) >= 5 and edits(a, b) <= (1 if len(b) < 8 else 2)


def same_po(a, b, exact=False):
    """Two reads are the same PO once a site suffix is ignored; exact=False also allows one OCR slip. Splitting a
    file uses exact=True: one job's POs can be consecutive numbers (269942 / 269943-MOB)."""
    base = lambda s: re.sub(r"[A-Z]{2,4}$", "", norm(s)) if re.search(r"\d", norm(s)) else norm(s)
    x, y = base(a), base(b)
    return bool(x and y) and (x == y or (not exact and edits(x, y) <= 1))


def po_key(po):
    """The PO number's pattern: the site suffix (151559-MPO -> MPO), else the leading letters (POHTAU012603583)."""
    s = str(po or "").upper().strip()
    m = re.search(r"-\s*([A-Z]{2,4})\s*$", s)
    if m:
        return "sfx:" + m.group(1)
    m = re.match(r"^([A-Z]{2,6})", re.sub(r"[^A-Z0-9]", "", s))
    return "pre:" + m.group(1) if m else None


# --------------------------------------------------------------------------- numbers and dates
_MONEY = re.compile(r"(?<![\w.,])R?\s?(\d{1,3}(?:[  ,.]\d{3})+|\d+)([.,]\d{1,2})?(?![\d%])")
_NUM = re.compile(r"(?<![\w.,/-])(\d{1,3}(?:[  ,]\d{3})+|\d+)(?:([.,])(\d+))?(?![\w%/-])")


def to_num(intpart, dec):
    v = re.sub(r"[  ,.]", "", intpart)
    return float(v + ("." + dec[1:] if dec else ""))


def numbers(line):
    """[(value, has_decimals, start)] for the numbers on a line: '356 028,40', '2,480.00', '25', '99.2'."""
    out = []
    for m in _MONEY.finditer(line):
        try:
            out.append((to_num(m.group(1), m.group(2)), bool(m.group(2)), m.start()))
        except ValueError:
            pass
    return out


MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def find_dates(text):
    out = []
    for m in re.finditer(r"\b(\d{1,2})[\s\-/.]*(?:st|nd|rd|th)?[\s\-/.]*([A-Za-z]{3})[A-Za-z]*[\s\-/.,]*(\d{4})\b", text):
        mo = MONTHS.get(m.group(2).upper())
        if mo:
            out.append((m.start(), int(m.group(3)), mo, int(m.group(1))))
    for m in re.finditer(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b", text):
        out.append((m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in re.finditer(r"\b(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})\b", text):
        out.append((m.start(), int(m.group(1)), int(m.group(2)), int(m.group(3))))
    res = []
    for pos, y, mo, d in out:
        try:
            res.append((pos, date(y, mo, d).isoformat()))
        except ValueError:
            pass
    return sorted(res)


# --------------------------------------------------------------------------- header fields
_ORDER_LABELS = [
    (3, r"purchase\s*order\s*(?:number|no\b\.?|nr\b\.?|#)"),
    (3, r"client\s*p\.?\s*o\.?\s*(?:number|no\b\.?)"),
    (3, r"\bp\.?\s*o\.?\s*(?:number|no\b\.?|nr\b\.?|#)"),
    (2, r"\border\s*(?:number|no\b\.?|nr\b\.?|#)"),
    (2, r"\bdocument\s*(?:number|no\b\.?)"),
    (2, r"(?:^|\s{2,})number\s*[:#]"),               # 763: a "NUMBER:" label standing alone (QTS orders)
    (1, r"standard\s*purchase\s*order"),
    (1, r"\bpurchase\s*order\b(?!\s*(?:number|no|date))"),
]
_NOT_ORDER = re.compile(r"^(DATE|PAGE|COMPANY|REV|REVISION|TYPE|NO|NUMBER|ORDER|PURCHASE|DELIVERY|ACCOUNT|SUPPLIER|VAT|TEL|FAX|EMAIL|TO|FROM|OF"
                        r"|\d{1,2}(?:ST|ND|RD|TH))$", re.I)          # 763: "27th" (a date) was taken as an order number


def order_candidates(text):
    """[(score, value)] from labelled fields; the value is on the label's line or on the 1-3 lines under it, and a
    site suffix on its own line ('- MOB') is joined back on."""
    lines = text.splitlines()
    out = []
    for i, ln in enumerate(lines):
        for score, rx in _ORDER_LABELS:
            m = re.search(rx, ln, re.I)
            if not m:
                continue
            rest = ln[m.end():]
            vals = []
            mm = re.match(r"[\s:#|.]*([A-Z0-9][A-Z0-9/\-_.]*(?:\s?[-/]\s?[A-Z0-9]+)*)", rest, re.I)
            if mm and re.search(r"\d", mm.group(1)) and not _NOT_ORDER.match(mm.group(1)):
                vals.append((mm.group(1), i))
            else:
                col = m.start()
                for k in range(i + 1, min(i + 4, len(lines))):
                    seg = lines[k][max(0, col - 25): m.end() + 45].strip(" |")
                    tok = re.match(r"([A-Z0-9][A-Z0-9/\-_.]*(?:\s?[-/]\s?[A-Z0-9]+)*)", seg, re.I)
                    if tok and re.search(r"\d", tok.group(1)):
                        vals.append((tok.group(1), k))
                        break
            for v, k in vals:
                v = v.strip(" .,")
                nxt = lines[k + 1].strip() if k + 1 < len(lines) else ""
                if re.fullmatch(r"-\s*[A-Z]{2,4}", nxt) and "-" not in v:
                    v += "-" + re.sub(r"[-\s]", "", nxt)
                v = re.sub(r"\s*-\s*", "-", v)
                if 3 <= len(norm(v)) <= 25 and not re.fullmatch(r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{2,4}", v):
                    out.append((score, v))
            break
    return out


def written_name(top, names):
    """763: the FILE HANDED name top left; one OCR slip allowed on names of 5+ letters (REBECCH, WHITV)."""
    exact = next((n for n in names if re.search(r"\b" + n + r"\b", top)), None)
    if exact:
        return exact
    for w in re.findall(r"[A-Z]{4,9}", top):
        near = [n for n in names if len(n) >= 5 and abs(len(n) - len(w)) <= 1 and edits(n, w) <= 1]
        if len(near) == 1:
            return near[0]
    return None


def header(texts, lk, c, scan_day=None):
    text = "\n".join(texts)
    first = texts[0] if texts else ""
    f, flags = {}, []
    # VAT numbers: 10 digits starting with 4, spaces/dashes allowed inside; only Sage VAT numbers count
    raw = re.findall(r"(?<!\d)(4(?:[ \-]?\d){9})(?!\d)", text)
    seen, vats = [], []
    for r in raw:
        d = digits(r)
        if d in seen:
            continue
        seen.append(d)
        if lk.is_own(d):
            continue
        v, how = lk.match_vat(d)
        if v and v not in [x[0] for x in vats]:
            vats.append((v, how, d))
    if not vats:      # OCR cut the number short after a VAT label ("VAT No: 47302173"): a unique Sage prefix counts
        for m in re.finditer(r"(?i)\bvat\s*(?:reg(?:istration)?\.?\s*)?(?:no|nr|number)?\.?\s*[:#]?\s*(4[\d ]{7,11})", text):
            p = digits(m.group(1))
            hits = [v for v in lk.vats if v.startswith(p)] if 8 <= len(p) < 10 and not lk.is_own(p.ljust(10, "0")) else []
            if len(hits) == 1:
                vats.append((hits[0], "prefix", p))
                break
    f["vat_numbers_seen"] = seen
    if vats:
        f["vat_number"], how, read = vats[0]
        f["vat_read"] = read
        if how in ("one_digit", "prefix"):
            flags.append("vat_ocr_corrected")
        if len(vats) > 1:
            flags.append("several_customer_vat_numbers")
            f["vat_other"] = [v for v, _, _ in vats[1:]]
    else:
        f["vat_number"] = None
        flags.append("vat_unknown" if [d for d in seen if not lk.is_own(d)] else "vat_missing")
    # 763: MPS's own purchase order form (letterhead, "ORDER A 85193") filed with the customer orders
    if re.search(r"MINING\s+PRESSURE\s+SYSTEMS", "\n".join(first.splitlines()[:6]), re.I) and \
            re.search(r"\bORDER\s+A\s?\d{5}\b", first) and not vats:
        flags.append("not_a_customer_order")
    # order number
    cands = order_candidates(first) or order_candidates(text)
    cands.sort(key=lambda x: -x[0])
    jobs_here = {norm(x) for t in texts for x in re.findall(r"(?<!\d)(\d{4,5}\s?[A-E])(?![A-Z0-9])", t)}
    dropped = [v for _, v in cands if own_quote_ref(v) or near_job(v, jobs_here)]
    if dropped:                                          # 763
        f["order_candidates_dropped"] = dropped[:5]
        if any(own_quote_ref(v) for v in dropped):
            flags.append("mps_quote_as_order")
        cands = [(sc, v) for sc, v in cands if v not in dropped]
    f["order_candidates"] = [v for _, v in cands][:5]
    f["order_number"] = cands[0][1] if cands else None
    # order date: next to a date label, else the first date on page 1
    def plausible(d):
        return not scan_day or (date.fromisoformat(scan_day) - date.fromisoformat(d)).days in range(-3, 181)
    od = None
    for m in re.finditer(r"(?:order\s*date|po\s*date|date\s*of\s*order|\bdate\b)", first, re.I):
        ds = [x for x in find_dates(first[m.end(): m.end() + 120]) if plausible(x[1])]
        if ds:
            od = ds[0][1]
            break
    if not od:
        ds = [x for x in find_dates(first) if plausible(x[1])]
        od = ds[0][1] if ds else None
    f["order_date"] = od
    # totals: the largest excl / VAT / incl triple that adds up (VAT 15% of excl), else excl + incl
    vals = sorted({round(v, 2) for ln in text.splitlines() for v, dec, _ in numbers(ln) if dec and v >= 1})
    rate = c["vat_rate"]
    best = None
    vs = set(vals)
    for e in reversed(vals):
        v = round(e * rate, 2)
        for vv in (v, round(v - 0.01, 2), round(v + 0.01, 2)):
            if vv in vs:
                for ii in (round(e + vv, 2), round(e + vv - 0.01, 2), round(e + vv + 0.01, 2)):
                    if ii in vs:
                        best = (e, vv, ii)
                        break
            if best:
                break
        if best:
            break
    if not best:
        for e in reversed(vals):
            i = round(e * (1 + rate), 2)
            if any(abs(i - x) <= 0.02 for x in vals):
                best = (e, round(i - e, 2), i)
                break
    if best:
        f["total_excl"], f["total_vat"], f["total_incl"] = best
    else:
        f["total_excl"] = f["total_vat"] = f["total_incl"] = None
        flags.append("totals_not_found")
    # the handwritten FILE HANDED name (top left of page 1)
    top = " ".join(first.splitlines()[:4]).upper()
    f["project_lead_written"] = written_name(top, c["file_handed"])
    return f, flags


# --------------------------------------------------------------------------- lines (CPU)
_STOP = re.compile(r"\b(sub\s*-?\s*total|total\s*(?:excl|incl|zar|vat|amount|summary)?|vat\s*@|amount\s*excl|grand\s*total|carried\s*forward)\b", re.I)
_SKIP = re.compile(r"\b(page\s*\d|vat\s*reg|reg\.?\s*no|tel\b|fax\b|p\.?\s*o\.?\s*box)", re.I)


def cpu_lines(texts, total_excl):
    """Item rows where qty x unit price (- discount) = line total; following text-only rows extend the description.
    Returns (items, ok): ok when every row checks and the rows sum to the excl total (+-R1)."""
    items, cur = [], None
    for page in texts:
        for ln in page.splitlines():
            if _STOP.search(ln) and items:
                cur = None
                continue
            if _SKIP.search(ln):
                continue
            nums = numbers(ln)
            row = None
            if len(nums) >= 2:
                money_ = [(v, p) for v, dec, p in nums if dec]
                if money_:
                    tot, tp = money_[-1]
                    others = [(v, dec, p) for v, dec, p in nums if p < tp]
                    for qi, (q, qdec, qp) in enumerate(others):
                        for (u, udec, up) in others[qi + 1:]:
                            if q > 0 and u > 0 and abs(q * u - tot) <= max(0.05, tot * 0.001):
                                row = (q, u, None, tot, qp)
                                break
                            if q > 0 and u > 0 and q * u > tot > 0:
                                disc = round(100 * (1 - tot / (q * u)), 2)
                                if disc in (2.5, 5.0, 7.5, 10.0, 12.5, 15.0, 20.0, 25.0, 30.0) or any(abs(disc - x) < 0.05 for x, _, _ in nums):
                                    row = (q, u, disc, tot, qp)
                                    break
                        if row:
                            break
            if row:
                q, u, disc, tot, qp = row
                desc = re.sub(r"[\d.,]+\s*%?", " ", ln)
                desc = re.sub(r"\s{2,}", " ", desc).strip(" |-")
                code = None
                mcode = re.match(r"\s*(?:\d{1,3}\s+)?([A-Z0-9][A-Z0-9\-/.]{3,})\s{2,}", ln)
                if mcode and re.search(r"\d", mcode.group(1)) and re.search(r"[A-Z]", mcode.group(1)):
                    code = mcode.group(1)
                cur = {"line_no": len(items) + 1, "customer_code": code, "description": desc, "quantity": q,
                       "unit_price": u, "discount_pct": disc, "line_total": round(tot, 2)}
                items.append(cur)
            elif cur is not None:
                t = ln.strip(" |")
                if t and not re.search(r"\d{3,}[.,]\d{2}", t) and len(t) > 3 and len(cur["description"]) < 400:
                    cur["description"] = (cur["description"] + " " + re.sub(r"\s{2,}", " ", t)).strip()
                elif not t:
                    cur = None
    s = round(sum(i["line_total"] for i in items), 2)
    ok = bool(items) and total_excl is not None and abs(s - total_excl) <= 1.0
    return items, ok, s


# --------------------------------------------------------------------------- the reader
def split_orders(barcodes, texts, c):
    """[(job or None, [page indexes], job_from, flags)]: a page with a job barcode not equal to the current job
    starts an order. Two job barcodes on a page: the printed MPS JOB NUMBER decides."""
    groups = []
    for i, codes in enumerate(barcodes):
        jobs = sorted({norm(x) for x in codes if JOB_RX.match(norm(x))}, key=lambda j: (int(j[:-1]), j))      # 763: by number
        printed = [norm(x) for x in re.findall(r"MPS\s*JOB\s*NUMBER\s*[\r\n ]*(\d{4,5}\s?[A-E])\b", texts[i], re.I)]
        flags, how = [], "barcode"
        if len(jobs) > 1:
            pick = [j for j in jobs if j in printed]
            nums = [(int(j[:-1]), j[-1]) for j in jobs]
            if len(pick) == 1:
                flags.append("stray_job_barcode: " + ", ".join(j for j in jobs if j != pick[0]))
                jobs = pick
            elif len(jobs) == 2 and nums[0][1] == nums[1][1] and nums[1][0] == nums[0][0] + 1:
                # 763: the cut-off barcode strip of the previous label on the roll above the sticker (17 Nov: 7 of 7
                # pairs were n-1 / n, the sticker's own number is the higher one)
                flags.append(f"stray_job_barcode: {jobs[0]} (strip of the previous label)")
                jobs, how = [jobs[1]], "barcode_pair"
            else:
                flags.append("several_jobs: " + ", ".join(jobs))
                jobs = jobs[:1]
        if not jobs and printed:
            jobs, how = printed[:1], "printed"
        if jobs and (not groups or groups[-1][0] != jobs[0]):
            groups.append([jobs[0], [i], how, flags])
        elif groups:
            groups[-1][1].append(i)
            groups[-1][3] += flags
        else:
            groups.append([None, [i], None, flags])
    return groups


def analyse(texts, barcodes, lk, cfg=None, name=""):
    """OCR output of one scanned file -> [order dict]. No GPU here: 'needs_gpu' says when the lines must be read
    by the vision model."""
    c = dict(DEFAULTS, **(cfg or {}))
    lk.refresh()
    orders = []
    m = re.match(r"DOC(\d\d)(\d\d)(\d\d)", name or "")
    scan_day = f"20{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
    groups = []
    for job, pages, job_from, gflags in split_orders(barcodes, texts, c):
        # one job can carry several customer POs in one file (Harmony: 269942/269943/269956-MOB on 33344E):
        # a page whose strongly labelled PO number differs from the current one starts a new order
        cur = None
        for i in pages:
            top = [v for s, v in sorted(order_candidates(texts[i]), key=lambda x: -x[0])]
            first_page = any(re.search(rx, texts[i], re.I) for s, rx in _ORDER_LABELS if s >= 3)
            if top and first_page and cur is not None and cur[4] and not same_po(top[0], cur[4], exact=True):
                groups.append(cur)
                cur = None
            if cur is None:
                cur = [job, [i], job_from, list(gflags) if not groups or groups[-1][0] != job else [], top[0] if top else None]
            else:
                cur[1].append(i)
                cur[4] = cur[4] or (top[0] if top else None)
        groups.append(cur)
    orders = _orders(groups, texts, lk, c, scan_day, name)
    # a second PO on the same job in one file often loses its site suffix (269943 next to 269942-MOB): inherit it
    for o in orders:
        on = o["fields"].get("order_number") or ""
        if o["job"] and on and not re.search(r"-[A-Z]{2,4}$", on):
            sib = [x["fields"].get("order_number") or "" for x in orders if x is not o and x["job"] == o["job"]]
            sfx = {m.group(0) for s in sib for m in [re.search(r"-[A-Z]{2,4}$", s)] if m}
            if len(sfx) == 1 and len(norm(on.rstrip("-"))) == len(norm(re.sub(r"-[A-Z]{2,4}$", "", sib[0]))):
                o["fields"]["order_number"] = on.rstrip("-") + sfx.pop()
                o["flags"].append("order_suffix_from_sibling")
                if o["fields"].get("vat_number"):
                    acct, how, note, cands = lk.propose_account(o["fields"]["vat_number"], o["fields"]["order_number"])
                    o["fields"].update(customer_account=acct, account_how=how, account_note=note)
                    o["flags"] = [x for x in o["flags"] if not x.startswith("account_")] + (
                        ["account_" + how] if how in ("po_history_split", "most_recent") else [])
    return orders


def _orders(groups, texts, lk, c, scan_day, name):
    orders = []
    for job, pages, job_from, gflags, _ in groups:
        ptexts = [texts[i] for i in pages]
        f, flags = header(ptexts, lk, c, scan_day)
        flags = gflags + flags
        f["source_pages"] = [i + 1 for i in pages]
        jf = None
        sev = next((x for x in flags if x.startswith("several_jobs: ")), None)
        if sev:      # two job stickers, printed number unreadable: the job whose Job File order matches the scan wins
            alts = [norm(j) for j in sev.split(": ", 1)[1].split(", ")]
            fit = [j for j in alts if (lk.jobfile.get(j) or {}).get("OrderNumber")
                   and any(same_order(x, lk.jobfile[j]["OrderNumber"].strip().splitlines()[0]) for x in f.get("order_candidates", []))]
            if len(fit) != 1:                            # 763: the Job File order number printed anywhere on the page
                page = ocr_key(" ".join(ptexts))
                fit = [j for j in alts if len(norm((lk.jobfile.get(j) or {}).get("OrderNumber"))) >= 5
                       and re.search(r"\d", lk.jobfile[j]["OrderNumber"])
                       and ocr_key(lk.jobfile[j]["OrderNumber"].strip().splitlines()[0]) in page]
            if len(fit) == 1:
                job, job_from = fit[0], "barcode+jobfile"
                flags[flags.index(sev)] = "stray_job_barcode: " + ", ".join(j for j in alts if j != fit[0])
        if not job:
            hits, how = lk.jobs_for_order(f.get("order_number"))
            if len(hits) == 1:
                job, job_from = hits[0], "jobfile_order_no" + ("" if how == "exact" else "_" + how)
                flags.append("job_from_jobfile_order_no")
                if how != "exact":                     # 763: a near match (OCR look-alike, lost suffix): a person confirms
                    flags.append(f"job_unconfirmed: job {job} from Job File order {lk.jobfile[job].get('OrderNumber')} ({how} match)")
            else:
                flags.append("no_job")
        f["job_number"], f["job_number_from"] = job, job_from
        if job:
            n = int(job[:-1])
            if job[-1] == "E" and lk.max_job and n > lk.max_job + 300:
                flags.append("job_out_of_range")
            jf = lk.jobfile.get(job)
        # Job File cross-check (order no., value incl. VAT, FILE HANDED)
        if jf:
            jfo = (jf.get("OrderNumber") or "").strip().splitlines()[0].strip() if (jf.get("OrderNumber") or "").strip() else ""
            f["jobfile_order_number"] = jfo or None
            f["project_lead_jobfile"] = jf.get("FileHanded") or None
            if jfo:
                jo = norm(jfo)
                cn = f.get("order_candidates", [])
                hit = next((x for x in cn if same_order(x, jfo)), None)
                if not hit and len(norm(jfo)) >= 5 and re.search(r"\d", jfo) and \
                        (norm(jfo) in norm(" ".join(ptexts)) or ocr_key(jfo) in ocr_key(" ".join(ptexts))):
                    hit = jfo                                # 763: printed on the order, under a label the rules don't know
                    f["order_number_found"] = "on the page"
                if hit:
                    f["order_number"], f["order_number_from"] = jfo, "scan_confirmed_by_jobfile"
                    if norm(hit) != jo:
                        f["order_number_read"] = hit
                elif not f.get("order_number"):
                    f["order_number"], f["order_number_from"] = jfo, "jobfile"
                    flags.append("order_number_from_jobfile")
                    if job_from not in ("barcode", "printed"):       # 763: a job picked by a rule, not read: hold
                        flags.append("job_unconfirmed: the order number was not read and the job was not read off one sticker")
                else:
                    other = [j for j in lk.jobfile_by_order.get(norm(f["order_number"]), []) if j != job]
                    f["order_number_from"] = "scan"
                    if len(other) == 1:
                        flags.append(f"job_mismatch: order {f['order_number']} is Job File {other[0]}")
                    else:
                        flags.append(f"jobfile_order_number_differs: {jfo}")
            try:
                jv = float(jf.get("OrderValue") or "")
                if f.get("total_incl") and abs(jv - f["total_incl"]) > 1.0 and abs(jv - (f.get("total_excl") or 0)) > 1.0:
                    flags.append(f"jobfile_value_differs: {jv:.2f}")
            except ValueError:
                pass
            if f.get("project_lead_written") and jf.get("FileHanded") and f["project_lead_written"] != jf["FileHanded"].upper():
                flags.append(f"project_lead_differs: written {f['project_lead_written']}, Job File {jf['FileHanded']}")
            if not jfo:
                f["order_number_from"] = "scan" if f.get("order_number") else None     # 763: was left unset
        else:
            f["order_number_from"] = "scan" if f.get("order_number") else None
        if job_from == "barcode_pair" and f.get("order_number_from") != "scan_confirmed_by_jobfile":
            # 763: the higher of two consecutive barcodes, not confirmed by the Job File order number printed on the
            # page: still a person's look (the job proposed is the likely one)
            pair = next((x for x in flags if x.startswith("stray_job_barcode: ")), "")
            flags.append(f"several_jobs: {pair.split(': ', 1)[1].split(' ')[0] if pair else '?'}, {job} ({job} proposed: in every pair seen the "
                         "sticker's own barcode was the higher number; the Job File order number was not found on the page)")
        # account (decision 13)
        if f.get("vat_number"):
            account_from_vat(f, flags, lk)
        elif f.get("order_number"):
            acct, note = lk.account_from_po(f["order_number"])
            if acct:
                cl = lk.clients.get(acct, {})
                f.update(customer_account=acct, account_how="po_history_no_vat", account_note=note,
                         customer_name=cl.get("Name"), vat_number_sage=digits(cl.get("Tax_Number")) or None)
        if not f.get("customer_account") and jf and (jf.get("CodOrAccount") or "").upper() == "COD":
            acct = "KUS10D" if (jf.get("Division") or "").upper() == "KUSASA" else "CAS10D"
            f.update(customer_account=acct, account_how="cod_cash", account_note="Job File COD, VAT on no Sage account",
                     customer_name=lk.clients.get(acct, {}).get("Name"))
        # lines (CPU)
        items, ok, s = cpu_lines(ptexts, f.get("total_excl"))
        f["items"], f["lines_sum_excl"] = items, s
        f["line_count"] = len(items)
        orders.append({"_page_numbers": {round(v, 2) for t in ptexts for ln in t.splitlines() for v, dec, _ in numbers(ln) if dec},
                       "job": job, "pages": [i + 1 for i in pages], "fields": f, "flags": flags,
                       "lines_ok": ok, "needs_gpu": not ok, "hold": hold_reason(flags), "name": name})
    return orders


HOLD = ("not_a_customer_order", "no_job", "several_jobs", "job_mismatch", "job_unconfirmed", "vat_missing", "vat_unknown", "job_out_of_range")


def hold_reason(flags):
    for h in HOLD:
        if any(x == h or x.startswith(h + ":") for x in flags):
            return h
    return None


# --------------------------------------------------------------------------- OCR (box) and the vision model
def ocr(pdf, c, log=None):
    """ocrmypdf + zbarimg with dn_reader's helpers -> (upright pdf, texts, barcodes, tmpdir)."""
    import dn_reader as d
    tmp = Path(tempfile.mkdtemp(prefix="co-"))
    up = tmp / "upright.pdf"
    d.make_upright(pdf, up, {"ocr": c["ocr"], "ocr_jobs": c["ocr_jobs"]}, log)
    n = d.page_count(up)
    texts = d.page_texts(up, n)
    with tempfile.TemporaryDirectory() as td:
        codes = d.page_barcodes(up, n, int(c["barcode_dpi"]), td)
        for i in range(n):
            if not any(JOB_RX.match(norm(x)) for x in codes[i]):
                codes[i] += [x for x in d.barcode_retry(up, i + 1, 300, td) if x not in codes[i]]
    return up, texts, codes, tmp


GPU_PROMPT = """You are reading a customer's purchase order sent to Mining Pressure Systems (MPS). MPS is the supplier, not the customer: ignore MPS's own address and VAT number (4320123039).
Read the order exactly as printed or handwritten and answer with JSON only:
{"order_number": the customer's purchase order number exactly as written, including any suffix such as "-MPO" or "/10538" (never the MPS JOB NUMBER sticker such as 33343E),
 "order_date": "YYYY-MM-DD",
 "customer_vat_number": the customer's 10-digit VAT number or null,
 "total_excl": number or null, "total_vat": number or null, "total_incl": number or null,
 "lines": [{"line_no": n, "item_code": customer's item code or null, "description": full item description,
            "quantity": number, "unit": text or null, "unit_price": price per unit excl VAT, "discount_pct": number or null,
            "line_total": line total excl VAT}],
 "written_name_top_left": the handwritten name at the top left of page 1, or null}
Numbers are plain JSON numbers without currency or thousands separators. Include every order line on every page; leave out totals, VAT and notes."""


def gpu_read(up_pdf, texts, c, log=None):
    """The vision model reads the page images (Tesseract text given as a hint). -> dict (the JSON above)."""
    tmp = Path(tempfile.mkdtemp(prefix="cog-"))
    try:
        n = min(len(texts), int(c["gpu_max_pages"]))
        subprocess.run(["pdftoppm", "-png", "-scale-to", str(c["gpu_px"]), "-f", "1", "-l", str(n), str(up_pdf), str(tmp / "p")],
                       check=True, capture_output=True, timeout=300)
        imgs = [base64.b64encode(p.read_bytes()).decode() for p in sorted(tmp.glob("p*.png"))]
        hint = "\n\n".join(f"--- page {i + 1} (OCR text, may contain errors) ---\n{t[:6000]}" for i, t in enumerate(texts[:n]))
        body = {"model": c["gpu_model"], "stream": False, "format": "json", "keep_alive": c["keep_alive"],
                "options": {"temperature": 0, "num_ctx": int(c["num_ctx"]),
                            "num_predict": max(int(c.get("answer_cap_min", 4000)), int(c.get("answer_cap_per_page", 3000)) * max(1, len(imgs)))},   # 684
                "messages": [{"role": "user", "content": GPU_PROMPT + "\n\n" + hint, "images": imgs}]}
        req = urllib.request.Request(c["ollama_url"].rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=int(c["ollama_timeout"])) as r:
            resp = json.loads(r.read())
        if resp.get("done_reason") == "length":          # 684: the model looped; fail now, not at the 15 min timeout
            raise RuntimeError(f"answer stopped at the {body['options']['num_predict']}-token cap "
                               f"({round((resp.get('eval_duration') or 0) / 1e9)} s): the model was repeating itself on this page")
        ans = json.loads(resp["message"]["content"])
        ans["_secs"] = round(time.time() - t0, 1)
        return ans
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def account_from_vat(f, flags, lk):
    """Decision 13: the account from the VAT number (PO history decides between accounts sharing it)."""
    acct, how, note, cands = lk.propose_account(f["vat_number"], f.get("order_number"))
    f.update(customer_account=acct, account_how=how, account_note=note, vat_candidates=cands)
    if acct:
        cl = lk.clients.get(acct, {})
        f["customer_name"] = cl.get("Name")
        f["client_dclink"] = int(float(cl["DCLink"])) if cl.get("DCLink") else None
        f["tax_type_id"] = int(float(cl["iDefTaxTypeID"])) if cl.get("iDefTaxTypeID") else None
        if str(cl.get("On_Hold", "")).lower() in ("true", "1"):
            flags.append("account_on_hold")
    if how in ("po_history_split", "most_recent"):
        flags.append("account_" + how)
    if acct and (acct, norm(f.get("order_number"))) in lk.sage_orders:
        flags.append("already_in_sage")


def apply_gpu(order, ans, lk, c):
    """Merge the vision model's answer into an order: its lines replace the CPU's when they add up; header fields
    fill gaps only (the CPU's checked values win). Returns the order."""
    f, flags = order["fields"], order["flags"]
    def num(x):
        try:
            return round(float(x), 2) if x not in (None, "") else None
        except (TypeError, ValueError):
            return None
    items = []
    for i, l in enumerate(ans.get("lines") or [], 1):
        q, u, t = num(l.get("quantity")), num(l.get("unit_price")), num(l.get("line_total"))
        if t is None and q is not None and u is not None:
            t = round(q * u * (1 - (num(l.get("discount_pct")) or 0) / 100), 2)
        items.append({"line_no": i, "customer_code": l.get("item_code"), "description": str(l.get("description") or "").strip(),
                      "quantity": q, "unit": l.get("unit"), "unit_price": u, "discount_pct": num(l.get("discount_pct")),
                      "line_total": t})
    s = round(sum(i["line_total"] or 0 for i in items), 2)
    excl = f.get("total_excl") or num(ans.get("total_excl"))
    rate = c["vat_rate"]
    n_items = len(items)
    if n_items >= 2 and n_items % 2 == 0 and items[: n_items // 2] and all(
            (a["description"], a["line_total"]) == (b["description"], b["line_total"]) for a, b in zip(items[: n_items // 2], items[n_items // 2:])) \
            and excl and abs(s / 2 - excl) <= 1.0:
        items = items[: n_items // 2]                     # 763: the model listed every line twice (Northam 31375E)
        s = round(sum(i["line_total"] or 0 for i in items), 2)
        flags.append("gpu_lines_listed_twice")
    if items and excl and abs(s - excl * (1 + rate)) <= 1.0 and abs(s - excl) > 1.0:
        # 763: the model took the "Total (Incl)" column as the line total (ECS 33533E): back to excl when every line
        # is quantity x unit price x (1 + VAT)
        if all(i["quantity"] is not None and i["unit_price"] is not None and i["line_total"] is not None
               and abs(i["quantity"] * i["unit_price"] * (1 - (i["discount_pct"] or 0) / 100) * (1 + rate) - i["line_total"]) <= 0.05 * max(1, i["line_total"])
               for i in items):
            for i in items:
                i["line_total"] = round(i["quantity"] * i["unit_price"] * (1 - (i["discount_pct"] or 0) / 100), 2)
            s = round(sum(i["line_total"] for i in items), 2)
            flags.append("gpu_lines_were_incl_vat")
    ok = bool(items) and excl is not None and abs(s - excl) <= 1.0
    printed = order.get("_page_numbers", set())
    if not ok and items and excl is None and round(s, 2) in printed:
        # no total found by either reader, but the lines add up to a number printed on the order: that is the total
        f["total_excl"], excl, ok = s, s, True
        v = round(s * c["vat_rate"], 2)
        f["total_vat"] = v if v in printed else None
        f["total_incl"] = round(s + v, 2) if round(s + v, 2) in printed else None
        flags[:] = [x for x in flags if x != "totals_not_found"] + ["totals_from_lines"]
    gx = num(ans.get("total_excl"))
    if not ok and items and gx is not None and abs(s - gx) <= 1.0 and f.get("total_excl") and \
            gx in order.get("_page_numbers", set()):
        # the CPU took the wrong triple; the model's lines and its own excl total agree and that total is printed
        f["total_excl"], f["total_vat"], f["total_incl"] = gx, num(ans.get("total_vat")), num(ans.get("total_incl"))
        flags[:] = [x for x in flags if x != "totals_not_found"] + ["totals_from_gpu"]
        excl, ok = gx, True
    if not f.get("total_excl") and num(ans.get("total_excl")):
        f["total_excl"], f["total_vat"], f["total_incl"] = num(ans.get("total_excl")), num(ans.get("total_vat")), num(ans.get("total_incl"))
        flags[:] = [x for x in flags if x != "totals_not_found"] + ["totals_from_gpu"]
    jf = lk.jobfile.get(order.get("job") or "") or {}
    try:
        jv = float(jf.get("OrderValue") or "")
    except ValueError:
        jv = None
    if not ok and items and jv and s and (abs(s * (1 + rate) - jv) <= max(1.5, 0.001 * jv) or abs(s - jv) <= max(1.5, 0.001 * jv)):
        # 763: no total on the page the rules could use, but the lines add up to the value typed in Job File (incl. VAT,
        # or excl. where it was typed so): the lines and that total stand, marked
        f["total_excl"] = s
        f["total_vat"] = round(s * rate, 2)
        f["total_incl"] = round(s * (1 + rate), 2)
        excl, ok = s, True
        flags[:] = [x for x in flags if x != "totals_not_found"] + ["totals_from_jobfile_value"]
    if f.get("total_excl") and (f.get("total_incl") in (None, 0) or (f.get("total_incl") == f["total_excl"] and not f.get("total_vat"))):
        v = round(f["total_excl"] * rate, 2)              # 763: incl. missing, or the model copied excl. into incl.
        if round(f["total_excl"] + v, 2) in printed or "totals_from_jobfile_value" in flags:
            f["total_vat"], f["total_incl"] = v, round(f["total_excl"] + v, 2)
    go = str(ans.get("order_number") or "").strip()
    if go and (own_quote_ref(go) or near_job(go, [order.get("job")])):
        go = ""
    if go and not f.get("order_number"):
        f["order_number"], f["order_number_from"] = go, "gpu"
    elif go and f.get("order_number_from") == "jobfile":
        # 763: the order number came from Job File (nothing labelled on the page). The model read one off the image:
        # the same -> confirmed; another -> the scan's number stands and Job File is checked against it
        if same_order(go, f["order_number"]):
            f["order_number_from"] = "gpu_confirmed_by_jobfile"
            flags[:] = [x for x in flags if x != "order_number_from_jobfile" and not x.startswith("job_unconfirmed")]
        else:
            jfo = f["order_number"]
            f["order_number"], f["order_number_from"] = go, "gpu"
            flags[:] = [x for x in flags if x != "order_number_from_jobfile"]
            other, _ = lk.jobs_for_order(go)
            other = [j for j in other if j != order.get("job")]
            flags.append(f"job_mismatch: order {go} is Job File {other[0]}" if len(other) == 1 else f"jobfile_order_number_differs: {jfo}")
    if not f.get("vat_number") and ans.get("customer_vat_number"):
        v, how = lk.match_vat(digits(ans["customer_vat_number"]))
        if v:
            f["vat_number"] = v
            flags[:] = [x for x in flags if x not in ("vat_missing", "vat_unknown")] + ["vat_from_gpu"]
            account_from_vat(f, flags, lk)
    if not f.get("project_lead_written") and ans.get("written_name_top_left"):
        f["project_lead_written"] = written_name(str(ans["written_name_top_left"]).upper().strip(), c["file_handed"])
    f["gpu_lines_sum_excl"] = s
    if ok:
        f["items"], f["lines_sum_excl"], f["line_count"] = items, s, len(items)
        order["lines_ok"], order["lines_from"] = True, "gpu"
    else:
        flags.append(f"totals_mismatch: lines {s:.2f} vs excl {excl}")
        order["lines_from"] = "none"
    order["needs_gpu"] = False
    order["hold"] = hold_reason(flags) or (None if ok else "totals_mismatch")
    order["gpu_ran"] = True
    return order


# --------------------------------------------------------------------------- publishing (worker) and edits (dashboard)
REVIEW_TEXT = {
    "no_job": "No job number (no sticker barcode, not in Job File)", "several_jobs": "More than one job sticker on the page",
    "job_mismatch": "The PO number belongs to another job in Job File", "vat_missing": "No customer VAT number on the order",
    "vat_unknown": "VAT number on no Sage account", "job_out_of_range": "Job number far above the newest job",
    "totals_mismatch": "Lines don't add up to the order total", "account_check": "Account not settled by VAT / PO history",
    "job_unconfirmed": "Job not read off one sticker and no order number read to confirm it",
    "not_a_customer_order": "MPS's own purchase order (A-number), not a customer's order: Move to Purchase Orders",
    "mps_quote_as_order": "MPS's own quotation accepted as the order: its Document No is not the order number",
    "totals_from_jobfile_value": "Total taken from the lines, which add up to the Job File value",
    "gpu_lines_were_incl_vat": "Line totals were incl. VAT: worked back to excl.",
}
SETTLED = ("single_vat", "po_history", "manual")


def person_hold(o):
    """Why an order waits on the dashboard: a paper problem (hold_reason), lines that don't add up, or an account
    the rules didn't settle (decision 13: split history, most-recent fallback, no VAT, cash sale)."""
    if o.get("hold"):
        return o["hold"]
    if not o.get("lines_ok") and not o["fields"].get("lines_from_workings"):
        return "totals_mismatch"
    if o["fields"].get("account_how") not in SETTLED:
        return "account_check"
    return None


def review_text(flags):
    out = []
    for x in flags:
        k = x.split(":", 1)[0]
        out.append(REVIEW_TEXT.get(k, x.replace("_", " ")))
    return "; ".join(dict.fromkeys(out)) or None


def lines_text(items, limit=4000):
    t = "\n".join(f"{_q(i.get('quantity'))} x {i.get('description') or ''} @ {i.get('unit_price')} = {i.get('line_total')}" for i in items)
    return t[:limit] or None


def _q(v):
    try:
        v = float(v)
        return str(int(v)) if v == int(v) else f"{v:g}"
    except (TypeError, ValueError):
        return str(v or "")


def finish(o, lk):
    """Final sidecar fields for one order (before the PDF is written)."""
    f = o["fields"]
    o["hold"] = person_hold(o)
    f["lines_ok"] = bool(o.get("lines_ok"))
    f["lines_from"] = o.get("lines_from") or ("cpu" if o.get("lines_ok") else "none")
    f["account_proposed"] = f.get("customer_account")
    f["revision"] = f.get("revision", 0)
    f["pages"] = len(o["pages"])
    f["lines_text"] = lines_text(f.get("items") or [])
    f["review"] = review_text(o["flags"])
    f["library_doc_type"] = "Customer Order"
    jf = lk.jobfile.get(o["job"] or "", {})
    cust = (jf.get("Customer") or f.get("customer_name") or "").strip()
    f["file_customer"] = cust or None
    stem = " ".join(x for x in (cust, (f.get("order_number") or "NO ORDER NO").replace("/", "_"), f"rev{f['revision']}", o["job"] or "") if x)
    return re.sub(r"[^A-Za-z0-9 _.()&'-]+", "_", stem)[:150]


def split_pdf(up_pdf, pages, dst, n_pages):
    """The order's pages as their own PDF (the whole file when it is one order)."""
    dst = Path(dst)
    if list(pages) == list(range(1, n_pages + 1)):
        shutil.copy2(up_pdf, dst)
        return dst
    td = Path(tempfile.mkdtemp(prefix="cos-"))
    try:
        subprocess.run(["pdfseparate", "-f", str(pages[0]), "-l", str(pages[-1]), str(up_pdf), str(td / "s-%04d.pdf")],
                       check=True, capture_output=True, timeout=120)
        parts = [str(td / f"s-{p:04d}.pdf") for p in pages]
        if len(parts) == 1:
            shutil.move(parts[0], dst)
        else:
            subprocess.run(["pdfunite", *parts, str(dst)], check=True, capture_output=True, timeout=120)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return dst


def recompute(f):
    """After an edit on the dashboard: lines text, sum and checks again; an account changed by a person is 'manual'."""
    items = f.get("items") or []
    for i in items:
        try:
            if i.get("line_total") in (None, "") and i.get("quantity") is not None and i.get("unit_price") is not None:
                i["line_total"] = round(float(i["quantity"]) * float(i["unit_price"]) * (1 - float(i.get("discount_pct") or 0) / 100), 2)
        except (TypeError, ValueError):
            pass
    s = round(sum(float(i.get("line_total") or 0) for i in items), 2)
    f["lines_sum_excl"], f["line_count"] = s, len(items)
    f["lines_ok"] = bool(items) and f.get("total_excl") not in (None, "") and abs(s - float(f["total_excl"])) <= 1.0
    f["lines_text"] = lines_text(items)
    if f.get("customer_account") and f.get("customer_account") != f.get("account_proposed"):
        f["account_how"] = "manual"
    return f

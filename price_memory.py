"""Price memory for the purchase-order reader.

A unit price read off an order is corrected to a price this supplier was paid for the same item on a checked order,
but only when the two differ in the way Melyssa's handwriting is misread: her 4 read as 1, 6, 7, 0 or 2 or dropped
altogether (86.00 for 486.00), or the decimal point lost (x10 / x100). Every correction is reported so it is flagged
for review; a price that simply changed since is left alone (it is not "confusable") or, rarely, corrected and flagged.

Measured 24 Oct 2026 on the 134 held-out checked orders, memory built from the other 549: line prices 68.0 % -> 76.1 %
(25 corrections, 23 right, 2 wrong - both genuine price changes).

    price_memory.build(docs)                      -> {supplier_key: [[desc_key, price], ...]}
    price_memory.write(path, docs)                (training.py, with the verified supplier list)
    price_memory.load(path)                       cached on the file's mtime
    price_memory.fix_lines(lines, supplier, mem)  -> [(line_no, old, new, description)]; lines changed in place
"""
import difflib, json, os, re
from collections import defaultdict

# what each true digit has been misread as on checked orders (true -> read)
CONFUSE = {"4": set("16702"), "1": set("47"), "6": set("40"), "0": set("46"), "7": set("12"), "2": set("6")}
_CACHE = {}


def _n(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def supplier_key(s):
    return re.sub(r"(PTY|LTD|CC)+$", "", _n(s))[:12]


def _price(v):
    try:
        return None if v in (None, "") else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def build(docs):
    """docs: checked orders (field dicts with supplier and items[]). Returns {supplier_key: [[desc_key, price], ...]}."""
    mem = defaultdict(set)
    for f in docs:
        s = supplier_key(f.get("supplier") or f.get("supplier_name"))
        if not s or s.startswith("UNKNOWN"):
            continue
        for it in f.get("items") or []:
            p, d = _price(it.get("unit_price")), _n(it.get("description"))
            if p and d:
                mem[s].add((d, p))
    return {k: sorted([list(x) for x in v]) for k, v in mem.items()}


def write(path, docs):
    mem = build(docs)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"note": "Unit prices from checked orders (training.py): the reader corrects misread prices to these",
                   "suppliers": mem}, fh, ensure_ascii=False)
    os.replace(tmp, path)
    return sum(len(v) for v in mem.values())


def load(path):
    if not path or not os.path.exists(path):
        return {}
    mt = os.path.getmtime(path)
    hit = _CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            mem = json.load(fh).get("suppliers") or {}
    except (OSError, ValueError):
        mem = {}
    _CACHE[path] = (mt, mem)
    return mem


def confusable(read, known):
    """True when a misread of Melyssa's digits would turn the price `known` into `read`."""
    if read is None or known is None or read == known:
        return False
    for f in (10, 100, 0.1, 0.01):
        if abs(read - known * f) < 0.005:
            return True
    a, b = f"{read:.2f}", f"{known:.2f}"
    if len(a) == len(b):
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        return 0 < len(diff) <= 2 and all(x in CONFUSE.get(y, ()) for x, y in diff)
    if len(b) == len(a) + 1:                      # a 4 dropped: 86.00 read for 486.00
        return any(b[i] == "4" and b[:i] + b[i + 1:] == a for i in range(len(b)))
    return False


def fix_lines(lines, supplier, mem, min_sim=0.85, price_key="unit_price", desc_key="description"):
    """Corrects confusable unit prices in place; returns [(line_no, old, new, description)]."""
    known = mem.get(supplier_key(supplier)) or []
    out = []
    if not known:
        return out
    for i, it in enumerate(lines or [], 1):
        p, d = _price(it.get(price_key)), _n(it.get(desc_key))
        if p is None or not d:
            continue
        cands = {kp for kd, kp in known if difflib.SequenceMatcher(None, d, kd).ratio() >= min_sim}
        if not cands or p in cands:
            continue                              # unknown item, or a price this item has been bought at
        fits = {kp for kp in cands if confusable(p, kp)}
        if len(fits) == 1:
            new = fits.pop()
            it[price_key] = new
            out.append((i, p, new, it.get(desc_key)))
    return out

"""scorer.py: one way to score a reader's answer against the checked answer, for every document type.

    compare(read, truth, kind)   -> {metric: (right, counted)} for one document
    score(pairs, kind)           -> {"fields": {metric: {"right", "counted", "acc"}}, "overall", "docs"}
    regressions(old, new, ...)   -> the metrics that got worse from one score to the next (the regression gate)

`kind` is a document type ("po", "ws", "jt", "tc") or "generic" for the others (delivery notes, release notes,
customer orders, proofs of payment, supplier invoices). The po / ws / jt / tc rules are the same ones their training
scripts use (training.py, ws_training.py, jt_training.py, tc_training.py): tests/test_scorer.py checks that the two give
the same numbers, so a score here and a score on the Training tab mean the same thing.

Rules, in short: a field that is blank in both the reading and the checked answer is not counted. Text is compared
without spaces, punctuation or case. Item lines are compared in order (line 1 with line 1, ...), out of the number of
checked lines. "overall" is the mean of the per-field accuracies.
"""
import difflib
import re

_DATE = re.compile(r"^\s*(\d{1,4})[./\-\s](\d{1,2})[./\-\s](\d{1,4})\s*$")


# ------------------------------------------------------------------------------------------------ normalising
def norm_text(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def norm_jt(v):
    """jt_training.norm: [signature] / [illegible] are blank, and 9.09.2026 == 09-09-2026 == 2026/09/09."""
    s = str(v or "").strip()
    if s.lower() in ("[signature]", "[illegible]", "null", "none"):
        return ""
    m = _DATE.match(s)
    if m:
        a, b, c = m.groups()
        y, mo, d = (a, b, c) if len(a) == 4 else (c, b, a)
        if len(y) == 2:
            y = "20" + y
        try:
            return f"{int(y):04d}{int(mo):02d}{int(d):02d}"
        except ValueError:
            pass
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _num(v, nd):
    try:
        return None if v in (None, "") else round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _fuzzy(a, b, norm):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() >= 0.9


def _same(kind, a, b, norm):
    if kind == "text":
        return norm(a) == norm(b)
    if kind == "num2":
        return _num(a, 2) == _num(b, 2)
    if kind == "num3":
        return _num(a, 3) == _num(b, 3)
    if kind == "fuzzy":
        return _fuzzy(a, b, norm)
    if kind == "dims":
        return {k: norm(v) for k, v in (a or {}).items()} == {k: norm(v) for k, v in (b or {}).items()}
    if kind == "set":                  # "A1; A2" == "A2;A1"
        return {norm(x) for x in str(a or "").split(";") if norm(x)} == {norm(x) for x in str(b or "").split(";") if norm(x)}
    raise ValueError(f"unknown comparison {kind!r}")


# ------------------------------------------------------------------------------------------------ what is scored per type
# fields: [(name, comparison)]; lines: [(metric, line key, comparison)]; blank: "raw" = skip when both are None / "",
# "norm" = skip when both normalise to ""; bools: scored only when the checked answer has the key.
SPECS = {
    "po": {"fields": [(k, "text") for k in ("order_number", "polymer_po", "supplier", "date", "order_method", "att_of",
                                            "allocation", "job_number", "other_po_ref", "quote_ref", "payment_terms",
                                            "signed_by", "cancelled")],
           "items": "items", "only_dicts": False, "norm": norm_text, "blank": "raw",
           "lines": [("line_description", "description", "fuzzy"), ("line_qty", "quantity", "num2"),
                     ("line_price", "unit_price", "num2")]},
    "ws": {"fields": [(k, "text") for k in ("date", "sheet_no", "sheet_of", "project", "corrosion_int", "corrosion_ext",
                                            "destination", "job_number", "spec", "customer_dwg_no", "rev", "mps_dwg_no",
                                            "batch_no")] + [("stamped", "bool"), ("ifc_signed", "bool"), ("stamp_date", "text")],
           "items": "items", "only_dicts": False, "norm": norm_text, "blank": "raw",
           "lines": [("line_item_no", "item_no", "text"), ("line_qty", "qty", "num2"), ("line_size", "size", "fuzzy"),
                     ("line_dwg_no", "dwg_no", "text"), ("line_dims", "dims", "dims")]},
    # JTs are scored in the reader's answer format (jt_reader), as jt_training does
    "jt": {"fields": [(k, "text") for k in ("jt_number", "date", "mps_job_no", "transferred_from", "transferred_to",
                                            "received_by", "remarks")],
           "items": "line_items", "only_dicts": True, "norm": norm_jt, "blank": "norm",
           "lines": [("line_qty", "quantity", "text"), ("line_description", "description", "fuzzy")]},
    "tc": {"fields": [("cert_no", "text"), ("date", "text"), ("mill", "text"), ("mps_po", "set"), ("ronin_po", "set"),
                      ("family", "text"), ("grade", "text")],
           "items": "items", "only_dicts": True, "norm": norm_text, "blank": "raw",
           "lines": [("line_size_nb", "size_nb", "num3"), ("line_qty", "qty", "num3"), ("line_heat", "heat_no", "heat")]},
}

# generic: every plain value on the checked answer except the pipeline's own bookkeeping, plus the number of lines
GENERIC_SKIP = {"checked", "checked_how", "checked_by", "checked_at", "content_hash", "review", "read_error", "pages",
                "file", "source_file", "source_pdf", "source_folder", "source_modified", "source_sha256", "extractor",
                "flags", "edits", "notes", "item_codes", "old_columns"}


def generic_spec(truth):
    keys = [k for k, v in truth.items() if k not in GENERIC_SKIP and not k.startswith("_")
            and (v is None or isinstance(v, (str, int, float, bool)))]
    return {"fields": [(k, "text") for k in sorted(keys)], "items": "items", "only_dicts": True, "norm": norm_text,
            "blank": "raw", "lines": []}


def spec_for(kind, truth=None):
    return SPECS[kind] if kind in SPECS else generic_spec(truth or {})


# ------------------------------------------------------------------------------------------------ scoring
def _blank(v, norm, how):
    return norm(v) == "" if how == "norm" else v in (None, "")


def compare(read, truth, kind, gold_heats=None):
    """{metric: (right, counted)} for one document. tc: gold_heats = the old site's heat numbers (scored whether or
    not the cert is checked); pass truth=None for a cert that has only those."""
    read = read or {}
    res = {}
    if kind == "tc" and gold_heats:
        got = set()
        for x in [h for h in (read.get("heats") or []) if isinstance(h, dict)] + \
                 [i for i in (read.get("items") or []) if isinstance(i, dict)]:
            k = norm_text(x.get("heat_no_read") or x.get("heat_no"))
            if k:
                got.add(k)
        res["heat_old_site"] = (sum(1 for g in gold_heats if g in got), len(gold_heats))
    if truth is None:
        return res
    sp = spec_for(kind, truth)
    norm = sp["norm"]
    for k, how in sp["fields"]:
        a, b = read.get(k), truth.get(k)
        if how == "bool":
            if k not in truth:                       # checked before the field existed: not scored
                continue
            res[k] = (1 if bool(a) == bool(b) else 0, 1)
            continue
        if _blank(a, norm, sp["blank"]) and _blank(b, norm, sp["blank"]):
            continue
        res[k] = (1 if _same(how, a, b, norm) else 0, 1)
    ri, vi = read.get(sp["items"]) or [], truth.get(sp["items"]) or []
    if sp["only_dicts"]:
        ri, vi = [x for x in ri if isinstance(x, dict)], [x for x in vi if isinstance(x, dict)]
    if ri or vi:
        n = max(len(vi), 1)
        res["lines_count"] = (1 if len(ri) == len(vi) else 0, 1)
        for metric, key, how in sp["lines"]:
            if how == "heat":                        # the reader's own heat reading, before the old-site correction
                right = sum(1 for x, y in zip(ri, vi) if norm_text(x.get("heat_no_read") or x.get("heat_no")) == norm_text(y.get(key)))
            else:
                right = sum(1 for x, y in zip(ri, vi) if _same(how, x.get(key), y.get(key), norm))
            res[metric] = (right, n)
    return res


def total(per_doc):
    """[{metric: (right, counted)}] -> the score dict."""
    tot = {}
    for c in per_doc:
        for k, (r, n) in c.items():
            t = tot.setdefault(k, [0, 0])
            t[0] += r
            t[1] += n
    fields = {k: {"right": r, "counted": n, "acc": round(r / n, 4) if n else None} for k, (r, n) in tot.items()}
    accs = [v["acc"] for v in fields.values() if v["acc"] is not None]
    return {"fields": fields, "overall": round(sum(accs) / len(accs), 4) if accs else None, "docs": len(per_doc)}


def score(pairs, kind):
    """pairs: [(read, truth)] -> {"fields": {metric: {"right", "counted", "acc"}}, "overall": acc, "docs": n}."""
    return total([compare(r, t, kind) for r, t in pairs])


def regressions(old, new, max_drop=0.02, min_counted=5):
    """Metrics where `new` scores lower than `old` by more than max_drop (accuracy, 0-1), among metrics counted at
    least min_counted times in both. -> [(metric, old acc, new acc)], worst first; overall is checked too."""
    out = []
    of, nf = old.get("fields") or {}, new.get("fields") or {}
    for k in sorted(set(of) & set(nf)):
        a, b = of[k], nf[k]
        if a.get("acc") is None or b.get("acc") is None or min(a.get("counted", 0), b.get("counted", 0)) < min_counted:
            continue
        if b["acc"] < a["acc"] - max_drop:
            out.append((k, a["acc"], b["acc"]))
    if old.get("overall") is not None and new.get("overall") is not None and new["overall"] < old["overall"] - max_drop:
        out.append(("overall", old["overall"], new["overall"]))
    return sorted(out, key=lambda x: x[2] - x[1])

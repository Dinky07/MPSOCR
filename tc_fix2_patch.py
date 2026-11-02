#!/usr/bin/env python3
"""445 (Dino 2 Nov "Test the narrow version"): the 444 prompt changes made normal certs read worse (item lines 100 ->
78-83 %), so the prompt is left exactly as it is. Only a certificate page answered with nothing but its page type
(DOC280117-007: "inspection_only", empty) is asked once more - to read the whole page, the handwritten MPS job / order
number at the top included. The handwritten order joins order_numbers (-> mps_po), the job is fields.mps_job; a job
written together with its order ("JOB 4122 A49797" read as one) is split.
    tc_fix2_patch.py <tc_reader.py>"""
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
for old, new in (
    ('''              keep_alive="15m", num_ctx=49152, earlier_heats=None):''',
     '''              keep_alive="15m", num_ctx=49152, earlier_heats=None, extra=""):'''),
    ('''        body = {"model": model, "stream": False, "format": "json", "keep_alive": keep_alive,
''', '''        prompt += ("\\n\\n" + extra) if extra else ""
        body = {"model": model, "stream": False, "format": "json", "keep_alive": keep_alive,
'''),
    ('''            pages.append({"page": p, "how": how, "answer": a})
''', '''            if (a or {}).get("page_type") in CERT_TYPES and not _has_content(a):
                # 445: a certificate page answered with its type only (DOC280117-007): asked once more
                try:
                    a2 = read_page(path, p, ollama_url, model, "", family, px, hint, timeout, keep_alive,
                                   earlier_heats=earlier, extra=REREAD_EMPTY)
                    if _has_content(a2):
                        a, how = a2, how + "+reread (empty answer)"
                except Exception:
                    pass
            pages.append({"page": p, "how": how, "answer": a})
'''),
    ('''def read(path, ollama_url, model="qwen2.5vl:7b",''', '''REREAD_EMPTY = ('Your answer for this page was empty, but it is a certificate (a page with a chemical composition table '
                'is an mtc, even when it is titled "INSPECTION CERTIFICATE"). Read the whole page: the header fields, every '
                'order number, every item line with its heat number ("Heat No.", "Fitting Heat No."), and the chemistry / '
                'mechanical results. MPS often writes its job number and order number by hand at the top ("JOB 4122 A49797"): '
                'add "handwritten": {"job": the job number as written or null, "order": the order number as written or null}.')


def _has_content(a):
    a = a or {}
    return any(a.get(k) for k in ("cert_no", "mill", "items", "heats", "order_numbers", "customer", "standard"))


def read(path, ollama_url, model="qwen2.5vl:7b",'''),
    ('''         "cert_pages": [p["page"] for p in cert_pages], "extra": [], "legibility": []}
''', '''         "cert_pages": [p["page"] for p in cert_pages], "extra": [], "legibility": []}
    for p in cert_pages:                                  # 445: MPS's handwritten job / order number (re-asked pages)
        hw = (p["answer"] or {}).get("handwritten")
        if not isinstance(hw, dict):
            continue
        job, order = str(hw.get("job") or "").strip(), str(hw.get("order") or "").strip()
        m = re.search(r"(?<![A-Z0-9])A\\s?-?\\s?\\d{5}(?!\\d)", job.upper())
        if m and not order:                               # "JOB 4122 A49797" read as one: the A-number is the order
            order, job = m.group(0), (job[:m.start()] + job[m.end():]).strip()
        if order:
            o = {"label": "handwritten", "value": order}
            if o not in d["order_numbers"]:
                d["order_numbers"].append(o)
        if job and not d.get("mps_job"):
            d["mps_job"] = job
'''),
    ('''    f["order_numbers"] = d["order_numbers"]
''', '''    f["order_numbers"] = d["order_numbers"]
    f["mps_job"] = re.sub(r"[^0-9A-Z]", "", re.sub(r"\\bJOB\\b", "", str(d.get("mps_job") or "").upper())) or None   # 445
'''),
):
    n = s.count(old)
    if n != 1:
        sys.exit(f"expected 1 match, found {n}: {old[:70]!r}")
    s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("patched", p)

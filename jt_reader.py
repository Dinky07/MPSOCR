#!/usr/bin/env python3
"""Job Transfer (J.T.) reader for the pipeline: the prompt and output schema of the earlier JT work (the corrected
review set in <root>/training/*.jsonl), worked examples shown before every read as po_reader does, and a tidy-up
into the worker's JT fields (SCHEMAS["jt"]: jt_number, date, job_number, from, to, written_by, received_by,
remarks, items[item_no, quantity, unit, description]) so normalise("jt") and the sync carry on unchanged.

    import jt_reader
    raw_answer, pages, text = jt_reader.read(pdf, ollama_url, model, fewshot_dir, px)
    worker_raw = jt_reader.to_worker(raw_answer)        # -> normalise("jt", worker_raw, cfg)

Worked examples: <fewshot_dir>/<name>.png + <name>.json (the JSON in the answer schema below). jt_fewshot/ is built
from the review set by eval_jt.py --make-fewshot.
"""
import base64, glob, json, os, re, subprocess, tempfile, urllib.request

PROMPT = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "jt_prompt.txt"), encoding="utf-8").read() \
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "jt_prompt.txt")) else None

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "jt_number": {"type": ["string", "null"]}, "customer": {"type": ["string", "null"]},
        "mps_job_no": {"type": ["string", "null"]}, "date": {"type": ["string", "null"]},
        "transferred_from": {"type": ["string", "null"]}, "transferred_to": {"type": ["string", "null"]},
        "line_items": {"type": "array", "items": {"type": "object", "properties": {
            "quantity": {"type": ["string", "null"]}, "item_no": {"type": ["string", "null"]},
            "description": {"type": ["string", "null"]}, "blank_row_above": {"type": "boolean"}},
            "required": ["quantity", "item_no", "description", "blank_row_above"]}},
        "remarks": {"type": ["string", "null"]}, "transferred_by": {"type": ["string", "null"]},
        "received_by": {"type": ["string", "null"]}, "receiving_stamp": {"type": ["string", "null"]},
    },
    "required": ["jt_number", "customer", "mps_job_no", "date", "transferred_from", "transferred_to", "line_items",
                 "remarks", "transferred_by", "received_by", "receiving_stamp"],
}
NUM_PREDICT = 3072
NUM_CTX = 49152         # same as po_reader.NUM_CTX: Ollama keeps one model loaded across document types
_FEW = {}
_PROMPTS = {}


def prompt_text(name=None):
    """jt_prompt.txt, or another prompt file beside this one named by the type (e.g. jt_prompt_ronin.txt)."""
    if not name:
        return PROMPT
    if name not in _PROMPTS:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.path.basename(name)), encoding="utf-8") as fh:
            _PROMPTS[name] = fh.read()
    return _PROMPTS[name]


def fewshot_msgs(fewshot_dir, prompt=None):
    """Worked examples (image + verified answer), cached per folder and prompt."""
    prompt = prompt or PROMPT
    key = (fewshot_dir, prompt)
    if key in _FEW:
        return _FEW[key]
    msgs = []
    if fewshot_dir and os.path.isdir(fewshot_dir):
        for j in sorted(glob.glob(os.path.join(fewshot_dir, "*.json"))):
            if os.path.basename(j) == "sources.json":
                continue
            png = os.path.splitext(j)[0] + ".png"
            if not os.path.exists(png):
                continue
            with open(j, encoding="utf-8") as fh:
                ans = json.load(fh)
            for li in ans.get("line_items") or []:        # examples predate the flag: every item there starts after a gap
                li.setdefault("blank_row_above", True)
            msgs.append({"role": "user", "content": prompt, "images": [base64.b64encode(open(png, "rb").read()).decode()]})
            msgs.append({"role": "assistant", "content": json.dumps(ans, ensure_ascii=False)})
    _FEW[key] = msgs
    return msgs


def read(path, ollama_url, model="qwen2.5vl:7b", fewshot_dir=None, px=1600, timeout=900, keep_alive="30m", prompt=None):
    """(answer dict in ANSWER_SCHEMA, pages read, raw text). Page 1 only: a JT is a single page."""
    text = prompt_text(prompt)
    if not text:
        raise RuntimeError("jt_prompt.txt is missing beside jt_reader.py")
    with tempfile.TemporaryDirectory() as tmp:
        p = str(path)
        if p.lower().endswith(".pdf"):
            subprocess.run(["pdftoppm", "-png", "-scale-to", str(px), "-f", "1", "-l", "1", p, os.path.join(tmp, "p")],
                           check=True, capture_output=True, timeout=300)
            pngs = sorted(glob.glob(os.path.join(tmp, "p*.png")))
        else:
            pngs = [p]
        if not pngs:
            raise RuntimeError("no pages could be rendered")
        body = {"model": model, "stream": False, "format": ANSWER_SCHEMA, "keep_alive": keep_alive,
                "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
                "messages": fewshot_msgs(fewshot_dir, text) + [{"role": "user", "content": text,
                            "images": [base64.b64encode(open(pngs[0], "rb").read()).decode()]}]}
        req = urllib.request.Request(ollama_url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read())["message"]["content"]
    return tidy(parse_answer(raw)), 1, raw


def parse_answer(raw):
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise RuntimeError(f"model returned non-JSON: {raw[:200]!r}")
        d = json.loads(m.group(0))
    if not isinstance(d, dict):
        raise RuntimeError("model returned a non-object answer")
    return d


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def tidy(a):
    """Apply the table rules the prompt asks for, in code: once an item with a quantity or item number has been seen,
    a later entry with neither is a continuation of the previous item (no blank row above: joined with " | ") or a
    note for the whole form (blank row above: moved to remarks, after the remarks box text). Entries before the first
    real item are left alone, so a form whose writer forgot the quantity still keeps its item."""
    items = [li for li in (a.get("line_items") or []) if isinstance(li, dict)]
    out, notes, seen_item, last = [], [], False, "item"       # last: what a continuation line attaches to
    for li in items:
        has_key = bool(_clean(li.get("quantity")) or _clean(li.get("item_no")))
        desc = _clean(li.get("description"))
        if has_key or not seen_item or not desc:
            out.append(li); seen_item = seen_item or has_key; last = "item"
            continue
        if li.get("blank_row_above") is False and (out if last == "item" else notes):
            if last == "item":
                prev = out[-1]
                prev["description"] = f"{_clean(prev.get('description'))} | {desc}" if _clean(prev.get("description")) else desc
            else:
                notes[-1] = f"{notes[-1]} | {desc}"
        else:
            notes.append(desc); last = "note"
    if notes:
        rem = _clean(a.get("remarks")) or ""
        for n in notes:
            if _norm(n) and _norm(n) not in _norm(rem):
                rem = f"{rem} | {n}" if rem else n
        a["remarks"] = rem
    a["line_items"] = out
    a["_moved_to_remarks"] = len(notes)
    return a


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s in ("", "null", "None", "[signature]") else s


def to_worker(a, number_prefix=None):
    """The answer in the worker's JT schema (what extract_builtin returned before): normalise('jt') does the rest."""
    items = []
    for li in a.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        desc = _clean(li.get("description"))
        if not desc:
            continue
        q = _clean(li.get("quantity"))
        unit = None
        item_no = _clean(li.get("item_no"))
        if item_no and re.fullmatch(r"(OFF|EA|EACH|SETS?|PAIRS?)\.?", item_no.upper()) and q and not re.search(r"[A-Z]", q.upper()):
            q, item_no = f"{q} {item_no.upper()}", None          # "34 | OFF" written across the two columns
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(OFF|EA|EACH|M|MTR|MTRS|KG|KGS|LEN|LENS|SETS?|PAIRS?|BOXES?|ROLLS?)\.?", (q or "").upper())
        if m:
            q, unit = m.group(1), m.group(2)
        items.append({"item_no": item_no, "quantity": q, "unit": unit, "description": desc})
    job = _clean(a.get("mps_job_no"))
    if job:
        job = re.sub(r"^(?:J(?:OB)?\s*(?:NO\.?)?\s*[:#]?\s*)(?=\d)", "", job.upper()).strip()
    num = re.sub(r"\D", "", str(a.get("jt_number") or "")) or None
    if num and number_prefix:                                  # Ronin forms: DF 16593 -> DF16593
        num = f"{number_prefix}{num}"
    out = {"jt_number": num,
           "date": _clean(a.get("date")), "job_number": job,
           "from": _clean(a.get("transferred_from")), "to": _clean(a.get("transferred_to")),
           "written_by": _clean(a.get("transferred_by")), "received_by": _clean(a.get("received_by")),
           "remarks": _clean(a.get("remarks")), "items": items}
    for k in ("customer", "receiving_stamp"):
        if _clean(a.get(k)):
            out[k] = _clean(a.get(k))
    return out

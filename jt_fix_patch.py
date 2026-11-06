#!/usr/bin/env python3
"""531 (Dino 6 Nov "Yes, fix it"): half the JTs scanned to Chronoscan\\JTs MPS are upside down, and the reader failed
on 10 scans (89 pages) because one page's answer was not JSON (a long or looping answer cut off), which failed the
whole scan. jt_reader.py:
  1. each page image goes through Tesseract's orientation check first and is turned upright when it is confidently
     sideways / upside down (as tc_reader since 453);
  2. an answer that is not JSON is asked again once without the worked examples (they make the model loop, 29 Oct);
     if that is not JSON either, the line items read before the cut are kept ("salvaged", flagged for review);
worker.py: a JT page whose answer still cannot be used is held on its own (like a timed-out page, 332), so the file's
other pages are kept.
    jt_fix_patch.py <jt_reader.py> <worker.py>"""
import sys

jr, wk = sys.argv[1], sys.argv[2]
s = open(jr, encoding="utf-8").read()
R = [
    ('''def read(path, ollama_url, model="qwen2.5vl:7b", fewshot_dir=None, px=1600, timeout=900, keep_alive="30m", prompt=None):''',
     '''UPRIGHT_MIN_CONF = 2.0


def upright(png):
    """531: turn a sideways / upside-down page image upright in place (Tesseract OSD). Returns the degrees turned."""
    try:
        out = subprocess.run(["tesseract", png, "stdout", "--psm", "0"], capture_output=True, text=True, timeout=120).stdout
        rot = re.search(r"Rotate:\\s*(\\d+)", out)
        conf = re.search(r"Orientation confidence:\\s*([\\d.]+)", out)
        deg = int(rot.group(1)) if rot else 0
        if deg not in (90, 180, 270) or not conf or float(conf.group(1)) < UPRIGHT_MIN_CONF:
            return 0
        from PIL import Image
        with Image.open(png) as im:
            im.rotate(-deg, expand=True).save(png)
        return deg
    except Exception:
        return 0


def salvage(raw):
    """531: an answer cut off part way (not JSON): the header and the line items read before the cut, or None."""
    i = raw.find('"line_items"')
    if i < 0:
        return None
    for j in range(len(raw) - 1, i, -1):
        if raw[j] != "}":
            continue
        for tail in ("]}", "}]}", "]}}"):
            try:
                d = json.loads(raw[: j + 1] + tail)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("line_items"), list):
                d["_salvaged"] = True
                return d
    return None


def read(path, ollama_url, model="qwen2.5vl:7b", fewshot_dir=None, px=1600, timeout=900, keep_alive="30m", prompt=None):'''),
    ('''        if not pngs:
            raise RuntimeError("no pages could be rendered")
        body = {"model": model, "stream": False, "format": ANSWER_SCHEMA, "keep_alive": keep_alive,
                "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
                "messages": fewshot_msgs(fewshot_dir, text) + [{"role": "user", "content": text,
                            "images": [base64.b64encode(open(pngs[0], "rb").read()).decode()]}]}
        req = urllib.request.Request(ollama_url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read())["message"]["content"]
    return tidy(parse_answer(raw)), 1, raw''',
     '''        if not pngs:
            raise RuntimeError("no pages could be rendered")
        turned = upright(pngs[0])                            # 531: upside-down scans read upright
        img = base64.b64encode(open(pngs[0], "rb").read()).decode()

        def ask(examples):
            body = {"model": model, "stream": False, "format": ANSWER_SCHEMA, "keep_alive": keep_alive,
                    "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
                    "messages": fewshot_msgs(examples, text) + [{"role": "user", "content": text, "images": [img]}]}
            req = urllib.request.Request(ollama_url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())["message"]["content"]

        raw = ask(fewshot_dir)
        try:
            d = parse_answer(raw)
        except (ValueError, RuntimeError):
            d = None                                         # 531: not JSON - once more without the worked examples
            if fewshot_dir:
                raw2 = ask(None)
                try:
                    d, raw = parse_answer(raw2), raw2
                except (ValueError, RuntimeError):
                    pass
            if d is None:
                d = salvage(raw)
            if d is None:
                raise RuntimeError(f"model returned non-JSON twice: {raw[:200]!r}")
    a = tidy(d)
    if turned:
        a["_turned_upright"] = turned
    if d.get("_salvaged"):
        a["_salvaged"] = True
    return a, 1, raw'''),
]
for old, new in R:
    if s.count(old) != 1:
        sys.exit(f"jt_reader: expected 1 match, found {s.count(old)}: {old[:70]!r}")
    s = s.replace(old, new)
open(jr, "w", encoding="utf-8", newline="\n").write(s)

w = open(wk, encoding="utf-8").read()
old = '''                            raw = jt_reader.to_worker(ans, jc.get("number_prefix"))
                        except PageReadFailed as e:'''
new = '''                            raw = jt_reader.to_worker(ans, jc.get("number_prefix"))
                            if ans.get("_salvaged"):            # 531: answer cut off; the lines before the cut kept
                                extra["read_error"] = "answer cut off: the lines read before the cut are kept - check the rest"
                        except (PageReadFailed, ValueError, RuntimeError) as e:     # 531: a page, not the whole scan'''
if w.count(old) != 1:
    sys.exit(f"worker: expected 1 match, found {w.count(old)}")
open(wk, "w", encoding="utf-8", newline="\n").write(w.replace(old, new))
print("patched", jr, wk)

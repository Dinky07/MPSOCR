#!/usr/bin/env python3
"""453 (Dino 3 Nov "build it and test it"): a sideways scanned test cert (2270, 0059: the table read half or made up)
is turned upright before the model sees it. Each rendered page goes through Tesseract's orientation check (OSD, --psm 0);
when it says the page is turned 90 / 180 / 270 with confidence UPRIGHT_MIN_CONF or more, the image is rotated (Pillow).
An upright page, a page Tesseract can't judge or any error: the image is left as it is. The prompt is unchanged.
    tc_upright_patch.py <tc_reader.py>"""
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
for old, new in (
    ('''def read_page(path, page, ollama_url, model, fewshot_dir, family=None, px=1600, hint=None, timeout=1200,''',
     '''UPRIGHT_MIN_CONF = 2.0


def upright(png):
    """453: rotate a sideways page image upright in place (Tesseract OSD). Returns the degrees turned, 0 if none."""
    try:
        r = run(["tesseract", png, "stdout", "--psm", "0"], 120)
        out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else str(r.stdout or "")
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


def read_page(path, page, ollama_url, model, fewshot_dir, family=None, px=1600, hint=None, timeout=1200,'''),
    ('''            png = render(path, page, px, os.path.join(tmp, "p.png"))
        else:
            png = str(path)
''', '''            png = render(path, page, px, os.path.join(tmp, "p.png"))
        else:
            png = shutil.copy(str(path), os.path.join(tmp, "p" + os.path.splitext(str(path))[1]))
        turned = upright(png)                                 # 453: sideways scans read upright
'''),
    ('''            raw = json.loads(r.read())["message"]["content"]
    return parse_answer(raw)
''', '''            raw = json.loads(r.read())["message"]["content"]
    a = parse_answer(raw)
    if turned and isinstance(a, dict):
        a["turned_upright"] = turned
    return a
'''),
    ('''import argparse, base64, glob, json, os, re, subprocess, sys, tempfile, urllib.request''',
     '''import argparse, base64, glob, json, os, re, shutil, subprocess, sys, tempfile, urllib.request'''),
):
    n = s.count(old)
    if n != 1:
        sys.exit(f"expected 1 match, found {n}: {old[:70]!r}")
    s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("patched", p)

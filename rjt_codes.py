#!/usr/bin/env python3
"""Ronin JT shadow pass (Dino 10 Nov 2026): the trust status of a stock code the Ronin JT model produced.
  red    no code, or the code is not on the stock list
  amber  on the list, but the description read contradicts its stock description, the quantity differs from the reader's,
         or the two read a different number of lines
  green  on the list and nothing contradicts it
A code is never changed: on the 212 scored lines the single stock code one character away was wrong 8 times of 13."""
import csv
import re

GROUPS = {
    "wall": [("light", r"\bSTD\b|\bST[O09]\b|\bS(?:CH|ET|C|=)?\s*[+=\-]*\s*40\b"),
             ("heavy", r"\bX-?S\b|\bS(?:CH|ET|C|=)?\s*[+=\-]*\s*80\b"),
             ("extra", r"\bX\s*X\s*S\b|\bS(?:CH|ET|C|=)?\s*[+=\-]*\s*160\b"),
             ("jis", r"\bJIS\b"),
             ("s10", r"\bS(?:CH|ET|C|=)?\s*[+=\-]*\s*10\b"),
             ("s20", r"\bS(?:CH|ET|C|=)?\s*[+=\-]*\s*20\b")],
    "face": [("rf", r"\bR\s*[./]?\s*F(?=$|[^A-Z]|S[BOT]\b)"), ("ff", r"\bF\s*[./]?\s*F(?=$|[^A-Z])")],
    "form": [("so", r"\bS\s*[./]\s*O\b|\bSO\b|\bSLIP\b"),
             ("sb", r"\bS\s*[./]?\s*B\b|STEP\s*BORE|\bST\b|\bRFS[BT]\b"),
             ("wn", r"\bW\s*[./]?\s*N\b|WELD\s*NECK"),
             ("bl", r"\bBL(?:ANK|IND)?\b|\bBLK\b")],
    "radius": [("lr", r"\bL\s*[./]?\s*R\b|\b1[.,]?5\s*D\b"), ("3d", r"\b3\s*D\b"), ("5d", r"\b5\s*D\b")],
    "reducer": [("ecc", r"\bECC"), ("con", r"\bCON\b|\bC\s*/\s*R(?:E?D)?\b|\bC\s*/\s*F\b")],
}
ON = tuple(GROUPS) + ("angle", "class", "bore")


def norm(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def num(v):
    try:
        return None if v in (None, "") else round(float(re.sub(r"[^\d.]", "", str(v)) or "x"), 2)
    except ValueError:
        return None


def stock_list(path):
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for r in csv.reader(fh, delimiter="|"):
            if r and r[0].strip() and r[0].strip().upper() != "CODE":
                out[norm(r[0])] = r[1].strip() if len(r) > 1 else ""
    return out


def attrs(text):
    t = " ".join(str(text or "").upper().split())
    a = {g: {v for v, rx in opts if re.search(rx, t)} for g, opts in GROUPS.items()}
    a["angle"] = set(re.findall(r"\b(90|60|45|30|22|11)\s*(?:°|DEG)", t))
    a["class"] = {f"{x}/{y}" for x, y in re.findall(r"\b(\d{2,4})\s*/\s*([234])\b", t)}
    a["class"] |= {f"{x}#" for x in re.findall(r"\b(150|300|600|900|1500|2500)\s*#", t)}
    a["class"] |= {f"{x}#" for x in re.findall(r"\bASA\s*(\d{3,4})", t)}
    a["bore"] = {str(int(x)) for x in re.findall(r"\b(\d{2,4})\s*(?:NB\b|N\s*[./]?\s*[3B]\b|W\s*/\s*3\b)", t)}
    return a


def conflicts(texts, stock_desc, groups=ON):
    """Groups where the texts read name a value and none of them agrees with the stock description."""
    s = attrs(stock_desc)
    read = {g: set() for g in groups}
    for t in texts:
        a = attrs(t)
        for g in groups:
            read[g] |= a[g]
    return [g for g in groups if read[g] and s[g] and not (read[g] & s[g])]


def judge(model_line, reader_line, stock, same_count, groups=ON):
    """-> (status, reasons) for one line. reader_line is None when the two read a different number of lines."""
    code = norm(model_line.get("item_no"))
    if not code:
        return "red", ["no code"]
    if code not in stock:
        return "red", ["not on the stock list"]
    why, texts = [], [model_line.get("item")]
    if not same_count:
        why.append("line count differs from the reader")
    if reader_line is not None:
        texts.append(reader_line.get("description") or reader_line.get("item"))
        if num(model_line.get("qty")) != num(reader_line.get("quantity", reader_line.get("qty"))):
            why.append("quantity differs from the reader")
    why += [f"{g} contradicts the stock description" for g in conflicts(texts, stock[code], groups)]
    return ("amber" if why else "green"), why


def judge_jt(model_lines, reader_lines, stock, groups=ON):
    same = len(model_lines) == len(reader_lines)
    return [judge(m, reader_lines[j] if same else None, stock, same, groups) for j, m in enumerate(model_lines)]

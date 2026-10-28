#!/usr/bin/env python3
"""Workshop-sheet stamp: A/B of the close date reading on the 40 stamp crops in work/ws_stamp/crops, labelled by eye
(27 Oct). Prompt v2: no example dates, says the DATE space is often blank, asks whether this is the MPS IFC stamp at all
(colour-printed MPS logos, a client's acceptance stamp and red-pen notes were picked up as "red ink" in the dry run).
Read-only: prints per crop and a summary per model, writes reports/ws stamp ab <stamp>.csv.

    python3 ws_stamp_ab.py [--models qwen2.5vl:7b,qwen3-vl:8b-instruct]
"""
import argparse, base64, csv, json, re, sys, time, urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path("/srv/ocr/scans/pipeline")
CFG = json.load(open("/srv/ocr/pipeline/config.json"))
CROPS = ROOT / "work" / "ws_stamp" / "crops"

# by eye, 27 Oct: "blank" = the MPS stamp with no date written; a date = what is handwritten in the stamp;
# "logo" / "other stamp" / "red pen" = not the MPS stamp at all
LOGO, OTHER, PEN, BLANK = "logo", "other stamp", "red pen", "blank"
TRUTH = {
    "10006E-WS-HH01-SHT1of2-05-09-2025": BLANK, "10006E-WS-HH02-SHT2of2-05-09-2025": BLANK,
    "10006E-WS-HH09-SHT9of14-07-09-2023": BLANK, "10387E-WS-GN01-SHT2of5-19-01-2024": "2024-01-23",
    "10387E-WS-GN02-SHT2of4-19-01-2024_2": "2024-01-23", "10387E-WS-GN03-SHT3of5-19-01-2024": "2024-01-23",
    "10387E-WS-GN04-SHT1of5-19-01-2024": "2024-01-23", "10387E-WS-GN04-SHT5of5-19-01-2024": "2021-01-23",
    "10649E-WS-1-SHT1of1-10-01-2021": BLANK, "10768E-WS-GN01-SHT1of1-13-03-2024": BLANK,
    "10784E-WS-GN01-SHT1of1-08-01-2024": BLANK, "10784E-WS-GN01-SHT1of1-08-01-2024_2": "2024-01-04",
    "10928E-WS-MS01-SHT1of5-06-03-2024": OTHER, "11021E-WS-GN01-SHT1of2-10-01-2025": BLANK,
    "11021E-WS-GN01-SHT1of35-21-01-2025": "2025-01-21", "11021E-WS-GN01-SHT1of35-30-01-2025": LOGO,
    "11021E-WS-GN02-SHT2of2-10-01-2025": BLANK, "11021E-WS-GN02-SHT2of35-15-01-2025": "2025-01-21",
    "11021E-WS-GN02-SHT2of35-30-01-2025": LOGO, "11021E-WS-GN03-SHT3of35-30-01-2025": LOGO,
    "11021E-WS-GN04-SHT4of35-15-01-2025": "2025-01-21", "11021E-WS-GN04-SHT4of35-30-01-2025": LOGO,
    "11021E-WS-GN05-SHT5of35-30-01-2025": LOGO, "11021E-WS-GN05A-SHT5of17-16-01-2025": BLANK,
    "11021E-WS-GN06-SHT6of35-15-01-2025": "2025-01-21", "11021E-WS-GN06-SHT6of35-30-01-2025_2": LOGO,
    "11021E-WS-GN07-SHT7of35-21-01-2025": "2025-01-21", "11021E-WS-GN07-SHT7of35-30-01-2025_2": LOGO,
    "11021E-WS-GN08-SHT8of35-15-01-2025": "2025-01-21", "11021E-WS-GN08-SHT8of35-30-01-2025": LOGO,
    "11021E-WS-GN09-SHT9of11-29-10-2024_2": PEN, "11021E-WS-GN09-SHT9of35-15-01-2025": "2025-01-21",
    "11021E-WS-GN09-SHT9of35-30-01-2025": LOGO, "11021E-WS-GN09-SHT9of35-30-01-2025_2": LOGO,
    "11021E-WS-GN10-SHT10of12-18-11-2024_3": BLANK, "11021E-WS-GN11-SHT11of12-18-11-2024_2": BLANK,
    "11021E-WS-GN11-SHT11of35-15-01-2025": "2025-01-21", "11021E-WS-GN11-SHT11of35-30-01-2025": LOGO,
    "11021E-WS-GN12-SHT12of35-15-01-2025": "2025-01-21", "11021E-WS-GN12-SHT12of35-30-01-2025": LOGO,
}

PROMPT = (
    "This picture is cut from a scanned workshop drawing of Mining Pressure Systems (MPS). MPS's quality stamp is a red "
    "rubber stamp with four lines: MINING PRESSURE SYSTEMS / ISSUED FOR CONSTRUCTION / DATE: ...... SIGN: ...... / "
    "Q A APPROVED.\n"
    "1. is_ifc_stamp: true only if that red stamp (or most of it) is in the picture. A printed company logo, another "
    "company's stamp or red handwriting is not it.\n"
    "2. If it is, look only at the DATE space of the stamp. When a date is handwritten there it is usually in two parts: "
    "the year and month written above the DATE line, and the day written on the dotted line after \"DATE:\". Copy "
    "exactly what is handwritten. Very often nothing is written there (only the printed dots): then date_blank is true "
    "and the date fields are null. Never guess or make up a date; ignore the signature and all printed text.\n"
    "Answer only JSON: {\"is_ifc_stamp\": true or false, \"date_blank\": true or false, \"year_month_written\": text or "
    "null, \"day_written\": text or null, \"date_iso\": \"YYYY-MM-DD\" or null}")


def ask(model, png, num_ctx):
    body = {"model": model, "stream": False, "format": "json", "keep_alive": "5m",
            "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": 200},
            "messages": [{"role": "user", "content": PROMPT, "images": [base64.b64encode(png.read_bytes()).decode()]}]}
    req = urllib.request.Request(CFG["ollama_url"].rstrip("/") + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    raw = json.loads(urllib.request.urlopen(req, timeout=600).read())["message"]["content"]
    try:
        return json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {}


def to_iso(a):
    """The date from the two handwritten parts (year-month above the line, day on it), or a full date written in one
    place (year first, or day-month-year). Year-month alone is not a date. The model's own date_iso is not used: it fills
    in a missing day. OCR-ish slips: O->0, l/I->1."""
    def digits(s):
        return re.sub(r"[^0-9]", " ", str(s or "").upper().replace("O", "0").replace("L", "1").replace("I", "1")).split()
    ym, dd = digits(a.get("year_month_written")), digits(a.get("day_written"))
    try:
        if len(ym) >= 3 and len(ym[0]) == 4:
            return date(int(ym[0]), int(ym[1]), int(ym[2])).isoformat()
        if len(ym) >= 3 and len(ym[2]) == 4:
            return date(int(ym[2]), int(ym[1]), int(ym[0])).isoformat()
        if len(ym) >= 2 and len(ym[0]) == 4 and dd:
            return date(int(ym[0]), int(ym[1]), int(dd[0])).isoformat()
    except ValueError:
        pass
    return None


ap = argparse.ArgumentParser()
ap.add_argument("--models", default="qwen2.5vl:7b,qwen3-vl:8b-instruct")
a = ap.parse_args()
rows, summ = [], {}
for model in a.models.split(","):
    s = {"crops": 0, "stamp_right": 0, "not_stamp_called_stamp": 0, "stamp_missed": 0, "blank_right": 0, "blank_given_date": 0,
         "dated": 0, "date_exact": 0, "date_wrong": 0, "date_missing": 0, "secs": 0}
    t0 = time.time()
    for name, truth in TRUTH.items():
        png = CROPS / (name + ".png")
        if not png.is_file():
            continue
        t = time.time()
        try:
            r = ask(model, png, 49152)
        except Exception as e:
            r = {"error": str(e)[:120]}
        iso = to_iso(r) if r.get("is_ifc_stamp") and not r.get("date_blank") else None
        is_stamp = truth not in (LOGO, OTHER, PEN)
        s["crops"] += 1
        said = bool(r.get("is_ifc_stamp"))
        s["stamp_right"] += said == is_stamp
        s["not_stamp_called_stamp"] += (said and not is_stamp)
        s["stamp_missed"] += (is_stamp and not said)
        if truth == BLANK:
            s["blank_right"] += iso is None
            s["blank_given_date"] += iso is not None
        elif is_stamp:
            s["dated"] += 1
            s["date_exact"] += iso == truth
            s["date_wrong"] += bool(iso) and iso != truth
            s["date_missing"] += not iso
        rows.append({"model": model, "crop": name, "truth": truth, "is_ifc_stamp": r.get("is_ifc_stamp"), "date_blank": r.get("date_blank"),
                     "year_month_written": r.get("year_month_written"), "day_written": r.get("day_written"),
                     "model_date_iso": r.get("date_iso"), "date": iso, "ok": (iso == truth) if truth[:2] == "20" else (iso is None and said == is_stamp),
                     "secs": round(time.time() - t, 1), "error": r.get("error", "")})
        print(f"{model[:12]:12s} {name[:40]:40s} truth={truth:11s} stamp={str(r.get('is_ifc_stamp')):5s} blank={str(r.get('date_blank')):5s} "
              f"{str(r.get('year_month_written'))!s:10s} {str(r.get('day_written'))!s:6s} -> {iso}", flush=True)
    s["secs"] = round(time.time() - t0)
    summ[model] = s
out = ROOT / "reports" / f"ws stamp ab {datetime.now():%Y-%m-%d %H%M}"
with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
print(json.dumps(summ, indent=1))

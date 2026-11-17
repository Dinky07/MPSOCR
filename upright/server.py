#!/usr/bin/env python3
"""mps-upright: which way up is this page?  (17 Nov 2026)

A small HTTP service for the MPS OCR pipeline, running in its own container on the RTX 2070 Super.
Model: PaddleOCR's document orientation classifier PP-LCNet_x1_0_doc_ori (0 / 90 / 180 / 270), through ONNX Runtime.

Every page is asked four times (as it is, and turned 90, 180 and 270 degrees clockwise). Each answer implies an
orientation of the original page; the page is "confident" when at least three of the four agree.

  GET  /health
  POST /ocr     {"pdf": "/scans/...pdf", "pages": [1] (optional: all), "px": 2400 (optional)}   (755, 17 Nov)
        -> {"pages": [{"page": 1, "lines": [{"text": "...", "score": 0.98, "box": [x0, y0, x1, y1]}], "width": w, "height": h}]}
        PaddleOCR PP-OCRv5 server detection + recognition, for the CPU readers' second reading. Loaded on first use.
  POST /orient   {"pdf": "/scans/...pdf", "pages": [1, 2] (optional: all), "px": 768 (optional)}
        -> {"pages": [{"page": 1, "turn": 180, "confident": true, "votes": 4, "labels": [...], "scores": [...]}], "ms": 41}
     "turn" = degrees clockwise the page has to be turned to stand upright (the PDF /Rotate convention).
The scans are mounted read-only; this service never changes a file.
"""
import collections, json, os, sys, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image, ImageDraw
import pypdfium2 as pdfium

MODEL = "PP-LCNet_x1_0_doc_ori"
SIGN = int(os.environ.get("UPRIGHT_SIGN", "1"))      # +1: label = degrees clockwise the content is turned; -1: counter-clockwise
PX = int(os.environ.get("UPRIGHT_PX", "768"))
STATE = {"model": MODEL, "sign": SIGN, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "pages": 0, "requests": 0,
         "turned": {"0": 0, "90": 0, "180": 0, "270": 0}, "not_confident": 0, "errors": 0, "last": None}
LOCK = threading.Lock()
ENGINE = None


PROVIDERS = ["CUDAExecutionProvider", "CPUExecutionProvider"]
OCR = None


def _session_provider(model_dir_glob):
    """The provider a plain ONNX Runtime session on the model gets (what PaddleOCR's own session gets too)."""
    import glob, onnxruntime as ort
    files = glob.glob(model_dir_glob)
    if not files:
        return None
    return ort.InferenceSession(files[0], providers=PROVIDERS).get_providers()[0]


def load():
    """ONNX Runtime with the CUDA provider (the 2070 Super), the CPU when it is not there.
    755: the providers are passed in engine_config. device="gpu:0" (734) went through ONNX Runtime's plugin-device
    lookup, which this build does not have ("No registered plugin EP device found"), and the model ran on the CPU."""
    import onnxruntime as ort
    try:
        ort.preload_dlls()
    except Exception as e:
        STATE["preload_error"] = repr(e)[:200]
    STATE["onnxruntime"] = ort.__version__
    STATE["providers_available"] = ort.get_available_providers()
    from paddleocr import DocImgOrientationClassification
    try:
        m = DocImgOrientationClassification(model_name=MODEL, engine="onnxruntime", engine_config={"providers": PROVIDERS})
    except Exception as e:
        STATE["gpu_error"] = repr(e)[:300]
        m = DocImgOrientationClassification(model_name=MODEL, engine="onnxruntime", engine_config={"providers": ["CPUExecutionProvider"]})
    prov = _session_provider("/models/paddlex/official_models/PP-LCNet_x1_0_doc_ori_onnx/*.onnx")
    STATE["device"] = {"CUDAExecutionProvider": "gpu (2070 Super)", "CPUExecutionProvider": "cpu"}.get(prov, prov or "unknown")
    return m


def ocr_engine():
    global OCR
    if OCR is None:
        from paddleocr import PaddleOCR
        OCR = PaddleOCR(text_detection_model_name="PP-OCRv5_server_det", text_recognition_model_name="PP-OCRv5_server_rec",
                        use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False,
                        engine="onnxruntime", engine_config={"providers": PROVIDERS})
        STATE["ocr_loaded"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return OCR


def ocr_pdf(path, pages=None, px=2400):
    out = []
    with LOCK:
        eng = ocr_engine()
        pdf = pdfium.PdfDocument(path)
        try:
            n = len(pdf)
            for p in (pages or range(1, n + 1)):
                if not 1 <= p <= n:
                    continue
                im = render(pdf, p, px)
                arr = np.ascontiguousarray(np.asarray(im)[:, :, ::-1])
                r = eng.predict(arr)[0]
                d = r.json.get("res", r.json) if hasattr(r, "json") else dict(r)
                lines = [{"text": t, "score": round(float(sc), 4), "box": [int(v) for v in b]}
                         for t, sc, b in zip(d.get("rec_texts", []), d.get("rec_scores", []), d.get("rec_boxes", []))]
                out.append({"page": p, "width": im.size[0], "height": im.size[1], "lines": lines})
                STATE["ocr_pages"] = STATE.get("ocr_pages", 0) + 1
        finally:
            pdf.close()
    return out


def _label(res):
    d = None
    for get in (lambda r: r.json, lambda r: dict(r)):
        try:
            d = get(res)
            break
        except Exception:
            continue
    d = d.get("res", d)
    return int(str(d["label_names"][0])), float(np.asarray(d["scores"]).ravel()[0])


def ask(images):
    """images: PIL images -> [(label, score)], in order."""
    arrs = [np.ascontiguousarray(np.asarray(im.convert("RGB"))[:, :, ::-1]) for im in images]     # BGR, as cv2 reads
    return [_label(r) for r in ENGINE.predict(arrs, batch_size=len(arrs))]


def classify(im):
    """One page image -> dict. The four turns are clockwise (PIL's rotate() is counter-clockwise, hence the minus)."""
    turns = (0, 90, 180, 270)
    got = ask([im if t == 0 else im.rotate(-t, expand=True) for t in turns])
    implied = [(lab - SIGN * t) % 360 for (lab, _), t in zip(got, turns)]
    best, votes = collections.Counter(implied).most_common(1)[0]
    turn = (-SIGN * best) % 360
    return {"turn": turn, "confident": votes >= 3, "votes": votes, "label": best,
            "labels": [g[0] for g in got], "scores": [round(g[1], 4) for g in got]}


def render(pdf, page, px=PX):
    """Page (1-based) as the viewer shows it (the page's own /Rotate applied), long side px."""
    w, h = pdf[page - 1].get_size()
    return pdf[page - 1].render(scale=px / max(w, h)).to_pil().convert("RGB")


def orient_pdf(path, pages=None, px=PX):
    out = []
    with LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            n = len(pdf)
            for p in (pages or range(1, n + 1)):
                if not 1 <= p <= n:
                    continue
                r = classify(render(pdf, p, px))
                r["page"] = p
                out.append(r)
                STATE["pages"] += 1
                STATE["turned"][str(r["turn"])] += r["confident"]
                STATE["not_confident"] += (not r["confident"])
        finally:
            pdf.close()
    return out


def smoke():
    """A drawn page of text, upside down: the answer must be 180 either way round."""
    im = Image.new("RGB", (800, 1100), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 760, 120), outline="black", width=3)
    for i in range(34):
        d.text((60, 60 + i * 30), f"PURCHASE ORDER line {i:02d}  quantity 12  description 200NB x 6mm pipe  price 1234.50", fill="black")
    with LOCK:
        up, down = classify(im), classify(im.rotate(180, expand=True))
    return {"upright": up["turn"], "upside_down": down["turn"], "ok": up["turn"] == 0 and down["turn"] == 180}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, STATE)
        else:
            self._send(404, {"error": "GET /health, POST /orient"})

    def do_POST(self):
        t0 = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not self.path.startswith(("/orient", "/ocr")):
                return self._send(404, {"error": "POST /orient or /ocr"})
            path = os.path.realpath(body.get("pdf") or "")
            if not path.startswith("/scans/") or not os.path.isfile(path):
                return self._send(400, {"error": "pdf must be a file under /scans"})
            if self.path.startswith("/ocr"):
                pages = ocr_pdf(path, body.get("pages"), int(body.get("px") or 2400))
                return self._send(200, {"pages": pages, "ms": int((time.time() - t0) * 1000)})
            pages = orient_pdf(path, body.get("pages"), int(body.get("px") or PX))
            STATE["requests"] += 1
            STATE["last"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pdf": path, "pages": len(pages), "ms": int((time.time() - t0) * 1000)}
            self._send(200, {"pages": pages, "ms": int((time.time() - t0) * 1000)})
        except Exception as e:
            STATE["errors"] += 1
            STATE["last_error"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": repr(e)[:300]}
            traceback.print_exc()
            self._send(500, {"error": repr(e)[:300]})


def main():
    global ENGINE
    ENGINE = load()
    try:
        STATE["smoke"] = smoke()
    except Exception as e:
        STATE["smoke"] = {"ok": False, "error": repr(e)[:300]}
        traceback.print_exc()
    print("mps-upright ready:", json.dumps(STATE), flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8091), H).serve_forever()


if __name__ == "__main__":
    main()

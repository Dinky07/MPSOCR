# mps-upright: page orientation service for the MPS OCR pipeline, on the RTX 2070 Super (17 Nov 2026).
# PaddleOCR's document orientation model (PP-LCNet_x1_0_doc_ori) through ONNX Runtime; CUDA and cuDNN come from PyPI.
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
RUN pip install "onnxruntime-gpu[cuda,cudnn]" paddleocr pypdfium2 pillow numpy
# make the pip-installed NVIDIA libraries findable by the CUDA provider
RUN python - <<'PY'
import glob, site, os
dirs = sorted({os.path.dirname(p) for sp in site.getsitepackages() for p in glob.glob(sp + "/nvidia/*/lib/*.so*")})
open("/etc/ld.so.conf.d/nvidia-pip.conf", "w").write("\n".join(dirs) + "\n")
print("\n".join(dirs))
PY
RUN ldconfig
WORKDIR /app
COPY server.py evaltest.py /app/
EXPOSE 8091
CMD ["python", "/app/server.py"]

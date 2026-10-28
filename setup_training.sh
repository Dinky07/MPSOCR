#!/bin/bash
# One-off: the Python environment for the full training job (fine-tuning the PO reader on the GPU).
#   sudo bash setup_training.sh [VENV=/srv/ocr/train-venv] [USER that runs the dashboard]
# Needs: the NVIDIA driver (already there for Ollama), ~40 GB free disk (base model + merged copy), 32 GB RAM advised.
set -euo pipefail
VENV=${1:-/srv/ocr/train-venv}
OWNER=${2:-$(stat -c %U /srv/ocr/pipeline/state.db)}

echo "== checks"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || { echo "no NVIDIA GPU visible"; exit 1; }
free -g | awk '/Mem:/ {print "RAM: " $2 " GB" ($2 < 30 ? "  (under 32 GB: merging the fine-tuned model may be slow or fail)" : "")}'
df -BG --output=avail "$(dirname "$VENV")" | tail -1 | awk '{print "free disk: " $1}'
command -v python3 >/dev/null && python3 --version

echo "== creating $VENV (owner: $OWNER)"
apt-get install -y python3-venv python3-dev build-essential >/dev/null
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel >/dev/null
echo "== installing Unsloth + PyTorch (CUDA) - several GB, takes a while"
"$VENV/bin/pip" install unsloth
"$VENV/bin/python" - <<'EOF'
import torch
print("torch", torch.__version__, "CUDA available:", torch.cuda.is_available(),
      torch.cuda.get_device_name(0) if torch.cuda.is_available() else "")
EOF
mkdir -p /srv/ocr/hf-cache
chown -R "$OWNER" "$VENV" /srv/ocr/hf-cache
echo "export HF_HOME=/srv/ocr/hf-cache" > "$VENV/hf.env"
echo "== done. The dashboard's Training tab can now run the full training job."

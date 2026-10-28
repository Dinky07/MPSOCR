#!/usr/bin/env python3
"""Weekly training queue (systemd timer mps-ocr-training.timer, Saturdays 17:00; Dino 26 Oct: train once a week on the
week's scans, after he has reviewed the training data; ad hoc runs mid-week as needed).

In order, each on everything checked so far (the pools grow with every week's scans):
  1. import the PO checking files: every row marked Use in any checking file is written as Checked (the same as the 19:30
     import; rows marked during the week after their day's import are picked up here)
  2. purchase orders   training.py quick      worked examples, kept only if line qty + price improve on the test orders
  3. workshop sheets   ws_training.py quick
  4. job transfers     jt_training.py quick
     test certificates tc_training.py quick   (heat numbers against the old site, checked certs)
  5. PO numbers pass   numbers_run.sh          fine-tune the line quantity / price model, score it (not live: report only)
Each quick job checks for itself whether there is anything new to learn from and skips quietly when there isn't; a new
setup is only installed when it scores better on the test documents. Nothing starts while another training job runs.

    python3 nightly_training.py                    # what the timer runs
    python3 nightly_training.py --dry-run          # only say what would run
    python3 nightly_training.py --only po,numbers  # ad hoc: some of the steps (import, po, ws, jt, tc, numbers)
"""
import argparse, json, os, subprocess, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("MPS_OCR_CONFIG", str(HERE / "config.json")))
STEPS = [("import", ["po_daily.py", "import", "--apply"], "PO checking files"),
         ("po", ["training.py", "quick"], "purchase orders"),
         ("ws", ["ws_training.py", "quick"], "workshop sheets"),
         ("jt", ["jt_training.py", "quick"], "job transfers"),
         ("tc", ["tc_training.py", "quick"], "test certificates"),
         ("numbers", ["numbers_run.sh"], "PO numbers pass (line quantities and prices)")]


def running(tdir):
    try:
        j = json.loads((tdir / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if j.get("state") != "running":
        return None
    try:
        os.kill(int(j.get("pid")), 0)
    except (OSError, ValueError, TypeError):
        return None                    # left "running" by a job that died (box restarted): not running
    return j.get("kind") or "training"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="comma list of steps: " + ", ".join(k for k, _, _ in STEPS))
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    tdir = Path(cfg["root"]) / "training"
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} training queue" + (f" (only {', '.join(sorted(only))})" if only else ""), flush=True)
    for key, cmd, what in STEPS:
        if only and key not in only:
            continue
        script = HERE / cmd[0]
        if not script.exists():
            print(f"  {what}: {cmd[0]} not installed, skipped", flush=True)
            continue
        busy = running(tdir) if key != "import" else None
        if busy:
            print(f"  {what}: a {busy} job is running, skipped", flush=True)
            continue
        if a.dry_run:
            print(f"  {what}: would run {' '.join(cmd)}", flush=True)
            continue
        print(f"{datetime.now():%H:%M:%S}  {what}: {' '.join(cmd)}", flush=True)
        exe = ["/bin/bash", str(script)] if cmd[0].endswith(".sh") else [sys.executable, str(script)]
        env = dict(os.environ, MPS_OCR_CONFIG=str(CONFIG), IN_QUEUE="1")
        r = subprocess.run(exe + cmd[1:], cwd=str(HERE), env=env)
        if key in ("po", "ws", "jt", "tc"):
            sub = tdir
            j = {}
            try:
                j = json.loads((sub / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            print(f"  {what}: exit {r.returncode}; {j.get('state', '?')}: {j.get('summary', '')}", flush=True)
        else:
            print(f"  {what}: exit {r.returncode}", flush=True)
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} training queue done", flush=True)


if __name__ == "__main__":
    main()

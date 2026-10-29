#!/usr/bin/env bash
# 335: the weekly queue's numbers step. The retrain runs in its own unit (mps-numbers-weekly.service, up to Monday 03:00:
# Dino 29 Oct "training can be from Saturday 17:00 to Monday 03:00, there is no time limit"), so the training queue's
# own time limit doesn't cut it short. This only starts it; numbers_run.sh pauses and resumes the worker.
if systemctl is-active --quiet mps-numbers-weekly.service; then echo "mps-numbers-weekly already running"; exit 0; fi
sudo -n /usr/bin/systemctl start mps-numbers-weekly.service && echo "mps-numbers-weekly started: journalctl -u mps-numbers-weekly"
systemctl is-active mps-numbers-weekly.service

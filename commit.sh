#!/bin/bash
# commit.sh "<message>": records the pipeline folder's current state (used at the end of runner scripts; hourly auto-commit)
cd /srv/ocr/pipeline && git add -A >/dev/null 2>&1 && git diff --cached --quiet || git commit -q -m "${1:-auto-commit $(date +%F_%H:%M)}" && git log --oneline -1

#!/bin/bash
# commit.sh "<message>": records the pipeline folder's current state (used at the end of runner scripts; hourly auto-commit),
# then pushes main to GitHub Dinky07/MPSOCR (best effort: a failed push never fails the caller; the next run retries it)
cd /srv/ocr/pipeline || exit 1
git add -A >/dev/null 2>&1
git diff --cached --quiet || git commit -q -m "${1:-auto-commit $(date +%F_%H:%M)}" || exit 1
git log --oneline -1
timeout 60 git push -q github HEAD:main >/dev/null 2>&1 || echo "commit.sh: GitHub push failed, retried at the next commit" >&2
exit 0

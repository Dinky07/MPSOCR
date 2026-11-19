#!/bin/bash
# commit.sh "<message>": records the pipeline folder's current state (used at the end of runner scripts; hourly auto-commit),
# then brings GitHub Dinky07/MPSOCR and the mirrors up to date. Best effort: a failed push never fails the caller, the
# next run tries again.
#
# Since 18 Nov changes also arrive the other way (pull requests merged on GitHub), so the box and GitHub can each hold
# commits the other lacks. This script never brings GitHub's commits in (that is a deploy: ops/deploy.sh); it says what
# it sees, and when the two have gone separate ways it leaves flags/alert_git for the dashboard's Alerts card until
# ops/deploy.sh has joined them.
cd /srv/ocr/pipeline || exit 1
FLAG="${MPS_FLAGS:-/srv/ocr/scans/pipeline/flags}/alert_git"
git add -A >/dev/null 2>&1
git diff --cached --quiet || git commit -q -m "${1:-auto-commit $(date +%F_%H:%M)}" || exit 1
git log --oneline -1

alert() {   # first line = when (the Alerts card's format), then what to do
    [ -d "$(dirname "$FLAG")" ] && [ ! -e "$FLAG" ] && { date -Is; echo "$1"; } > "$FLAG"
    echo "commit.sh: $1" >&2
}

if timeout 60 git fetch -q github main 2>/dev/null; then
    behind=$(git rev-list --count HEAD..github/main 2>/dev/null || echo 0)
    ahead=$(git rev-list --count github/main..HEAD 2>/dev/null || echo 0)
    if [ "$behind" -gt 0 ] && [ "$ahead" -gt 0 ]; then
        alert "git: the box has $ahead commit(s) GitHub lacks and GitHub has $behind the box lacks, so nothing can be pushed. Run /srv/ocr/pipeline/ops/deploy.sh (dry run), then with --apply."
    elif [ "$behind" -gt 0 ]; then
        echo "commit.sh: GitHub main has $behind commit(s) not deployed here (ops/deploy.sh)" >&2
        rm -f "$FLAG"
    elif [ "$ahead" -gt 0 ]; then
        if timeout 60 git push -q github HEAD:main >/dev/null 2>&1; then rm -f "$FLAG"
        else echo "commit.sh: GitHub push failed, retried at the next commit" >&2; fi
    else
        rm -f "$FLAG"
    fi
else
    echo "commit.sh: GitHub not reachable, retried at the next commit" >&2
fi

# the mirrors on the box and the NAS follow the box (they had stood still since 17 Nov: nothing pushed to them)
for r in box origin; do
    git remote | grep -qx "$r" || continue
    timeout 60 git push -q "$r" HEAD:main >/dev/null 2>&1 || echo "commit.sh: mirror '$r' not updated" >&2
done
exit 0

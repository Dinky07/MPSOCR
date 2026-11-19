#!/usr/bin/env bash
# Bring what was merged on GitHub (Dinky07/MPSOCR, main) into the live pipeline folder on mps-frigate.
# Replaces the hand-typed "git pull github main && sudo systemctl restart ..." of the pull requests.
#
#   /srv/ocr/pipeline/ops/deploy.sh                      # dry run: what would come in, what would restart; changes nothing
#   /srv/ocr/pipeline/ops/deploy.sh --apply              # does it
#   /srv/ocr/pipeline/ops/deploy.sh --apply --restart-workers
#
# --apply, in order:
#   1. every incoming .py is compiled as it is on GitHub; one that does not compile stops the deploy before anything changes
#   2. what is uncommitted here is committed (as commit.sh does), so nothing local can be lost
#   3. GitHub's commits are merged in (no rebase: the history the mirrors hold stays valid). A conflict undoes the
#      merge and stops, with the files named
#   4. the dashboard is restarted when a .py changed, the collector when collector.py did. The two workers are only
#      restarted with --restart-workers (a restart lets the page being read finish and puts the file back in the
#      queue); without it the command is printed. Changed units/ are named, not installed
#   5. the result is pushed to GitHub and the mirrors, and flags/alert_git is cleared
# Run as dinoc (the runner's user). Needs nothing the runner's sudoers does not already allow (systemctl on mps-* units).
set -u
P=/srv/ocr/pipeline
FLAG="${MPS_FLAGS:-/srv/ocr/scans/pipeline/flags}/alert_git"
APPLY=0; WORKERS=0
for a in "$@"; do
    case "$a" in
        --apply) APPLY=1 ;;
        --restart-workers) WORKERS=1 ;;
        *) echo "usage: deploy.sh [--apply] [--restart-workers]"; exit 2 ;;
    esac
done
say() { echo "deploy: $*"; }
cd "$P" || exit 1

timeout 60 git fetch -q github main || { say "GitHub not reachable: nothing done"; exit 1; }
incoming=$(git rev-list --count HEAD..github/main)
ahead=$(git rev-list --count github/main..HEAD)
dirty=$(git status --porcelain | wc -l)
say "here:   $(git log --oneline -1)"
say "GitHub: $(git log --oneline -1 github/main)"
say "$incoming commit(s) to bring in; $ahead commit(s) here that GitHub lacks; $dirty uncommitted file(s) here"

if [ "$incoming" -eq 0 ]; then
    say "nothing to deploy"
    if [ "$APPLY" -eq 1 ] && [ $((ahead + dirty)) -gt 0 ]; then ./commit.sh "auto-commit $(date +%F_%H:%M) (deploy.sh)"; fi
    exit 0
fi

git log --oneline HEAD..github/main | sed 's/^/deploy:   + /'
base=$(git merge-base HEAD github/main)
mapfile -t changed < <(git diff --name-only "$base" github/main)
printf 'deploy:   file %s\n' "${changed[@]}"

need_dash=0; need_coll=0; need_work=0; units=0
for f in "${changed[@]}"; do
    case "$f" in
        tests/*|ops/*|.github/*|*.md|.gitignore) ;;
        units/*) units=1 ;;
        dashboard.py) need_dash=1 ;;
        collector.py) need_coll=1; need_dash=1 ;;
        *.py) need_dash=1; need_work=1 ;;                 # the dashboard imports readers and sorters too
        *) need_work=1 ;;                                 # prompts, rules, config.json
    esac
done
[ "$need_dash" -eq 1 ] && say "restart: mps-ocr-dashboard"
[ "$need_coll" -eq 1 ] && say "restart: mps-ocr-collector"
[ "$need_work" -eq 1 ] && say "workers read changed files: mps-ocr-worker, mps-ocr-cpu-worker $([ "$WORKERS" -eq 1 ] && echo '(restarted: --restart-workers)' || echo '(NOT restarted without --restart-workers)')"
[ "$units" -eq 1 ] && say "units/ changed: install those with mps-unit-install; this script does not"

# 1. incoming Python must compile, checked from GitHub's copy before anything here changes
bad=0
for f in "${changed[@]}"; do
    case "$f" in *.py) ;; *) continue ;; esac
    git cat-file -e "github/main:$f" 2>/dev/null || continue          # deleted on GitHub
    if ! git show "github/main:$f" | python3 -c 'import sys; compile(sys.stdin.read(), sys.argv[1], "exec")' "$f"; then
        say "does not compile: $f"; bad=1
    fi
done
[ "$bad" -eq 1 ] && { say "stopped: nothing changed"; exit 1; }

if [ "$APPLY" -eq 0 ]; then say "dry run: nothing changed. Again with --apply to do it"; exit 0; fi

# 2. commit what is uncommitted here
git add -A >/dev/null 2>&1
git diff --cached --quiet || git commit -q -m "auto-commit $(date +%F_%H:%M) (before deploy)" || { say "could not commit local changes: stopped"; exit 1; }
before=$(git rev-parse HEAD)

# 3. merge
if ! git merge -q --no-edit github/main >/dev/null 2>&1; then
    say "conflict: the box and GitHub changed the same lines in:"
    git diff --name-only --diff-filter=U | sed 's/^/deploy:   /'
    git merge --abort
    say "merge undone; the folder is as it was ($(git log --oneline -1)). Settle those files in a pull request, then deploy again"
    exit 1
fi
say "merged: $(git log --oneline -1)   (to go back: git reset --hard ${before:0:7}, then restart the services)"

# 4. restarts
rs() { sudo -n /usr/bin/systemctl restart "$1" && say "restarted $1" || say "RESTART FAILED: $1"; }
[ "$need_dash" -eq 1 ] && rs mps-ocr-dashboard
[ "$need_coll" -eq 1 ] && rs mps-ocr-collector
if [ "$need_work" -eq 1 ]; then
    if [ "$WORKERS" -eq 1 ]; then rs mps-ocr-worker; rs mps-ocr-cpu-worker
    else say "workers still run the old code: sudo systemctl restart mps-ocr-worker mps-ocr-cpu-worker"; fi
fi

# 5. push
if timeout 60 git push -q github HEAD:main >/dev/null 2>&1; then rm -f "$FLAG"; say "GitHub main = $(git rev-parse --short HEAD)"
else say "push to GitHub failed: commit.sh tries again within the hour"; fi
for r in box origin; do
    git remote | grep -qx "$r" || continue
    timeout 60 git push -q "$r" HEAD:main >/dev/null 2>&1 || say "mirror '$r' not updated"
done
exit 0

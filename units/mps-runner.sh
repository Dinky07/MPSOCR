#!/usr/bin/env bash
# mps-runner: executes *.sh dropped in /srv/ocr/scans/pipeline/_src/run (the O:\pipeline\_src\run share folder)
# as dinoc (review F11, 28 Oct; root until then), one at a time, writing <name>.log and <name>.rc beside them in run/done/. Started and stopped by hand:
#   sudo systemctl start mps-runner     sudo systemctl stop mps-runner
# A file named STOP in the run folder also stops it. Only dinoc can write to the share.
set -u
RUN=/srv/ocr/scans/pipeline/_src/run
DONE=$RUN/done
mkdir -p "$DONE"
echo "$(date -Is) mps-runner started, watching $RUN"
while true; do
    if [ -e "$RUN/STOP" ]; then rm -f "$RUN/STOP"; echo "$(date -Is) STOP file seen, exiting"; exit 0; fi
    for f in "$RUN"/*.sh; do
        [ -e "$f" ] || continue
        case "$(basename "$f")" in mps-runner.sh|install-runner.sh) continue;; esac
        # wait until the file has not been written for 3 s (SMB copies arrive in pieces)
        age=$(( $(date +%s) - $(stat -c %Y "$f") ))
        [ "$age" -ge 3 ] || continue
        name=$(basename "$f" .sh)
        log="$DONE/$name.log"; rc="$DONE/$name.rc"
        rm -f "$log" "$rc"
        echo "$(date -Is) running $name" | tee -a "$log"
        ( cd /srv/ocr && bash "$f" ) >> "$log" 2>&1
        code=$?
        echo "$(date -Is) finished $name rc=$code" | tee -a "$log"
        echo "$code" > "$rc"
        mv -f "$f" "$DONE/$name.sh"
        true
    done
    sleep 2
done

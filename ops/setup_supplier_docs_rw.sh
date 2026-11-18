#!/usr/bin/env bash
# Give the collector a READ-WRITE mount of just Chronoscan\Supplier Docs MPS, so it can move imported
# invoices into its imported\ folder. Everything else on the NAS stays read-only (/mnt/nas-scans is not touched).
#
# Run on mps-frigate:
#   sudo bash setup_supplier_docs_rw.sh            # dry run: shows what it would do, changes nothing
#   sudo bash setup_supplier_docs_rw.sh --apply    # does it
#
# What --apply does:
#   1. adds one line to /etc/fstab (backup first): the Supplier Docs MPS folder at /mnt/nas-supplier-docs,
#      same NAS account and options as /mnt/nas-scans, but rw
#   2. mounts it and checks dinoc can write there (creates imported\ if missing; writes and removes one test file)
#   3. points the Supplier Docs MPS collector source in the live config.json at the new mount (backup first)
#   4. restarts mps-ocr-collector
# If the write check fails (NAS permission not given yet), it stops after step 2 and leaves config.json alone;
# the new mount stays in fstab, so just re-run with --apply after fixing the permission.
#
# Before --apply: on the Synology (DSM > File Station > SynologyShare > ScanToFile > Chronoscan >
# "Supplier Docs MPS" > Properties > Permission), give the NAS account this script prints Read & Write
# on that folder only (apply to this folder, sub-folders and files).
set -euo pipefail

APPLY=0; [[ "${1:-}" == "--apply" ]] && APPLY=1
SRC_MNT=/mnt/nas-scans
SUB="ScanToFile/Chronoscan/Supplier Docs MPS"
NEW_MNT=/mnt/nas-supplier-docs
CONFIG=/srv/ocr/pipeline/config.json
SVC=mps-ocr-collector
RUN_AS=dinoc

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
say() { echo "== $*"; }
do_() { if [[ $APPLY -eq 1 ]]; then "$@"; else echo "   (dry run) $*"; fi; }

# --- 1. the existing read-only scans mount ---------------------------------------------------------------
line=$(awk -v m="$SRC_MNT" '$1 !~ /^#/ && $2 == m {print; exit}' /etc/fstab || true)
if [[ -z "$line" ]]; then
  echo "No /etc/fstab line for $SRC_MNT. It is mounted some other way:"; findmnt "$SRC_MNT" || true
  echo "Stopping: send this output back to Claude."; exit 1
fi
read -r dev _ fstype opts _ <<<"$line"
[[ "$fstype" == "cifs" ]] || { echo "$SRC_MNT is $fstype, not cifs; stopping"; exit 1; }
say "current: $line"

cred=$(tr ',' '\n' <<<"$opts" | sed -n 's/^credentials=//p')
if [[ -n "$cred" && -r "$cred" ]]; then
  say "NAS account for the permission on the Synology: $(sed -n 's/^username=//p' "$cred")   (from $cred; password not shown)"
else
  say "NAS account: see the username= in the mount options or credentials file (not readable here)"
fi

new_opts=$(tr ',' '\n' <<<"$opts" | grep -vxE 'ro|rw' | paste -sd, -),rw
[[ ",$new_opts," == *",uid="* ]] || new_opts="$new_opts,uid=$(id -u $RUN_AS),gid=$(id -g $RUN_AS)"
new_dev="${dev%/}/${SUB// /\\040}"
new_line="$new_dev $NEW_MNT cifs $new_opts 0 0"
say "new fstab line: $new_line"

if awk -v m="$NEW_MNT" '$1 !~ /^#/ && $2 == m {f=1} END {exit !f}' /etc/fstab; then
  say "fstab already has $NEW_MNT; leaving it"
else
  do_ cp -a /etc/fstab "/etc/fstab.bak-supplierdocs-$(date +%Y%m%d-%H%M%S)"
  if [[ $APPLY -eq 1 ]]; then printf '%s\n' "$new_line" >> /etc/fstab; else echo "   (dry run) append to /etc/fstab"; fi
fi

# --- 2. mount and check writing ----------------------------------------------------------------------------
do_ mkdir -p "$NEW_MNT"
if [[ $APPLY -eq 1 ]]; then
  systemctl daemon-reload
  mountpoint -q "$NEW_MNT" || mount "$NEW_MNT"
  say "mounted: $(findmnt -no SOURCE,OPTIONS "$NEW_MNT")"
  t="$NEW_MNT/imported/.mps-write-test-$$"
  if sudo -u "$RUN_AS" mkdir -p "$NEW_MNT/imported" && sudo -u "$RUN_AS" touch "$t" && sudo -u "$RUN_AS" rm -f "$t"; then
    say "write check OK: $RUN_AS can write to $NEW_MNT/imported"
  else
    say "write check FAILED: give the NAS account Read & Write on 'Supplier Docs MPS' in DSM, then re-run with --apply"
    exit 2
  fi
else
  echo "   (dry run) mount $NEW_MNT and test that $RUN_AS can write to $NEW_MNT/imported"
fi

# --- 3. point the collector source at the rw mount --------------------------------------------------------
say "collector source in $CONFIG -> $NEW_MNT"
do_ cp -a "$CONFIG" "$CONFIG.bak-supplierdocs-$(date +%Y%m%d-%H%M%S)"
python3 - "$CONFIG" "$SRC_MNT/$SUB" "$NEW_MNT" "$APPLY" <<'EOF'
import json, os, sys
path, old, new, apply = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
cfg = json.load(open(path, encoding="utf-8"))
hits = [s for s in cfg.get("collector", {}).get("sources", []) if s.get("path") in (old, new)]
if not hits:
    sys.exit(f"no collector source with path {old!r} in {path}: is PR #2's config there? stopping")
for s in hits:
    print(f"   {s['path']}  ->  {new}")
    s["path"] = new
if apply:                       # write in place so the file keeps its owner and mode
    data = json.dumps(cfg, indent=2, ensure_ascii=False)
    json.loads(data)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
EOF

# --- 4. restart -------------------------------------------------------------------------------------------
do_ systemctl restart "$SVC"
if [[ $APPLY -eq 1 ]]; then
  sleep 5; systemctl --no-pager --lines=15 status "$SVC" || true
  say "done. Files read OK are moved to imported\\ on the collector's next polls (journalctl -u $SVC -f)."
  say "commit the config change: cd /srv/ocr/pipeline && ./commit.sh  (or the hourly auto-commit picks it up)"
else
  say "dry run finished; nothing changed. Run again with --apply."
fi

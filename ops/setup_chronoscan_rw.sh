#!/usr/bin/env bash
# Make the NAS Chronoscan folder writable for the collector, so it can move each scan it has read OK into that
# folder's imported\ sub-folder ("move_after_import" in config.json). The rest of the NAS share stays read-only.
#
# How: a second CIFS mount of just //DS1821plus/SynologyShare/ScanToFile/Chronoscan, read-write, mounted ON TOP of
# /mnt/nas-scans/ScanToFile/Chronoscan. Every path the collector and other scripts use stays the same; only that
# folder becomes writable. If the extra mount is ever missing, the path falls back to the read-only share and the
# collector just logs that it can't move (nothing breaks).
#
# Run on mps-frigate:
#   sudo bash setup_chronoscan_rw.sh            # dry run: shows what it would do, changes nothing
#   sudo bash setup_chronoscan_rw.sh --apply    # does it
#
# --apply: backs up /etc/fstab, adds the one line, mounts it, and checks that dinoc can create and remove a test
# file in Chronoscan\Supplier Docs MPS. It does not change config.json or restart anything.
# NAS side (done 18 Nov): the account in the credentials file has Read & Write on SynologyShare\ScanToFile\Chronoscan.
set -euo pipefail

APPLY=0; [[ "${1:-}" == "--apply" ]] && APPLY=1
SRC_MNT=/mnt/nas-scans
SUB="ScanToFile/Chronoscan"
NEW_MNT="$SRC_MNT/$SUB"
TEST_DIR="$NEW_MNT/Supplier Docs MPS"
RUN_AS=dinoc

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
say() { echo "== $*"; }

# --- the existing read-only share mount ---------------------------------------------------------------------
line=$(awk -v m="$SRC_MNT" '$1 !~ /^#/ && $2 == m {print; exit}' /etc/fstab || true)
[[ -n "$line" ]] || { echo "No /etc/fstab line for $SRC_MNT:"; findmnt "$SRC_MNT" || true; exit 1; }
read -r dev _ fstype opts _ <<<"$line"
[[ "$fstype" == "cifs" ]] || { echo "$SRC_MNT is $fstype, not cifs; stopping"; exit 1; }
say "current: $line"
cred=$(tr ',' '\n' <<<"$opts" | sed -n 's/^credentials=//p')
[[ -n "$cred" && -r "$cred" ]] && say "NAS account: $(sed -n 's/^username=//p' "$cred")   (password not shown)"

# same options, but rw, and file/dir modes that let the owner (dinoc) rename files and make folders;
# no automount of its own (it sits inside the automounted share and is mounted after it)
new_opts=$(tr ',' '\n' <<<"$opts" | grep -vE '^(ro|rw|file_mode=.*|dir_mode=.*|x-systemd\.automount|x-systemd\.idle-timeout=.*)$' | paste -sd, -)
new_opts="rw,$new_opts,file_mode=0664,dir_mode=0775"
[[ ",$new_opts," == *",uid="* ]] || new_opts="$new_opts,uid=$(id -u $RUN_AS),gid=$(id -g $RUN_AS)"
new_line="${dev%/}/$SUB $NEW_MNT cifs $new_opts 0 0"
say "new fstab line: $new_line"

if awk -v m="$NEW_MNT" '$1 !~ /^#/ && $2 == m {f=1} END {exit !f}' /etc/fstab; then
  say "fstab already has $NEW_MNT; leaving it as it is"
elif [[ $APPLY -eq 1 ]]; then
  cp -a /etc/fstab "/etc/fstab.bak-chronoscan-rw-$(date +%Y%m%d-%H%M%S)"
  printf '%s\n' "$new_line" >> /etc/fstab
  say "added to /etc/fstab (backup beside it)"
else
  echo "   (dry run) back up /etc/fstab and append the line above"
fi

if [[ $APPLY -eq 0 ]]; then
  echo "   (dry run) mount $NEW_MNT and check $RUN_AS can write in '$TEST_DIR'"
  say "dry run finished; nothing changed. Run again with --apply."
  exit 0
fi

systemctl daemon-reload
ls "$SRC_MNT/" >/dev/null                          # wake the share's automount first
mountpoint -q "$NEW_MNT" || mount "$NEW_MNT"
say "mounted: $(findmnt -no SOURCE,OPTIONS "$NEW_MNT" | tail -1)"
t="$TEST_DIR/.mps-write-test-$$"
if sudo -u "$RUN_AS" touch "$t" && sudo -u "$RUN_AS" rm -f "$t"; then
  say "write check OK: $RUN_AS can write in '$TEST_DIR'"
else
  say "write check FAILED: check the NAS account's Read & Write on Chronoscan in DSM, then: sudo umount '$NEW_MNT' && sudo mount '$NEW_MNT'"
  exit 2
fi
say "other folders on the share are still read-only: $(findmnt -no OPTIONS "$SRC_MNT" | tail -1 | cut -d, -f1)"
say "done. Next: pull main with the move_after_import config and restart mps-ocr-collector."

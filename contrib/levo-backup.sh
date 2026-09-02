#!/bin/sh
# Keep dated copies of levod's state file. It holds every sale's terms, and a
# project that did not keep its own listing response rebuilds its covenant's
# witness from them, so the file is worth more than its size suggests.
set -eu
STATE="${LEVOD_STATE:-/var/lib/levo/levo-state.json}"
DEST="${LEVO_BACKUP_DIR:-/var/backups/levo}"
KEEP="${LEVO_BACKUP_KEEP:-60}"
mkdir -p "$DEST"
[ -f "$STATE" ] || exit 0
cp -p "$STATE" "$DEST/levo-state-$(date -u +%Y%m%dT%H%M%SZ).json"
# Drop all but the newest $KEEP copies.
ls -1t "$DEST"/levo-state-*.json 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

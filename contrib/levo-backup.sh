#!/bin/sh
# Keep dated copies of levod's state file. It holds every sale's terms, and a
# project that did not keep its own listing response rebuilds its covenant's
# witness from them, so the file is worth more than its size suggests. It also
# holds the allocation ledger, which is nobody's business but its own account's
# -- hence the mode the copies are made with.
set -eu
STATE="${LEVOD_STATE:-/var/lib/levo/levo-state.json}"
DEST="${LEVO_BACKUP_DIR:-/var/backups/levo}"
KEEP="${LEVO_BACKUP_KEEP:-60}"
mkdir -p "$DEST"
chmod 700 "$DEST" 2>/dev/null || true

if [ ! -f "$STATE" ]; then
    # A deployment that has never been written to legitimately has no state
    # file yet. One that HAS copies and no state file is a different thing: the
    # path is wrong, or the file is gone, and a green timer over an empty
    # backup set is exactly the failure a backup exists to prevent.
    if ls "$DEST"/levo-state-*.json >/dev/null 2>&1; then
        echo "levo-backup: $STATE is missing, but $DEST holds copies of it." >&2
        echo "levo-backup: check LEVOD_STATE, or restore the file." >&2
        exit 1
    fi
    echo "levo-backup: nothing at $STATE yet; nothing to copy." >&2
    exit 0
fi

# Copy aside and check it before it takes a name a restore would trust: a
# truncated copy looks exactly like a good one, and the prune below would
# happily push a good one out to keep it.
part="$DEST/.levo-partial.$$"
trap 'rm -f "$part"' EXIT INT TERM
cp -p "$STATE" "$part"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$part" 2>/dev/null || {
    echo "levo-backup: the copy of $STATE is not valid JSON; keeping the older copies." >&2
    exit 1
}
mv "$part" "$DEST/levo-state-$(date -u +%Y%m%dT%H%M%SZ).json"

# Drop all but the newest $KEEP copies.
ls -1t "$DEST"/levo-state-*.json 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

#!/usr/bin/env bash
# Tell a person. Called by systemd through levo-alert@.service when a Levo
# unit fails, and by levo-check.sh when its verdict changes; either way one
# line goes to a push topic a phone or a browser subscribes to, with no
# account and no credential on this box beyond the topic's name.
#
#   contrib/levo-alert.sh "what happened"
#
# /etc/sequentia/levo-alert.env (mode 0600, box-only) holds:
#   NTFY_TOPIC=<a name nobody guesses>        # required; the channel
#   NTFY_URL=https://ntfy.sh                   # optional; a self-hosted ntfy
#   ALERT_PREFIX=levo                          # optional; the message's title
# Subscribe at $NTFY_URL/$NTFY_TOPIC. Without a topic this prints to the
# journal and exits 0, so a box that has not set one up loses nothing but the
# push. LEVO_ALERT_ENV names another file.
set -u
case "${1:-}" in
    -h|--help)
        echo "Usage: contrib/levo-alert.sh <message>"
        echo
        echo "Sends one line to the push topic named in /etc/sequentia/levo-alert.env"
        echo "(or LEVO_ALERT_ENV), and to the journal either way. Exits 0 always: the"
        echo "alert path must never be what takes a unit down a second time."
        exit 0 ;;
esac
ENV="${LEVO_ALERT_ENV:-/etc/sequentia/levo-alert.env}"
[ -r "$ENV" ] && { set -a; . "$ENV"; set +a; }
title="${ALERT_PREFIX:-levo} $(hostname -s)"
msg="${*:-something failed and nothing said what}"
echo "alert: $msg" >&2
if [ -z "${NTFY_TOPIC:-}" ]; then
    echo "alert: no NTFY_TOPIC in $ENV; the journal is the only record" >&2
    exit 0
fi
url="${NTFY_URL:-https://ntfy.sh}/$NTFY_TOPIC"
curl -sS --max-time 15 -H "Title: $title" -H "Priority: high" \
     -d "$msg" "$url" >/dev/null 2>&1 \
  || echo "alert: could not reach $url" >&2
exit 0

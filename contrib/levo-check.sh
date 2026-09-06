#!/usr/bin/env bash
# Is levod doing its job? Not "is the process up" -- systemd answers that, and
# it is the wrong question. A levod whose node has gone away, whose state file
# has gone read-only, or whose watcher thread has stopped goes on answering
# every page, and the first thing to notice would be a purchase that never
# settles or a sale nobody can lock. Its health document says each of those
# in its own field, and answers 503 when any of them is wrong; this reads the
# fields and says which.
#
#   contrib/levo-check.sh                     # this box's levod
#   LEVO_HEALTH_URL=http://127.0.0.1:8099/api/health contrib/levo-check.sh
#
# Settings: LEVO_HEALTH_URL (default http://127.0.0.1:$LEVOD_PORT/api/health,
# port 8099 when LEVOD_PORT is not set), LEVO_MAX_WATCHER_AGE in seconds
# (default 300: the watcher polls every 60), LEVO_CHECK_RETRY_AFTER in seconds
# (default 3: the pause before a refused connection is tried once more),
# LEVO_CHECK_STATE (where the timer's last verdict is kept; see below).
#
# Exit 0 when everything checks out, 1 with a line naming each thing that does
# not. levo-check.timer runs it every five minutes.
set -uo pipefail
case "${1:-}" in
    -h|--help)
        echo "Usage: contrib/levo-check.sh"
        echo
        echo "Reads levod's /api/health and exits 0 when levod is doing its job, 1 with a"
        echo "line per fault. Settings: LEVO_HEALTH_URL, LEVO_MAX_WATCHER_AGE,"
        echo "LEVO_CHECK_RETRY_AFTER, LEVO_CHECK_STATE. Only the timer's runs record a"
        echo "verdict and page anyone."
        exit 0 ;;
    ?*) echo "levo-check.sh takes no arguments; settings are environment variables" >&2; exit 2 ;;
esac

# A verdict is the normal way out, and the tail of this script says it to a
# person only when it CHANGES. A crash before the verdict is another matter:
# levo-check.service carries no OnFailure= (it would repeat the verdict every
# run), so this is the only thing that would say the check itself has broken.
#
# Only the TIMER's runs record a verdict and page anyone. A run by hand -- an
# operator looking, a drill on the box -- must not overwrite the timer's last
# verdict or announce a fault it made up: systemd marks its own runs with
# INVOCATION_ID, and naming LEVO_CHECK_STATE opts a run in.
ALERT="$(dirname "$0")/levo-alert.sh"
RECORDS=""
if [ -n "${INVOCATION_ID:-}" ] || [ -n "${LEVO_CHECK_STATE:-}" ]; then
    RECORDS=1
fi
VERDICT_SAID=""
trap 'rc=$?; if [ -n "$RECORDS" ] && [ -z "$VERDICT_SAID" ] && [ -x "$ALERT" ]; then "$ALERT" "levo check CRASHED (exit $rc) before reaching a verdict; journalctl -u levo-check.service says where"; fi' EXIT

URL="${LEVO_HEALTH_URL:-http://127.0.0.1:${LEVOD_PORT:-8099}/api/health}"
MAX_AGE="${LEVO_MAX_WATCHER_AGE:-300}"

fails=0
FAILS_SO_FAR=""
ok()  { echo "  ok    $1"; }
bad() { echo "  FAIL  $1"; fails=$((fails + 1)); FAILS_SO_FAR="$FAILS_SO_FAR
  FAIL  $1"; }

# The body is what is read: a 503 carries the same document as a 200, with
# the field that is wrong set to say so. A connection nobody answers is
# asked once more after a pause: a deploy restarts levod in about two
# seconds, and a check that lands inside them would page a person for a
# levod that was back before they read it.
body=$(curl -sS --max-time 10 "$URL" 2>&1)
rc=$?
if [ "$rc" -eq 7 ]; then
    sleep "${LEVO_CHECK_RETRY_AFTER:-3}"
    body=$(curl -sS --max-time 10 "$URL" 2>&1)
    rc=$?
fi
if [ "$rc" -ne 0 ]; then
    bad "levod ($URL) did not answer: $body"
else
    verdicts=$(printf '%s' "$body" | MAX_AGE="$MAX_AGE" python3 -c '
import json, os, sys
try:
    h = json.load(sys.stdin)
except Exception:
    print("FAIL health is not JSON: " + sys.stdin.read()[:80]); raise SystemExit
def line(good, what):
    print(("ok   " if good else "FAIL ") + what)
app, node, sf, w = (h.get(k) or {} for k in ("app", "node", "state_file", "watcher"))
line(h.get("ok") is True, "health says ok" if h.get("ok") is True else "health says not ok")
line(app.get("serving") is True, "the app is being served" if app.get("serving") else "no app is being served: web/dist is empty or missing")
line(not app.get("source_newer_than_bundle"), "the bundle is as new as the source" if not app.get("source_newer_than_bundle") else "the source is newer than the bundle: a deploy did not finish")
line(node.get("reachable") is True, "the node answers (height %s)" % node.get("height") if node.get("reachable") else "the node is not reachable")
line(sf.get("writable") is True and not sf.get("last_error"), "the state file is writable" if sf.get("writable") else "the state file cannot be written: %s" % (sf.get("last_error") or "no reason given"))
line(not sf.get("unsaved_changes"), "nothing is waiting to be saved" if not sf.get("unsaved_changes") else "changes are waiting that could not be saved")
# A watcher that never polled is for levod to judge: its own ok turns false
# after three intervals without a poll, which the first line above reports.
# A watcher that HAS polled must still be alive, and recently.
age = w.get("last_run_age_seconds")
max_age = int(os.environ["MAX_AGE"])
if age is None:
    line(True, "the watcher has not polled yet")
else:
    line(w.get("running") is True, "the watcher is running" if w.get("running") else "the watcher thread has died")
    line(age <= max_age, "the watcher polled %ss ago" % age if age <= max_age else "the watcher last polled %ss ago (limit %ss)" % (age, max_age))
errs = w.get("consecutive_errors") or 0
line(errs == 0, "the last poll succeeded" if errs == 0 else "%d polls in a row failed: %s" % (errs, w.get("last_error")))
unv = w.get("unverified_sales") or []
line(not unv, "every funded sale is placed in the chain" if not unv else "funding not found in the chain for: %s" % ", ".join(str(s) for s in unv[:5]))
')
    while IFS= read -r v; do
        case "$v" in
            "ok   "*) ok "${v#ok   }" ;;
            "FAIL "*) bad "${v#FAIL }" ;;
        esac
    done <<< "$verdicts"
fi

echo
# Tell a person when the verdict CHANGES, not every five minutes: the first
# failing run, and the run that passes again. `levo-alert.sh` says how the
# push channel is set up; without one this is a journal line.
STATE_FILE="${LEVO_CHECK_STATE:-/var/lib/levo-check/last}"
now_verdict=$([ "$fails" -eq 0 ] && echo ok || echo fail)
VERDICT_SAID=1
if [ -z "$RECORDS" ]; then
    echo "(run by hand: this verdict is not recorded and pages nobody; the timer's runs do both)"
    was="$now_verdict"
else
    was=$(cat "$STATE_FILE" 2>/dev/null || echo "unknown")
    mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null && echo "$now_verdict" > "$STATE_FILE" 2>/dev/null
fi
if [ "$now_verdict" != "$was" ] && [ -x "$ALERT" ]; then
    if [ "$now_verdict" = fail ]; then
        "$ALERT" "levo check FAILED ($fails): $(printf '%s' "$FAILS_SO_FAR" | grep '  FAIL' | head -3 | cut -c1-200 | tr '\n' ' ')"
    elif [ "$was" != unknown ]; then
        "$ALERT" "levo check passes again"
    fi
fi
if [ "$fails" -eq 0 ]; then
    echo "levo check passed"
    exit 0
fi
echo "$fails levo check(s) FAILED"
exit 1

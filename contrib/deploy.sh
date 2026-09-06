#!/bin/sh
# Put the current origin/main on this host and prove it is the one being served.
#
# The step this exists for is the build. levod serves a bundle out of web/dist,
# and a build that fails leaves the previous bundle exactly where it was: the
# pages still render, every API check still passes, and the site is two commits
# behind with nothing anywhere saying so. A deploy that pipes the build through
# `tail` or `| head -1` loses its exit status and reports that as success.
#
# So every step here is checked, and the last one asks levod itself whether the
# bundle it is serving is older than the source it was built from.
#
# Usage: contrib/deploy.sh [checkout] [service] [health url]
set -eu

case "${1:-}" in
  -h|--help)
    cat <<'HELP'
Usage: contrib/deploy.sh [checkout] [service] [health url]

Puts origin/main on this box: fetches, builds the app under a Node in Vite's
range, installs the systemd units, restarts levod and asks it what it is
serving. Defaults: /root/sequentia/levo, levod, and the health URL the unit's
environment file names. A change to this script hands over to the fetched
copy. contrib/README.md carries the rest.
HELP
    exit 0 ;;
esac

DIR=${1:-/root/sequentia/levo}
UNIT=${2:-levod}

# Where to ask is where levod listens, and that is written in the unit's
# environment file, the same place levod reads it from. A guessed port would
# report a healthy deployment as a silent one. When the unit is not installed
# yet, levod's own defaults apply.
health_url() {
  env_file=$(systemctl show "$UNIT" -p EnvironmentFiles --value 2>/dev/null | cut -d' ' -f1)
  host=127.0.0.1
  port=8099
  if [ -n "$env_file" ] && [ -r "$env_file" ]; then
    h=$(sed -n 's/^LEVOD_HOST=//p' "$env_file" | tail -n 1)
    p=$(sed -n 's/^LEVOD_PORT=//p' "$env_file" | tail -n 1)
    [ -n "$h" ] && host=$h
    [ -n "$p" ] && port=$p
  fi
  printf 'http://%s:%s/api/health' "$host" "$port"
}
HEALTH=${3:-$(health_url)}

say() { printf '%s\n' "$*" >&2; }
die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }

[ -d "$DIR/.git" ] || die "$DIR is not a checkout"
cd "$DIR"

say "== fetching"
BEFORE=$(git rev-parse HEAD:contrib/deploy.sh)
git fetch origin --quiet || die "could not fetch"
git reset --hard origin/main --quiet || die "could not check out origin/main"
COMMIT=$(git rev-parse --short HEAD)
say "   at $COMMIT $(git log --format=%s -1)"

# A deployment that changes this script would otherwise finish under the old
# one, since bash read it before the fetch. Hand over to the fetched copy,
# once: the second run fetches nothing new and carries on.
if [ "$BEFORE" != "$(git rev-parse HEAD:contrib/deploy.sh)" ] && [ -z "${LEVO_DEPLOY_HANDOVER:-}" ]; then
  say "   this script changed; continuing with the new one"
  LEVO_DEPLOY_HANDOVER=1 exec bash contrib/deploy.sh "$@"
fi

# The bundler needs a Node inside vite's engine range, and the system Node on a
# box that runs other services is usually not it -- upgrading /usr/bin/node
# under them to build one site would be the wrong trade. So a newer one is kept
# beside it, and this is the line that remembers to use it. web/scripts/check-node.mjs
# refuses the build otherwise rather than failing halfway.
for n in /opt/node*/bin; do
  [ -x "$n/node" ] && PATH="$n:$PATH" && break
done
export PATH
say "== building the app with $(node --version 2>/dev/null || echo 'no node on PATH')"
cd web
# Dependencies first: a lockfile that moved is the usual reason a build that
# worked last week does not work today.
if [ package-lock.json -nt node_modules ] || [ ! -d node_modules ]; then
  npm ci || die "npm ci failed"
fi
# No pipe: the exit status of the build is the whole point of this script.
LEVO_BASE=${LEVO_BASE:-/levo/} LEVO_SITE_ORIGIN=${LEVO_SITE_ORIGIN:-} npm run build || die "the app did not build -- the old bundle is still being served, so the site is unchanged rather than broken"
cd ..

# A unit change in the repository reaches the box nowhere but here. The
# copy is idempotent, and daemon-reload is what makes systemd read it.
say "== installing the units"
install -m 644 contrib/levod.service contrib/levo-backup.service contrib/levo-backup.timer \
  /etc/systemd/system/ || die "could not install the units"
systemctl daemon-reload || die "daemon-reload failed"

say "== restarting $UNIT"
systemctl restart "$UNIT" || die "could not restart $UNIT"

say "== checking what is being served"
i=0
while [ "$i" -lt 30 ]; do
  BODY=$(curl -fsS --max-time 5 "$HEALTH" 2>/dev/null) && break
  i=$((i + 1))
  sleep 1
done
[ -n "${BODY:-}" ] || die "$UNIT did not answer $HEALTH"

printf '%s' "$BODY" | python3 -c '
import json, sys
h = json.load(sys.stdin)
app = h.get("app", {})
bad = []
if not h.get("ok"):
    bad.append("levod reports itself unhealthy: " + json.dumps(h))
elif not app.get("serving"):
    bad.append("levod is not serving the app")
elif app.get("source_newer_than_bundle"):
    bad.append("the bundle being served is older than the source it was built "
               "from -- the build did not take")
if bad:
    print("deploy: " + "; ".join(bad), file=sys.stderr)
    raise SystemExit(1)
print("deploy: serving %s, built %s" % (app.get("bundle", "the app"), app.get("built_at")))
' || die "the deploy did not take"

say "== done"

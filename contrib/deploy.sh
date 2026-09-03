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

DIR=${1:-/root/sequentia/levo}
UNIT=${2:-levod}
HEALTH=${3:-http://127.0.0.1:8787/api/health}

say() { printf '%s\n' "$*" >&2; }
die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }

[ -d "$DIR/.git" ] || die "$DIR is not a checkout"
cd "$DIR"

say "== fetching"
git fetch origin --quiet || die "could not fetch"
git reset --hard origin/main --quiet || die "could not check out origin/main"
COMMIT=$(git rev-parse --short HEAD)
say "   at $COMMIT $(git log --format=%s -1)"

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

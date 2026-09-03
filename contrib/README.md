# Deploying levod

- `levod.service` runs `levod/server.py` from the checkout the box pulls, with
  every setting in `/etc/sequentia/levod.env`.
- `levod.env.example` lists every setting. Copy it, fill it in, keep it out of
  the repo.
- `levo-backup.sh`, `levo-backup.service` and `levo-backup.timer` copy the
  state file to `/var/backups/levo` four times a day and keep the newest sixty
  copies. The state file holds every sale's terms, which is what a funded
  sale's leaves are rebuilt from, and every buyer's allocation.

  Every one of those copies is on the machine they protect. That is enough for
  a bad write and nothing else: a lost disk takes the sales' terms with it, and
  a funded sale whose terms are gone can be seen on chain and reclaimed by
  nobody. Copy `/var/backups/levo` off the box on whatever schedule the rest of
  the deployment uses -- the file holds an allocation ledger, so treat it as
  private -- and check `contrib/README.md`'s restore drill against it once.

```sh
# Build the app FIRST. levod serves the app and the API from one origin, so
# starting it against an empty web/dist gives a site that answers 404 for every
# page while every other check passes -- and it decides at startup whether it is
# serving an app at all, so health cannot tell you either.
PATH=/opt/node24/bin:$PATH LEVO_BASE=/levo/ npm --prefix web ci
PATH=/opt/node24/bin:$PATH LEVO_BASE=/levo/ npm --prefix web run build

install -m 644 contrib/levod.service contrib/levo-backup.service contrib/levo-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now levod.service levo-backup.timer
contrib/levo-backup.sh                      # take the first copy now, not in six hours
curl -fsS http://127.0.0.1:8099/api/health | head -20
```

levod exits 78 and stays down when its state file cannot be read, rather than
restarting every five seconds over the one message that says to restore a
backup. `systemctl status levod` shows it; restore from `/var/backups/levo` and
start it again.

## Upgrading

Reinstall the units as well as the code: a unit change in the repo reaches the
box only here, and levod's own "do not restart me" status is a unit setting.

```sh
cd /root/sequentia/levo && git pull --ff-only
install -m 644 contrib/levod.service contrib/levo-backup.service contrib/levo-backup.timer /etc/systemd/system/
systemctl daemon-reload
PATH=/opt/node24/bin:$PATH LEVO_BASE=/levo/ npm --prefix web ci
PATH=/opt/node24/bin:$PATH LEVO_SITE_ORIGIN=https://sequentiatestnet.com LEVO_BASE=/levo/ npm --prefix web run build
systemctl restart levod
curl -fsS http://127.0.0.1:8099/api/health | head -20
```

The build needs a Node inside Vite's engine range (20.19 or later, or 22.12 or
later), which is why the two build lines put one in front: an older Node skips
the bundler's native binding as an unmet engine and then dies on a missing
module, which reads like a broken checkout rather than a version.

The last line is the check: `ok` is false when the node is unreachable, the
watcher has stalled or is failing every poll, the state file cannot be written,
or the built app is missing. levod serves the app and the API from one origin,
so an empty `web/dist` is a site that 404s every page while every other check
passes.

## Restoring the state file

Stop levod first. A restore under a running levod is overwritten by its next
save within the minute.

Choose the copy by its name, not by its date. `levo-backup.sh` copies with
`cp -p`, so every copy carries the state file's own write time and the newest
file is not always the newest state; the names are UTC stamps and sort in
order.

```sh
systemctl stop levod
ls -1 /var/backups/levo/levo-state-*.json | tail -3     # the stamp IS the age
cp /var/backups/levo/levo-state-<stamp>.json /var/lib/levo/levo-state.json
chown root:root /var/lib/levo/levo-state.json && chmod 600 /var/lib/levo/levo-state.json
systemctl start levod
journalctl -u levod -n 30 --no-pager

# What was restored, rather than that something was:
curl -fsS http://127.0.0.1:8099/api/health |
  python3 -c 'import json,sys; h=json.load(sys.stdin); print(h["ok"], h["state_file"], h["watcher"]["unverified_sales"])'
curl -fsS http://127.0.0.1:8099/api/projects | head -c 400
```

`ok` must be true, `state_file.writable` must be true, and the board must list
the sales you expect. A sale missing from it is a sale this copy predates.

**What a restore cannot bring back.** Purchases recorded between the copy and
the failure are gone from the allocation ledger, and the chain cannot return
them: the transactions are on chain, but which account made them is Levo's own
record. Those buyers get their whole tier cap back on a sale they have already
partly filled, until the purchase is recorded again with
`levo record <sale> --txid <txid> --tokens <n>`. Everything else the watcher
re-derives from the chain within a poll.

A restored file may also carry sales whose funding levod cannot place in the
chain any more. Those are reported as unverified in `/api/health` and on their
own pages, and are left exactly as they were rather than guessed at; the next
purchase or block that moves one puts it back on its feet.


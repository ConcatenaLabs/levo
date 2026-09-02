# Deploying levod

- `levod.service` runs `levod/server.py` from the checkout the box pulls, with
  every setting in `/etc/sequentia/levod.env`.
- `levod.env.example` lists every setting. Copy it, fill it in, keep it out of
  the repo.
- `levo-backup.sh`, `levo-backup.service` and `levo-backup.timer` copy the
  state file to `/var/backups/levo` four times a day and keep the newest sixty
  copies. The state file holds every sale's terms, which is what a funded
  sale's leaves are rebuilt from, and every buyer's allocation.

```sh
install -m 644 contrib/levod.service contrib/levo-backup.service contrib/levo-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now levod.service levo-backup.timer
contrib/levo-backup.sh                      # take the first copy now, not in six hours
```

levod exits 78 and stays down when its state file cannot be read, rather than
restarting every five seconds over the one message that says to restore a
backup. `systemctl status levod` shows it; restore from `/var/backups/levo` and
start it again.

After a pull that changes `web/`, rebuild the app on the box before restarting:

```sh
LEVO_BASE=/levo/ npm --prefix web ci && LEVO_BASE=/levo/ npm --prefix web run build
systemctl restart levod
```

The build needs a Node inside Vite's engine range (20.19 or later, or 22.12 or
later). An older Node skips the bundler's native binding as an unmet engine and
then fails on a missing module, which reads like a broken checkout rather than
a version. Where the Node on `PATH` is older, put a newer one in front for the
build only -- on the testnet box that is `/opt/node24`:

```sh
PATH=/opt/node24/bin:$PATH LEVO_BASE=/levo/ npm --prefix web ci
PATH=/opt/node24/bin:$PATH LEVO_BASE=/levo/ npm --prefix web run build
```

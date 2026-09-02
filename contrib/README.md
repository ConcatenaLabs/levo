# Deploying levod

- `levod.service` runs `levod/server.py` from the checkout the box pulls, with
  every setting in `/etc/sequentia/levod.env`.
- `levod.env.example` lists every setting. Copy it, fill it in, keep it out of
  the repo.
- `levo-backup.sh`, `levo-backup.service` and `levo-backup.timer` copy the
  state file to `/var/backups/levo` four times a day and keep the newest sixty
  copies.

```sh
install -m 644 contrib/levod.service contrib/levo-backup.service contrib/levo-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now levod.service levo-backup.timer
```

After a pull that changes `web/`, rebuild the app on the box before restarting:

```sh
LEVO_BASE=/levo/ npm --prefix web ci && LEVO_BASE=/levo/ npm --prefix web run build
systemctl restart levod
```

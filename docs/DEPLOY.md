# Deploy (production)

The API runs on one VPS (Ubuntu 24.04) with Docker Compose: `db` (Postgres 16, volume `pgdata`),
`api` (this image, runs `alembic upgrade head` on start) and `caddy` (HTTPS for `api.ticketoff.store`
and the web app on `ticketoff.store`, certificates from Let's Encrypt). Only ports 22, 80 and 443 are open; Postgres and the API are not
published to the host.

The API also keeps the files sent with support messages (BE-33) in the volume `attachments`
(`/data/attachments` inside the `api` container, one file per attachment, named by its uuid).
Event cover images (R-12) are in the same volume, in its folder `events/`
(`/data/attachments/events`, `EVENT_IMAGES_DIR`), so the same backup covers them.

Files: `docker-compose.prod.yml`, `deploy/Caddyfile`, `.env.prod.example`, `scripts/backup-db.sh`.

## Prerequisites

- DNS: A records `@`, `www`, `api` of `ticketoff.store` point to the server IP.
- Server: SSH by key only (`PasswordAuthentication no`), `ufw allow 22,80,443/tcp` + `443/udp`,
  Docker Engine with the compose plugin.

## First deploy

```bash
sudo mkdir -p /opt/ticketoff && sudo chown "$USER" /opt/ticketoff
git clone https://github.com/TimurKyr/ticketoff-backend.git /opt/ticketoff
cd /opt/ticketoff
cp .env.prod.example .env && chmod 600 .env
```

Fill `.env` on the server (secrets never leave it):

```bash
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^JWT_SECRET=.*/JWT_SECRET=$(openssl rand -hex 32)/" .env
sed -i "s/^TICKET_SIGNING_SECRET=.*/TICKET_SIGNING_SECRET=$(openssl rand -hex 32)/" .env
```

Start and check:

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f api caddy
curl https://api.ticketoff.store/health   # {"status":"ok","database":"ok"}
```

Docs: `https://api.ticketoff.store/docs`.

## Update

```bash
cd /opt/ticketoff && git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Migrations run automatically when the `api` container starts.

## Web app

Caddy serves the static build of `ticketoff-web` from `/opt/ticketoff/web` (mounted read-only at
`/srv/web`) on `ticketoff.store`; `www.ticketoff.store` redirects there. Unknown paths fall back to
`index.html` (SPA routing). The web app is built on a developer machine or in CI, not on the server
(1 GB RAM), and uploaded with `scripts/deploy.sh` from the `ticketoff-web` repo:

```bash
# in ticketoff-web
npm ci && npm run build
rsync -az --delete --filter='P /assets/**' --delay-updates dist/ ubuntu@<server>:/opt/ticketoff/web/
```

Before the first `up` with this mount, create the folder as `ubuntu`. Otherwise Docker Engine on
Linux creates `./web` as `root:root` and `rsync` fails with `Permission denied`:

```bash
mkdir -p /opt/ticketoff/web
# if Docker already created it:
sudo chown ubuntu:ubuntu /opt/ticketoff/web
```

DNS: `www` needs its own record (here a CNAME to `ticketoff.store`), or Caddy keeps retrying its
certificate; the apex domain works either way.

A missing file under `/assets/` is a plain 404 (`no-store`), not `index.html`. The deploy script
keeps old `assets/` files, so tabs opened before a deploy can still load their chunks.

A new upload needs no restart. After changing `deploy/Caddyfile`:
`docker compose -f docker-compose.prod.yml up -d caddy` (recreated if the mount changed) or
`docker compose -f docker-compose.prod.yml exec caddy caddy reload --config /etc/caddy/Caddyfile`.

## Backups

`scripts/backup-db.sh` writes `backups/ticketoff-<date>.dump` (`pg_dump -Fc`) and deletes dumps
older than 14 days. Cron (`crontab -e`):

```
15 3 * * * /opt/ticketoff/scripts/backup-db.sh >> /opt/ticketoff/backups/backup.log 2>&1
```

Restore into the running stack:

```bash
docker compose -f docker-compose.prod.yml exec -T db \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backups/<file>.dump
```

### Support attachments and event images (the `attachments` volume)

The database dump holds only the names, types and sizes of the files sent with support messages
and the ids of the event cover images (R-12, folder `events/` of the volume);
**the files themselves are in the volume `attachments`, so a dump alone does not save them**. Back
the folder up together with the dump (same cron, same day):

```bash
cd /opt/ticketoff
docker compose -f docker-compose.prod.yml exec -T api \
  tar czf - -C /data/attachments . > backups/attachments-$(date +%Y-%m-%d).tar.gz
```

Restore (into the running stack; existing files with the same name are overwritten):

```bash
docker compose -f docker-compose.prod.yml exec -T api \
  tar xzf - -C /data/attachments < backups/attachments-<date>.tar.gz
```

Restore the dump and the archive from the same day: a row without its file answers 404 on
download, and a file without a row is just unused space. Removing the volume
(`docker compose down -v`) deletes the files for good. The limits (3 files, 5 MB each, PNG, JPEG
or PDF) are in `app/modules/support/attachments.py`; the storage is behind an interface
(`app/modules/support/storage.py`), so it can be replaced by an S3-compatible store later.
Event images: one per event, PNG, JPEG or WebP up to 5 MB (`app/modules/events/images.py`); a
replaced or removed image's file is deleted at once.

## Demo data

`app/scripts/seed_demo.py` creates venues (Almaty, Astana), a demo organizer, scanner and buyer,
19 published `Demo: ...` events (BE-24: both cities, categories `music`, `theatre`, `education`,
`sport`, `kids`, `other`; today, tomorrow, the nearest weekend, the next 2-6 weeks and two past
events with scanned tickets; free and paid, two of them almost sold out), a promo campaign with the code `DEMO20` (20% off) and demo tickets in every scanner state (valid,
checked in, cancelled, refunded, bought with the promo code, another event). It is idempotent:
running it again creates nothing; demo events from an older seed (titles without `Demo: `) are
renamed, and tickets left valid by an interrupted run are brought to their demo status.

```bash
docker compose -f docker-compose.prod.yml exec -e DEMO_PASSWORD='<password>' api python -m app.scripts.seed_demo
```

- `DEMO_PASSWORD` is the password of all demo accounts (and resets it on existing ones). Without
  it, new accounts get a random password that is printed once. Do not commit it.
- `--print-tokens` prints the QR tokens and the promo link; `--qr-dir /tmp/demo-qr` writes one PNG
  per ticket (and `promo-DEMO20.png`) inside
  the container (copy out with `docker compose -f docker-compose.prod.yml cp api:/tmp/demo-qr .`).
- `--reset-scans` makes the demo tickets that should be valid valid again after a scanner demo.
- `--relative-dates` moves every open demo event whose day differs back to today + its offset
  (run it on the morning of a demo, so "today" and "this weekend" are right). Without it, events
  keep the dates of the run that created them.

## Platform admin

`app/scripts/create_admin.py` creates a platform admin (the `/admin/*` API, BE-22). The password
comes from `ADMIN_PASSWORD` or a hidden prompt (needs `-it`) and is never printed. Idempotent.

```bash
docker compose -f docker-compose.prod.yml exec -it api python -m app.scripts.create_admin <email>
```

If the email is already registered, the script refuses and changes nothing: registration is
public and emails are not verified, so someone else may own that account. Only for an account
you know is yours, `--promote-existing` makes it an admin and keeps its current password.

Suspending a user (`/admin/users/{id}/suspend`) blocks their login and tokens only. The events of
a suspended organizer keep selling; stop those with `/admin/events/{id}/suspend`.

## Current server

Oracle Cloud Always Free, `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB RAM + 2 GB swap), Ubuntu 24.04.
On Oracle, ports 80/443 must also be open in the subnet's Security List (ingress TCP 80,443 and
UDP 443); the image's own iptables rules were replaced with ufw.

## Notes

- Do not change `POSTGRES_PASSWORD` after the first start: Postgres keeps the password from the
  first init of the volume.
- `ENVIRONMENT` must not be `local`: the app then rejects a short `TICKET_SIGNING_SECRET` or one
  equal to `JWT_SECRET`.

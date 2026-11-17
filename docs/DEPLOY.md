# Deploy (production)

The API runs on one VPS (Ubuntu 24.04) with Docker Compose: `db` (Postgres 16, volume `pgdata`),
`api` (this image, runs `alembic upgrade head` on start) and `caddy` (HTTPS for `api.ticketoff.store`,
certificates from Let's Encrypt). Only ports 22, 80 and 443 are open; Postgres and the API are not
published to the host.

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

## Current server

Oracle Cloud Always Free, `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB RAM + 2 GB swap), Ubuntu 24.04.
On Oracle, ports 80/443 must also be open in the subnet's Security List (ingress TCP 80,443 and
UDP 443); the image's own iptables rules were replaced with ufw.

## Notes

- Do not change `POSTGRES_PASSWORD` after the first start: Postgres keeps the password from the
  first init of the volume.
- `ENVIRONMENT` must not be `local`: the app then rejects a short `TICKET_SIGNING_SECRET` or one
  equal to `JWT_SECRET`.

# Deployment: DigitalOcean

> **Status**: Updated for hybrid auth (JWT web sessions + API keys for CLI/automation).

## Overview

Lumiverb runs on a single DigitalOcean Droplet for Phase 1. This is a pragmatic bootstrap topology with a known single-host blast radius.

- **nginx** - TLS termination, static UI hosting, reverse proxy to FastAPI
- **systemd** - process supervision for API, worker, and Quickwit
- **PostgreSQL 16** - control plane DB plus one DB per tenant
- **Quickwit** - BM25 search cache on localhost only
- **uv** - Python environment and dependency management

The React UI is built to static assets and served directly by nginx.

## Quick start

From a fresh Ubuntu 22.04+ VPS with DNS already pointed:

```bash
curl -fsSL https://raw.githubusercontent.com/bubbafat/lumiverb/main/scripts/deploy-vps.sh \
  | bash -s -- --domain app.example.com --email you@example.com
```

Then create the first admin user:

```bash
cd /opt/lumiverb
sudo -u lumiverb .venv/bin/python -m src.cli create-user --email you@example.com --role admin
```

Open `https://app.example.com` and log in.

To update an existing install:

```bash
bash /opt/lumiverb/scripts/update-vps.sh
```

The rest of this document covers what the script does and how to customize it.

---

## Scope and non-negotiables

- All API routes are under `/v1/`.
- Web authentication uses email/password login with JWT bearer sessions.
- CLI and automation authenticate with static API keys: `Authorization: Bearer <api_key>`.
- Tenant context is derived from JWT claims or API key lookup.
- `JWT_SECRET` is required in production.
- Quickwit, proxies, and thumbnails are regenerable caches. Postgres is irreplaceable.

---

## Droplet sizing

Starting baseline:

- **CPU**: 2 vCPUs
- **RAM**: 4 GB
- **Disk**: 80 GB SSD root
- **Volume**: 100 GB attached volume mounted at `/var/lib/lumiverb`

Scale up when either condition is true:

- API p95 latency remains elevated under normal load
- Memory pressure causes swap usage or OOM restarts

---

## Directory layout

```text
/opt/lumiverb/          # app checkout / deploy artifact
/etc/lumiverb/          # config and secrets (root:root 0700)
  env                   # env vars file (root:root 0600)
/var/lib/lumiverb/      # data_dir (volume mount)
  proxies/
  thumbnails/
  quickwit/
/var/backups/lumiverb/  # local backup staging
```

---

## Environment variables

Secrets live in `/etc/lumiverb/env` and are never committed.

```bash
# /etc/lumiverb/env
# chmod 600; owner root (or root + readable by service group)

# Database
CONTROL_PLANE_DATABASE_URL=postgresql+psycopg2://app:<password>@127.0.0.1:5432/control_plane
TENANT_DATABASE_URL_TEMPLATE=postgresql+psycopg2://app:<password>@127.0.0.1:5432/{tenant_id}

# Auth
ADMIN_KEY=<openssl rand -hex 32>
API_SECRET_KEY=<openssl rand -hex 32>
JWT_SECRET=<openssl rand -hex 32>

# Password reset (optional — omit to disable forgot-password flow)
# SMTP_HOST=smtp.example.com
# SMTP_PORT=587
# SMTP_USER=apikey
# SMTP_PASSWORD=<smtp password>
# SMTP_FROM=noreply@example.com
# APP_HOST=https://app.example.com

# Storage
STORAGE_PROVIDER=local
DATA_DIR=/var/lib/lumiverb

# Search
QUICKWIT_URL=http://127.0.0.1:7280
QUICKWIT_ENABLED=true

# App
APP_ENV=production
LOG_LEVEL=INFO
```

Generate secrets:

```bash
openssl rand -hex 32
```

---

## PostgreSQL

For a single-Droplet Phase 1 deployment, run Postgres locally. Managed Postgres is recommended once uptime/ops requirements increase.

Install:

```bash
apt update
apt install -y postgresql-16 postgresql-16-pgvector
```

Bootstrap:

```sql
CREATE USER app WITH PASSWORD '<password>';
CREATE DATABASE control_plane OWNER app;
\c control_plane
CREATE EXTENSION IF NOT EXISTS vector;
```

Tenant databases are provisioned by the admin tenant-creation flow.

Hardening baseline:

- `listen_addresses = '127.0.0.1'`
- `ufw` firewall allows only SSH (22), HTTP (80), HTTPS (443) inbound — all other ports are denied
- daily logical backups plus weekly restore drill (see Backups)

---

## Quickwit

Quickwit runs on localhost and is not exposed publicly.

Install binary:

```bash
curl -L https://install.quickwit.io | bash
install -m 0755 ~/.local/bin/quickwit /usr/local/bin/quickwit
mkdir -p /var/lib/lumiverb/quickwit
chown -R lumiverb:lumiverb /var/lib/lumiverb/quickwit
```

Systemd unit:

```ini
# /etc/systemd/system/lumiverb-quickwit.service
[Unit]
Description=Lumiverb Quickwit
After=network.target

[Service]
Type=simple
User=lumiverb
Group=lumiverb
ExecStart=/usr/local/bin/quickwit run --service metastore --service indexer --service searcher --data-dir /var/lib/lumiverb/quickwit --listen-address 127.0.0.1 --rest-listen-port 7280
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/lumiverb/quickwit

[Install]
WantedBy=multi-user.target
```

> If your Quickwit release uses different `run` flags, adjust `ExecStart` to match `quickwit --help`.

---

## Application setup

```bash
git clone https://github.com/your-org/lumiverb /opt/lumiverb
cd /opt/lumiverb

# Keep consistent with project environment policy.
uv sync --all-extras

# Control-plane migrations
uv run alembic -c alembic-control.ini upgrade head
```

Bootstrap the first admin user after migrations:

```bash
uv run python -m src.cli create-user --email admin@example.com --role admin
```

---

## Build web UI

```bash
cd /opt/lumiverb/src/ui/web
npm ci
npm run build
```

The `dist/` directory is served by nginx.

Preferred production flow: build in CI, deploy artifact. Building on the Droplet is acceptable for manual deployments.

---

## systemd units

### API server

```ini
# /etc/systemd/system/lumiverb-api.service
[Unit]
Description=Lumiverb API Server
After=network.target postgresql.service lumiverb-quickwit.service
Wants=lumiverb-quickwit.service

[Service]
Type=simple
User=lumiverb
Group=lumiverb
WorkingDirectory=/opt/lumiverb
EnvironmentFile=/etc/lumiverb/env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/lumiverb/.venv/bin/uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=on-failure
RestartSec=5s
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/lumiverb

[Install]
WantedBy=multi-user.target
```

### Background worker

```ini
# /etc/systemd/system/lumiverb-worker.service
[Unit]
Description=Lumiverb Background Worker
After=network.target postgresql.service lumiverb-api.service
Wants=lumiverb-api.service

[Service]
Type=simple
User=lumiverb
Group=lumiverb
WorkingDirectory=/opt/lumiverb
EnvironmentFile=/etc/lumiverb/env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/lumiverb/.venv/bin/python -m src.workers.main
Restart=on-failure
RestartSec=10s
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/lumiverb

[Install]
WantedBy=multi-user.target
```

Enable services:

```bash
systemctl daemon-reload
systemctl enable --now lumiverb-quickwit lumiverb-api lumiverb-worker
```

---

## nginx

```nginx
# /etc/nginx/sites-available/lumiverb
server {
    listen 443 ssl http2;
    server_name app.example.com;

    ssl_certificate     /etc/letsencrypt/live/app.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.example.com/privkey.pem;

    root /opt/lumiverb/src/ui/web/dist;
    index index.html;

    # Keep responses secure by default.
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer-when-downgrade always;

    location /v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 100m;
        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}

server {
    listen 80;
    server_name app.example.com;
    return 301 https://$host$request_uri;
}
```

Certificate:

```bash
certbot --nginx -d app.example.com
```

---

## Firewall

The deploy script configures `ufw` to deny all inbound traffic except:

| Port | Protocol | Purpose |
|------|----------|---------|
| 22 | TCP | SSH |
| 80 | TCP | HTTP (certbot validation + HTTPS redirect) |
| 443 | TCP | HTTPS (nginx → API + UI) |

All backend services (PostgreSQL 5432, Quickwit 7280, uvicorn 8000) bind to `127.0.0.1` and are not reachable from the network even if the firewall were disabled.

To check status:

```bash
ufw status verbose
```

To allow additional ports (e.g. for a monitoring agent):

```bash
ufw allow <port>/tcp
```

---

## Deployments and updates

Preferred: run the update script, which pulls code, syncs deps, runs migrations, rebuilds the UI, and restarts services:

```bash
bash /opt/lumiverb/scripts/update-vps.sh
```

Manual fallback (e.g. pinning a specific release):

```bash
cd /opt/lumiverb
git fetch --all --prune
git checkout <release-tag-or-commit>
uv sync --all-extras
uv run alembic -c alembic-control.ini upgrade head
bash scripts/migrate.sh
cd src/ui/web && npm ci && npm run build && cd /opt/lumiverb
systemctl restart lumiverb-api lumiverb-worker
systemctl status --no-pager lumiverb-api lumiverb-worker lumiverb-quickwit
```

Rollback:

```bash
cd /opt/lumiverb
git checkout <previous-release-tag-or-commit>
uv sync --all-extras
systemctl restart lumiverb-api lumiverb-worker
```

---

## Post-deploy verification

Run this checklist after initial deploy and after each update.

1) Verify service state:

```bash
systemctl status --no-pager lumiverb-quickwit lumiverb-api lumiverb-worker
```

Expected: all services are `active (running)` with no restart loop.

2) Verify API health:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS https://app.example.com/v1/health
```

Expected: both requests return `200 OK`.

3) Verify nginx + TLS:

```bash
nginx -t
systemctl status --no-pager nginx
certbot certificates
```

Expected: nginx config test passes and certificate for your domain is present and not expired.

4) Verify firewall policy:

```bash
ufw status verbose
ss -ltnp | rg ':(22|80|443|5432|7280|8000)\b'
```

Expected:
- `ufw` is active with inbound allow rules only for `22`, `80`, and `443`.
- Postgres (`5432`), Quickwit (`7280`), and API (`8000`) are bound to `127.0.0.1` only.

5) Verify migrations are at head:

```bash
cd /opt/lumiverb
sudo -u lumiverb /usr/local/bin/uv run alembic -c alembic-control.ini current
```

Expected: control-plane migration shows current head revision.  
If you have tenants, also run `bash scripts/migrate.sh` and confirm no failures.

6) Verify static UI serving:

```bash
curl -I https://app.example.com/
```

Expected: `200 OK` (or cached `304`) and HTML served from nginx.

7) Check recent logs for startup errors:

```bash
journalctl -u lumiverb-api -u lumiverb-worker -u lumiverb-quickwit -n 200 --no-pager
```

Expected: no repeated fatal errors, tracebacks, or crash/restart loops.

If any check fails, fix before onboarding users or running bulk ingest jobs.

---

## Backups and restore

Policy baseline:

- Daily `pg_dump` of control plane DB and each tenant DB
- Retention: 14 daily + 8 weekly snapshots
- Weekly restore drill to a throwaway Postgres instance

Example backup script:

```bash
#!/usr/bin/env bash
set -euo pipefail
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/var/backups/lumiverb/${STAMP}"
mkdir -p "${OUT}"

pg_dump -Fc -d control_plane -f "${OUT}/control_plane.dump"

# Dump every non-template DB except postgres.
for db in $(psql -Atqc "SELECT datname FROM pg_database WHERE datistemplate = false AND datname NOT IN ('postgres')"); do
  pg_dump -Fc -d "$db" -f "${OUT}/${db}.dump"
done
```

Restore test example:

```bash
createdb restore_check
pg_restore --clean --if-exists -d restore_check /var/backups/lumiverb/<stamp>/control_plane.dump
dropdb restore_check
```

---

## Monitoring and logs

- Use `journalctl -u lumiverb-api -f` and `journalctl -u lumiverb-worker -f`.
- Alert on repeated service restarts and disk free space under 20%.
- Track API p95 latency, job queue depth, and failed job counts.

---

## Open decisions

- [ ] Move Postgres to managed service when operational burden or uptime needs justify it.
- [ ] Decide when to split Quickwit and Postgres off the API Droplet.
- [ ] Formalize CI/CD artifact deployment and release promotion gates.

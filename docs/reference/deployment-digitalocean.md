# Deployment: DigitalOcean

> **Status**: Work in progress. Sections marked `[TODO]` are incomplete or have open questions.

## Overview

Lumiverb runs on a DigitalOcean Droplet. The deployment is a standard Linux VPS setup:
- **nginx** — TLS termination and reverse proxy to the FastAPI process
- **systemd** — manages the API server and background worker processes
- **PostgreSQL 16** — control plane DB and tenant DBs (separate databases, same instance)
- **Quickwit** — BM25 full-text search; runs as a systemd service
- **uv** — manages the Python environment and dependencies

The React web UI is compiled to static files and served by nginx directly.

---

## Droplet Sizing

> [TODO] Confirm sizing based on expected library size and number of users.

Starting point:
- **CPU**: 2 vCPUs (Basic, shared)
- **RAM**: 4 GB (Quickwit and the embedding worker are the hungriest processes)
- **Disk**: 50–100 GB SSD (for PostgreSQL data + Quickwit indexes; media files are stored separately)

Media files (originals, proxies, thumbnails) should live on a **DigitalOcean Volume** (block storage) attached to the Droplet, not on the root disk. This keeps the Droplet replaceable without data loss.

> [TODO] Decide on storage provider for proxies/thumbnails — local Volume vs. DigitalOcean Spaces (S3-compatible).

---

## Directory Layout on the Droplet

```
/opt/lumiverb/          # application code (git clone or deploy artifact)
/etc/lumiverb/          # config and secrets (root:root, 700)
  env                   # environment variables (root:root, 600)
/var/lib/lumiverb/      # runtime data (data_dir)
  proxies/
  thumbnails/
/var/log/lumiverb/      # log files (if not using journald)
```

---

## Environment Variables

Secrets live in `/etc/lumiverb/env` — not in the repo, not in the Docker image.

```bash
# /etc/lumiverb/env
# chmod 600, owned by root (or the lumiverb service user)

# Database
CONTROL_PLANE_DATABASE_URL=postgresql+psycopg2://app:<password>@127.0.0.1:5432/control_plane
TENANT_DATABASE_URL_TEMPLATE=postgresql+psycopg2://app:<password>@127.0.0.1:5432/{tenant_id}

# Auth
JWT_SECRET=<openssl rand -hex 32>
ADMIN_KEY=<openssl rand -hex 32>

# SMTP (for password reset emails) — optional
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=noreply@example.com
SMTP_PASSWORD=<password>
SMTP_FROM=noreply@example.com
APP_HOST=https://app.example.com

# Storage
STORAGE_PROVIDER=local
DATA_DIR=/var/lib/lumiverb

# Search
QUICKWIT_URL=http://127.0.0.1:7280
QUICKWIT_ENABLED=true

# App
APP_ENV=production
LOG_LEVEL=WARNING
```

Generate secrets:
```bash
openssl rand -hex 32   # for JWT_SECRET and ADMIN_KEY
```

---

## PostgreSQL

> [TODO] Decide: managed DO Managed Database vs. self-hosted on the Droplet.
> Managed DB adds ~$15/mo but removes backup/HA burden. Likely worth it.

If self-hosted:
```bash
apt install postgresql-16 postgresql-16-pgvector
```

Create the control plane database and application user:
```sql
CREATE USER app WITH PASSWORD '<password>';
CREATE DATABASE control_plane OWNER app;
\c control_plane
CREATE EXTENSION IF NOT EXISTS vector;
```

Tenant databases are created programmatically via `lumiverb create-tenant`.

---

## Quickwit

> [TODO] Document exact Quickwit install and systemd unit.

Quickwit runs as a separate process listening on `127.0.0.1:7280` (not exposed externally).

```bash
# [TODO] install steps
curl -L https://install.quickwit.io | bash
```

---

## Application Setup

```bash
# Clone the repo
git clone https://github.com/your-org/lumiverb /opt/lumiverb
cd /opt/lumiverb

# Install Python dependencies
uv sync --no-dev

# Run database migrations
uv run alembic -c migrations/control/alembic.ini upgrade head

# Bootstrap the first admin user
uv run lumiverb create-user --email admin@example.com --role admin
```

---

## Building the Web UI

```bash
cd /opt/lumiverb/src/ui/web
npm ci
npm run build   # outputs to dist/
```

The `dist/` directory is served by nginx as static files.

> [TODO] Decide whether to build on the Droplet or in CI and deploy the artifact.

---

## systemd Units

### API Server

```ini
# /etc/systemd/system/lumiverb-api.service
[Unit]
Description=Lumiverb API Server
After=network.target postgresql.service

[Service]
Type=simple
User=lumiverb
WorkingDirectory=/opt/lumiverb
EnvironmentFile=/etc/lumiverb/env
ExecStart=/opt/lumiverb/.venv/bin/uvicorn src.api.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 2
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

### Background Worker

```ini
# /etc/systemd/system/lumiverb-worker.service
[Unit]
Description=Lumiverb Background Worker
After=network.target postgresql.service

[Service]
Type=simple
User=lumiverb
WorkingDirectory=/opt/lumiverb
EnvironmentFile=/etc/lumiverb/env
ExecStart=/opt/lumiverb/.venv/bin/python -m src.workers.main
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

> [TODO] Confirm worker entrypoint module path.

```bash
systemctl daemon-reload
systemctl enable --now lumiverb-api lumiverb-worker
```

---

## nginx

nginx terminates TLS (via Let's Encrypt / certbot) and proxies to the API. Static web UI files are served directly.

```nginx
# /etc/nginx/sites-available/lumiverb
server {
    listen 443 ssl;
    server_name app.example.com;

    ssl_certificate     /etc/letsencrypt/live/app.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.example.com/privkey.pem;

    # Serve the React web UI
    root /opt/lumiverb/src/ui/web/dist;
    index index.html;

    # API proxy
    location /v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Increase for large file uploads (scans with many assets)
        client_max_body_size 100m;
    }

    # SPA fallback — let React Router handle client-side routes
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

```bash
certbot --nginx -d app.example.com
```

---

## Deployments / Updates

> [TODO] Define the deploy process. Options: git pull + restart, or CI artifact push.

Minimal manual deploy:
```bash
cd /opt/lumiverb
git pull
uv sync --no-dev
uv run alembic -c migrations/control/alembic.ini upgrade head
systemctl restart lumiverb-api lumiverb-worker
```

---

## Open Questions

- [ ] Managed DO PostgreSQL vs. self-hosted?
- [ ] Proxy/thumbnail storage: local Volume or DO Spaces?
- [ ] Build web UI on Droplet or in CI?
- [ ] Deploy process: manual git pull, or automated via CI/CD?
- [ ] How many worker processes / API uvicorn workers for expected load?
- [ ] Log aggregation: journald only, or ship to a log service?
- [ ] Backups: pg_dump schedule, Volume snapshots?
- [ ] Domain and DNS setup

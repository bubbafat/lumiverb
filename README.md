# Lumiverb

> AI-powered photo and video library management for serious photographers.
> Find any image or moment in a library of 100,000+ assets using natural language.

---

## What It Does

- **Natural language search** — "golden hour portraits on the beach" just works
- **Video scene understanding** — every shot in every video is indexed and searchable
- **Visual similarity** — find images that look like this one
- **Privacy-first** — source files never leave your machine; only JPEG proxies are processed

Supports JPEG, PNG, TIFF, HEIC, HEIF, WebP, RAW (CR2/CR3/NEF/ARW/DNG/ORF/RW2), and video (MP4/MOV/AVI/MKV/MTS/M2TS).

---

## Getting Started (Self-Hosted)

### Prerequisites
- Docker and Docker Compose
- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

### Setup
```bash
git clone https://github.com/bubbafat/lumiverb
cd lumiverb
cp .env.example .env.local    # edit if needed
docker compose up -d
uv sync --all-extras
./scripts/init.sh
```

In a second terminal:
```bash
uv run fastapi dev src/api/main.py
```

Re-run `./scripts/init.sh` after the server is running to complete setup.

### First library
```bash
lumiverb library create "My Photos" /path/to/your/photos
lumiverb scan --library "My Photos"
lumiverb worker proxy --once --library "My Photos"
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  CLI (local agent)          API Server (FastAPI/v1)      │
│  - Scans local filesystem   - Multi-tenant              │
│  - Generates proxies        - JWT + API key auth        │
│  - Uploads metadata         - Per-tenant DB routing     │
└───────────────┬─────────────────────┬───────────────────┘
                │                     │
         ┌──────▼──────┐    ┌─────────▼────────┐
         │  Tenant DB  │    │  Quickwit Search  │
         │  (Postgres) │    │  (BM25 index)     │
         │  per tenant │    │  per model ver    │
         └──────┬──────┘    └──────────────────┘
                │
         ┌──────▼──────────────────────────────┐
         │  Workers (pull-based, FOR UPDATE     │
         │  SKIP LOCKED, horizontally scalable) │
         │  - proxy_worker  - ai_worker         │
         │  - video_worker  - metadata_worker   │
         │  - search_sync_worker                │
         └─────────────────────────────────────┘
```

Two databases per deployment:
- **Control plane** — tenant registry, API key routing (shared, tiny)
- **Tenant databases** — one per tenant, fully isolated (libraries, assets, scenes, metadata)

See [`docs/architecture.md`](docs/architecture.md) for the full design.

---

## Quick Start (Development)

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Docker + Docker Compose
- `ffmpeg` and `ffprobe` on PATH
- `exiftool` on PATH

### 1. Clone and install

```bash
git clone <repo-url>
cd <repo-name>
uv sync --all-extras
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — the defaults work for local development as-is
```

### 3. Start infrastructure

```bash
docker compose up -d
# Postgres on :5432, Quickwit on :7280
# Wait for healthchecks to pass (~15s)
docker compose ps
```

### 4. Run database migrations

```bash
# Control plane migrations
uv run alembic -c alembic-control.ini upgrade head

# Tenant DB migrations run automatically when a tenant is provisioned
```

### 5. Start the API server

```bash
uv run uvicorn src.api.main:app --reload --port 8000
```

### 6. Provision a tenant and get an API key

```bash
uv run lumiverb admin tenant create --name "My Library" --email me@example.com
# Returns: tenant_id, api_key
```

### 7. Scan your first library

```bash
uv run lumiverb config set --api-url http://localhost:8000 --api-key <your-key>
uv run lumiverb library create "My Photos" /path/to/photos
uv run lumiverb scan
```

### 8. Start workers

In separate terminals (or use a process manager):

```bash
uv run lumiverb worker proxy --once         # generate proxies and thumbnails
uv run lumiverb worker ai --mode light      # run Moondream vision analysis
uv run lumiverb worker metadata --phase exif
uv run lumiverb worker search-sync          # push to Quickwit
```

> **Note:** AI workers require [Moondream Station](https://moondream.ai) running locally.
> Start it with `moondream-station` before running the AI worker.

### 9. Search

```bash
uv run lumiverb search "golden hour portraits"
# or via API:
curl "http://localhost:8000/v1/search?q=golden+hour+portraits" \
  -H "Authorization: Bearer <your-key>"
```

---

## Project Structure

```
.
├── docs/
│   ├── architecture.md        # System design — start here
│   ├── cursor-api.md          # Cursor context: API server rules
│   ├── cursor-cli.md          # Cursor context: CLI/agent rules
│   └── product-overview.md    # User-facing product description
│
├── reference/                 # Frozen algorithm docs from PoC
│   ├── README.md              # What's here and why
│   ├── video_scene_segmentation.md
│   ├── worker_base_pattern.md
│   ├── bm25_similarity_search.md
│   └── ai_vision_metadata.md
│
├── src/
│   ├── api/                   # FastAPI application (v1 routes)
│   ├── cli/                   # Typer CLI (local agent)
│   ├── workers/               # Background workers (BaseWorker subclasses)
│   ├── repository/            # DB access layer (Repository pattern)
│   ├── models/                # SQLModel entities
│   ├── video/                 # FFmpeg pipeline, scene segmentation
│   ├── metadata/              # EXIF, sharpness, face detection
│   ├── ai/                    # Vision analysis (Moondream Station)
│   ├── search/                # Quickwit integration
│   └── core/                  # Config, storage, utilities
│
├── migrations/
│   ├── control/               # Alembic migrations for control plane
│   └── tenant/                # Alembic migrations for tenant DBs
│
├── tests/
│   ├── test_migrations.py     # Must be updated for every new migration
│   └── ...
│
├── scripts/
│   └── docker/
│       └── postgres-init.sql  # pgvector setup on first container start
│
├── quickwit/
│   └── media_scenes_schema.json  # Quickwit index schema
│
├── .cursorrules               # Cursor AI rules — read before coding
├── .env.example               # Environment template
├── docker-compose.yml         # Local dev infrastructure
├── docker-compose.dev.yml     # Test Quickwit on port 7281
└── pyproject.toml
```

---

## Development

### Running tests

```bash
# Fast tests only (no DB, no AI) — runs in seconds
uv run pytest -m fast

# All tests including DB (requires Docker)
uv run pytest -m "fast or slow"

# Migration tests
uv run pytest -m migration

# Everything
uv run pytest --all-extras
```

### Linting and formatting

```bash
uv run ruff check .
uv run ruff format .
uv run mypy src/
```

### Adding a migration

```bash
# Generate
uv run alembic -c alembic-tenant.ini revision --autogenerate -m "add_face_embeddings"

# Apply
uv run alembic -c alembic-tenant.ini upgrade head

# Then add a test to tests/test_migrations.py — this is required, not optional.
```

---

## Deployment (VPS)

Deploy Lumiverb to a DigitalOcean Droplet, Hetzner VPS, or any Ubuntu server (x86_64 or ARM64). The bootstrap script handles everything — Postgres, nginx, TLS, Quickwit, the API, and the web UI.

### What you need before you start

| Requirement | Details |
|---|---|
| **A VPS** | Ubuntu 22.04+, 2+ vCPUs, 4 GB+ RAM. A $24/mo DigitalOcean Droplet or Hetzner CX22 works. |
| **A domain** | e.g. `app.example.com`. You need access to its DNS records. |
| **SSH access** | You'll run the deploy script as root over SSH. |

The VPS does not need anything pre-installed beyond the Ubuntu base image. The script installs all dependencies (PostgreSQL 16, pgvector, nginx, Node.js 20, Python/uv, Quickwit, certbot).

### Step 1: Create the VPS

Create a Droplet (or equivalent) with Ubuntu 22.04 LTS. Note its public IP address.

### Step 2: Point DNS to the VPS

At your DNS provider (Cloudflare, Namecheap, Route 53, etc.), create an **A record**:

```
app.example.com  →  A  →  <your-vps-ip>
```

Wait for propagation (usually 1–5 minutes; you can check with `dig app.example.com`).

> DNS must resolve before the script runs — certbot validates the domain by making an HTTP request to it. If you want to deploy first and set up DNS later, add `--skip-certbot` and run `certbot --nginx -d app.example.com` after DNS is live.

### Step 3: Run the deploy script

SSH into the VPS and run:

```bash
curl -fsSL https://raw.githubusercontent.com/bubbafat/lumiverb/main/scripts/deploy-vps.sh \
  | bash -s -- --domain app.example.com --email you@example.com
```

This takes 3–5 minutes. It installs all system packages, creates the database, generates secrets, builds the web UI, configures nginx with TLS, and starts all services.

The `--email` flag is for Let's Encrypt certificate expiry notices (optional but recommended).

### Step 4: Provision a tenant and create the admin user

Lumiverb is multi-tenant. Before creating a user you need to provision a tenant, which creates a dedicated database and returns an API key.

```bash
# Get the admin key
ADMIN_KEY=$(sudo grep '^ADMIN_KEY=' /etc/lumiverb/env | cut -d= -f2-)

# Create a tenant (note the api_key in the response)
curl -s -X POST http://127.0.0.1:8000/v1/admin/tenants \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Org", "email": "you@example.com"}'
```

Save the `api_key` from the response, then configure the CLI and create your user:

```bash
# Configure the CLI with the tenant API key
sudo -u lumiverb mkdir -p /var/lib/lumiverb/.lumiverb
echo '{"api_url": "http://127.0.0.1:8000", "api_key": "<api_key from above>"}' \
  | sudo -u lumiverb tee /var/lib/lumiverb/.lumiverb/config.json > /dev/null

# Create the admin user (password must be 12+ characters)
sudo -u lumiverb /opt/lumiverb/.venv/bin/lumiverb create-user --email you@example.com --role admin
```

### Step 5: Log in

Open `https://app.example.com` in your browser and sign in with the email and password you just created.

### Updating

To pull the latest code, run migrations, rebuild the UI, and restart services:

```bash
bash /opt/lumiverb/scripts/update-vps.sh
```

### Optional: enable password reset emails

Edit `/etc/lumiverb/env`, uncomment and fill in the `SMTP_*` lines, then restart:

```bash
systemctl restart lumiverb-api
```

### Useful commands

```bash
journalctl -u lumiverb-api -f       # API server logs
journalctl -u lumiverb-worker -f    # Background worker logs
systemctl restart lumiverb-api      # Restart API
sudo grep ADMIN_KEY /etc/lumiverb/env   # Admin key (for CLI access)
```

For full details on what the script does, directory layout, nginx config, backups, and manual deployment steps, see [`docs/reference/deployment-digitalocean.md`](docs/reference/deployment-digitalocean.md).

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| 1 | Done | CLI + API, scan + ingest, BM25 search |
| 2 | Planned | Visual similarity, face clustering, keyword fingerprinting |
| 3 | Done | Web UI (React + TypeScript + Tailwind) |
| 4 | Planned | Mac app (local agent with GUI) |
| 5 | Done | User accounts (email/password + JWT), role-based access |
| 6 | Planned | Cloud self-service, Stripe billing |

---

## License

MIT — see [LICENSE](LICENSE).

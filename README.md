# Lumiverb

> AI-powered photo and video library management for serious photographers.
> Find any image or moment in a library of 100,000+ assets using natural language.

---

## What It Does

- **Natural language search** — "golden hour portraits on the beach" just works
- **Video scene understanding** — every shot in every video is indexed and searchable
- **Visual similarity** — find images that look like this one
- **Privacy-first** — originals stay on your own storage, read in place and never modified or uploaded

Supports JPEG, PNG, TIFF, HEIC, HEIF, WebP, RAW (CR2/CR3/NEF/ARW/DNG/ORF/RW2), and video (MP4/MOV/AVI/MKV/MTS/M2TS).

---

## Install

### On a Mac

With [Homebrew](https://brew.sh):

```bash
brew trust --tap bubbafat/lumiverb && brew install bubbafat/lumiverb/lumiverb
brew services start lumiverb
```

(Homebrew installs from a tap that isn't its own only once you trust it:
that's `brew trust`.)

Then open <http://localhost:6464> on that Mac and create your account. Add a
library (Libraries › Add library: a name and the folder your photos and
videos are in) and Lumiverb scans and processes it.

It's one service that starts again at login: Lumiverb runs its own
PostgreSQL 18 (with pgvector), Quickwit and nginx, so nothing is shared with
another PostgreSQL you run. It also brings ffmpeg, exiftool, libvips and
Python 3.12. The web app is for that Mac only to begin with; to use it from
a phone or another computer, an admin turns on Settings › Network on that
Mac. To add people, Admin › Manage users › Invite someone gives you a link to
send them; they choose their own password there. Details, ports
and the Terminal way to create the account: the
[tap's README](https://github.com/bubbafat/homebrew-lumiverb).

Update (it restarts itself into the new version):

```bash
brew upgrade lumiverb
```

### On a Linux server

Ubuntu 22.04 or later, with a domain name already pointing at it (the TLS
certificate is checked against it):

```bash
curl -fsSL https://raw.githubusercontent.com/bubbafat/lumiverb/main/scripts/deploy-vps.sh \
  | sudo bash -s -- --domain app.example.com --email you@example.com --worker
```

It installs everything (PostgreSQL 18 with pgvector, nginx with a
certificate, Quickwit, Python, Node.js), builds the web app and starts the
services. `--worker` adds the scheduler, which does all the processing. Then
create your account:

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server setup --env /etc/lumiverb/env
```

Update:

```bash
sudo bash /opt/lumiverb/scripts/update.sh
```

A server on your home network, without a domain: [docs/brain-setup.md](docs/brain-setup.md).
More on the Linux install below, under [Running a Linux server](#running-a-linux-server).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  CLI (local agent)          API Server (FastAPI/v1)      │
│  - Scans local filesystem   - Multi-tenant              │
│  - Generates proxies        - JWT + API key auth        │
│  - Runs vision AI           - Per-tenant DB routing     │
│  - Uploads via /v1/ingest   - Web UI (React/TS)         │
└───────────────┬─────────────────────┬───────────────────┘
                │                     │
         ┌──────▼──────┐    ┌─────────▼────────┐
         │  Tenant DB  │    │  Quickwit Search  │
         │  (Postgres) │    │  (BM25 index)     │
         │  per tenant │    │  per model ver    │
         └─────────────┘    └──────────────────┘
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
uv run uvicorn src.server.api.main:app --reload --port 8000
```

### 6. Provision a tenant and get an API key

```bash
curl -X POST http://localhost:8000/v1/admin/tenants \
  -H "Authorization: Bearer <ADMIN_KEY from .env>" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Library", "plan": "free"}'
# Returns: tenant_id, api_key
```

### 7. Create a library, scan and enrich

```bash
uv run lumiverb config set --api-url http://localhost:8000 --api-key <your-key>
uv run lumiverb library create --name "My Photos" --path /path/to/photos
uv run lumiverb scan --library "My Photos"
uv run lumiverb enrich --library "My Photos"
```

`scan` discovers files, extracts EXIF, generates proxies and uploads them. `enrich` runs AI over the proxies: CLIP embeddings, faces, vision descriptions and OCR, and video transcription.

> **Note:** Vision descriptions and OCR need an OpenAI-compatible endpoint:
> In the web app, as an admin: Settings → AI, enter the endpoint URL, Connect, pick the model, Save.

### 8. Search

```bash
uv run lumiverb search --library "My Photos" --query "golden hour portraits"
# or via API:
curl -G http://localhost:8000/v1/query \
  --data-urlencode "f=query:golden hour portraits" \
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
│   ├── workers/               # Processing modules (used by ingest CLI)
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

# Search documents against a real Quickwit (starts a throwaway one in Docker)
uv run pytest -m quickwit

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

## Running a Linux server

`deploy-vps.sh` (above) runs `scripts/deploy-api.sh` and `scripts/deploy-web.sh`
on one machine; run them yourself to split the API and the web app across
machines. It's safe to run again. Options it passes on include `--tenant
<name>` (the account's name), `--data-dir <path>` (proxies, thumbnails and
search indexes; e.g. a mounted volume) and `--skip-certbot` (no certificate
yet: run `certbot --nginx -d app.example.com` once DNS resolves).

Its account has nobody to log in until you create the first one, with
`lumiverb-server setup` (above) or the web app's setup page, which works only
from the server itself (reach it through an SSH tunnel). Finish that before
putting a tunnel or proxy in front of Lumiverb.

### Optional: enable password reset emails

Edit `/etc/lumiverb/env`, uncomment and fill in the `SMTP_*` lines, then restart:

```bash
systemctl restart lumiverb-api
```

### Useful commands

```bash
journalctl -u lumiverb-api -f       # API server logs
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

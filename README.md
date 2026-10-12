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

Then open <http://localhost:6464> on that Mac and create your account. Setting
up follows: where previews go, who can open Lumiverb, what to find, AI,
people and your first library (a name and the folder your photos and videos
are in), each saying what skipping it costs. Lumiverb then scans and
processes the library. Descriptions, tags and
text in photos need an AI machine: [AI descriptions with Ollama](#ai-descriptions-with-ollama). The previews it makes (thumbnails,
video copies for analysis) grow with your libraries: Settings › Storage moves
them to another drive, Lumiverb working meanwhile, and a library can have a
folder of its own (its settings).

It's one service that starts again at login: Lumiverb runs its own
PostgreSQL 18 (with pgvector), Quickwit and nginx, so nothing is shared with
another PostgreSQL you run. It also brings ffmpeg, exiftool, libvips and
Python 3.12. Its log is `$(brew --prefix)/var/log/lumiverb.log`, each line
saying which part wrote it (`api | …`, `scheduler | …`), kept to 50 MB. The web app is for that Mac only to begin with; to use it from
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

A server on your home network, without a domain: [docs/linux-server.md](docs/linux-server.md).
More on the Linux install below, under [Running a Linux server](#running-a-linux-server).

### Backups

Every night Lumiverb backs up its databases: people, names, faces, ratings,
notes, projects and settings, everything only it holds. Previews and the
search index aren't in them; they're made again from the originals. A Mac
asleep at 03:00 backs up when it wakes, and a Mac's service backs up before
an upgrade's migrations. Settings › Backups shows them, makes one now, and
chooses the folder (on the machine itself); a folder on another drive keeps
them if this one fails. Kept: every one from the last 7 days, one a week for
4 weeks, and the last 3 before an upgrade.

To put one back, Settings › Backups › Restore… gives the commands for that
backup. On a Mac:

```bash
brew services stop lumiverb
```

```bash
lumiverb-server restore --env "$(brew --prefix)/etc/lumiverb/env" BACKUP_FOLDER
```

```bash
brew services start lumiverb
```

It backs up what's there first, so a restore can be undone the same way.

---

## AI descriptions with Ollama

Most of Lumiverb needs no setup: previews, search by what's in a photo,
similar photos, faces and people, video scenes and transcripts all run on
the server itself. Descriptions, tags and text in photos (signs, screens,
documents) need an **AI machine**: a computer running
[Ollama](https://ollama.com) with a vision model. Without one they wait;
nothing fails, and once you add one it catches up on everything.

The model to use is `qwen3-vl:8b-instruct` (a 6 GB download). It used
about 10 GB of memory on a Mac Studio. Take the `-instruct` one: plain
`qwen3-vl:8b` thinks before it answers, and the thinking uses up the
answer, so most descriptions come back empty. With less memory,
`qwen3-vl:4b-instruct` is smaller (not tested here).

On a Mac:

```bash
brew install ollama
```

```bash
brew services start ollama
```

```bash
ollama pull qwen3-vl:8b-instruct
```

(Or the app from [ollama.com/download](https://ollama.com/download), then the
`ollama pull`.)

On Linux, install the NVIDIA driver first (`nvidia-smi` works): without it
Ollama runs on the CPU, where a photo takes minutes. Then:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

```bash
ollama pull qwen3-vl:8b-instruct
```

`ollama ps` shows whether the model runs on the GPU.

Then, as an admin, in Lumiverb:

1. **Settings › AI › Add machine**: a name and the URL, then **Connect**. It
   lists the models the machine has.
2. Under **Does**, tick **Descriptions & text**, then **Add**.
3. **Settings › AI › Models**, Descriptions & text: `qwen3-vl:8b-instruct`.

The URL is `http://localhost:11434/v1` when Ollama runs on the same computer
as Lumiverb. On another computer it's that computer's address, such as
`http://192.168.1.20:11434/v1`, and Ollama there must listen on the network.
Ollama has no password, so do that only on a network you trust. On Linux,
add `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]`:

```bash
sudo systemctl edit ollama.service
```

```bash
sudo systemctl restart ollama
```

On a Mac, see [Ollama's FAQ](https://docs.ollama.com/faq).

**Requests at once** is how many photos it works on together. Leave it at 1 unless
that Ollama runs several in parallel (`OLLAMA_NUM_PARALLEL`); each one takes
more memory.

Transcripts don't need Ollama: Lumiverb's own Whisper does them. To make
them faster with another GPU, see [AI machines](docs/linux-server.md#6-ai-machines).

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

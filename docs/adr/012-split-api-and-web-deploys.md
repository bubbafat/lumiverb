# ADR-012: Split API and Web Deploys

## Status

Proposed

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | API-only deploy and update scripts | Not started |
| 2 | Web-only deploy and update scripts | Not started |
| 3 | Retire monolithic scripts | Not started |

## Overview

Today Lumiverb runs as a single deployment unit on one VPS: nginx serves the static React SPA and proxies `/v1/*` to the FastAPI backend, which shares the machine with PostgreSQL and Quickwit. A single `deploy-vps.sh` bootstraps everything; a single `update-vps.sh` pulls code, rebuilds the UI, runs migrations, and restarts the API. Any change to any layer requires redeploying the whole stack.

This ADR splits the deployment into two independent units — **API** (Python/FastAPI + PostgreSQL + Quickwit) and **Web** (static React SPA served by nginx) — so that each can be deployed, updated, and scaled on separate machines. The user handles network connectivity between them; the scripts handle everything else.

After this change, a CSS tweak ships in seconds (Web update: `npm run build` + nginx reload) without touching the API process. A migration ships without rebuilding the frontend. And when the library grows, the API machine can be upsized independently of the web server.

## Motivation

- **Coupled release cycle**: Changing a button color in the React UI requires `update-vps.sh` to also run `uv sync`, `alembic upgrade head`, and restart uvicorn — a ~60-second outage window for a zero-risk frontend change.
- **Resource contention**: On a $12/month VPS, PostgreSQL, Quickwit, uvicorn (2 workers), nginx, and the Node.js build toolchain compete for 2GB RAM. Moving the web tier to a separate (smaller, cheaper) machine frees resources for the database and search engine.
- **Independent scaling**: The API is I/O-bound (database queries, file storage). The web tier is bandwidth-bound (serving static assets to browsers). These scale differently and benefit from separate machines/CDN.
- **Downtime isolation**: API restarts (migrations, dependency updates) don't affect the web UI. The SPA continues working from browser cache and shows a connection error banner only when API calls fail — better UX than a full page outage.
- **Native app preparation**: A macOS native client talks directly to the API. When the web UI lives on a separate machine, the API's network exposure is already correct — no nginx in the request path for programmatic clients.

## Design

### Architecture

**Current (monolithic):**
```
Internet ──► nginx (one machine)
               ├── /v1/* ──► uvicorn:8000
               ├── /*    ──► /opt/lumiverb/src/ui/web/dist
               │
               ├── PostgreSQL:5432 (localhost)
               └── Quickwit:7280 (localhost)
```

**Proposed (split):**
```
                         ┌─── API Machine ──────────────────┐
                         │  uvicorn:8000 (0.0.0.0 or LAN)  │
Internet ──► Web Machine │  PostgreSQL:5432 (localhost)      │
             nginx:443   │  Quickwit:7280 (localhost)        │
             ├── /v1/* ──┤  upkeep timers                    │
             ├── /* ──► dist/                                │
             └───────────┘──────────────────────────────────┘
```

The Web machine runs nginx and serves the static SPA. It proxies `/v1/*` to the API machine over the network (private VPC, WireGuard, or public with TLS — user's choice). PostgreSQL and Quickwit stay on the API machine, listening on localhost only.

**Same-machine mode still works.** If both scripts target the same machine, the result is functionally identical to today's monolithic deploy — nginx proxies to `127.0.0.1:8000`. The split is a deployment option, not a requirement.

### Environment Variables

**API machine** (`/etc/lumiverb/env`):

Unchanged from today. All existing env vars remain. One new variable:

| Variable | Default | Purpose |
|----------|---------|---------|
| `API_LISTEN_HOST` | `127.0.0.1` | Bind address for uvicorn. Set to `0.0.0.0` or a LAN IP when the web tier is on a separate machine. |

The API machine does not need Node.js, npm, or any frontend toolchain.

**Web machine** (`/etc/lumiverb-web/env`):

| Variable | Required | Purpose |
|----------|----------|---------|
| `API_UPSTREAM` | Yes | Full URL of the API server (e.g., `http://10.0.0.5:8000`, `https://api-internal.lumiverb.io`). Used in nginx `proxy_pass`. |
| `DOMAIN` | Yes | Public domain for TLS cert (e.g., `app.lumiverb.io`). |

The Web machine does not need Python, uv, PostgreSQL, Quickwit, or the env secrets (ADMIN_KEY, JWT_SECRET, etc.). It only needs nginx, Node.js (for builds), and git (to pull the repo for the UI source).

### Scripts

Four scripts replace the current two:

| Script | Machine | Purpose |
|--------|---------|---------|
| `scripts/deploy-api.sh` | API | Bootstrap: PostgreSQL, Quickwit, Python, uv, migrations, systemd units, firewall. No nginx, no Node.js, no UI build. |
| `scripts/update-api.sh` | API | Pull, `uv sync --extra cli`, migrations, restart uvicorn + Quickwit. No UI build. |
| `scripts/deploy-web.sh` | Web | Bootstrap: nginx, Node.js, TLS cert, clone repo, `npm ci && npm run build`, systemd (nginx only). No Python, no database. |
| `scripts/update-web.sh` | Web | Pull, `npm ci && npm run build`, reload nginx. No Python, no migrations, no service restart. Sub-second downtime (nginx reload is graceful). |

### deploy-api.sh

Extracts from the current `deploy-vps.sh` everything except:
- Node.js installation (Stage 1)
- Web UI build (Stage 10)
- nginx configuration (Stage 12 — but see below)

**Firewall**: Opens port `8000/tcp` in addition to `22/tcp`. Does NOT open `80/tcp` or `443/tcp` (no nginx on this machine). When on a private VPC, the user may further restrict `8000/tcp` to the web machine's IP via `--api-allow-from`.

**Parameters:**
```
deploy-api.sh --domain <api-domain-or-ip> \
              --data-dir /mnt/lumiverb/data \
              [--api-listen-host 0.0.0.0] \
              [--api-allow-from 10.0.0.0/24] \
              [--branch main] \
              [--tenant default]
```

`--domain` is used for the `APP_HOST` env var (so the API knows its own URL for things like password reset links). It does not set up TLS — the API is expected to be behind a private network or the web machine's TLS-terminating nginx.

**systemd units created:**
- `lumiverb-api.service` (uvicorn, binds to `API_LISTEN_HOST`)
- `lumiverb-quickwit.service`
- `lumiverb-upkeep.timer`
- `lumiverb-upkeep-daily.timer`
- `lumiverb-worker.service` (disabled by default, as today)

### update-api.sh

Steps (subset of current `update-vps.sh`):

1. `git pull`
2. `uv sync --extra cli`
3. Control plane migrations (`alembic upgrade head`)
4. Tenant migrations (`scripts/migrate.sh`)
5. Quickwit data dir sync
6. Restart `lumiverb-api`, conditionally restart `lumiverb-quickwit` and `lumiverb-worker`
7. Health check: poll `http://127.0.0.1:8000/health`

No `npm ci`, no `npm run build`, no nginx.

### deploy-web.sh

Fresh script. Bootstraps a web-only machine.

**Parameters:**
```
deploy-web.sh --domain app.lumiverb.io \
              --api-upstream http://10.0.0.5:8000 \
              [--email admin@example.com] \
              [--branch main] \
              [--skip-certbot] \
              [--certificate /path/to/letsencrypt.tar.gz]
```

**Steps:**

1. Install system packages: nginx, certbot, git, Node.js 20
2. Clone repo (or `git pull` if `/opt/lumiverb` exists)
3. `npm ci --no-audit --no-fund && npm run build` in `src/ui/web`
4. Write nginx config:
   ```nginx
   server {
       listen 80;
       server_name <DOMAIN>;

       root /opt/lumiverb/src/ui/web/dist;
       index index.html;

       add_header X-Content-Type-Options nosniff always;
       add_header X-Frame-Options DENY always;
       add_header Referrer-Policy no-referrer-when-downgrade always;

       location /v1/ {
           proxy_pass <API_UPSTREAM>;
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
   ```
5. TLS certificate (certbot or restore from archive)
6. Firewall: `22/tcp`, `80/tcp`, `443/tcp`
7. Enable and start nginx

**No systemd service units for Python processes.** No PostgreSQL, no Quickwit, no uv, no `.venv`.

### update-web.sh

The simplest script of the four.

**Steps:**

1. `git pull` (or skip if detached HEAD)
2. `npm ci --no-audit --no-fund && npm run build` in `src/ui/web`
3. `nginx -s reload`

Total time: ~10 seconds. Zero API downtime. nginx graceful reload means existing connections complete before workers pick up the new config.

**Parameters:**
```
update-web.sh [--branch main]
```

No `--api-upstream` on update — it was baked into the nginx config at deploy time. To change the upstream, re-run `deploy-web.sh` or edit `/etc/nginx/sites-available/lumiverb` directly.

### API CORS Configuration

When the web UI and API are on different machines, the browser sees same-origin requests (the browser talks to the web machine's nginx, which proxies to the API). No CORS headers are needed because the proxy makes all `/v1/*` requests same-origin from the browser's perspective. This is the same as today.

If a future client (e.g., native macOS app) calls the API directly, CORS is not relevant (native apps don't enforce CORS). No CORS configuration changes are needed for this ADR.

### Health Checking

**API machine**: Existing `GET /health` endpoint on uvicorn. `update-api.sh` polls it after restart.

**Web machine**: `update-web.sh` verifies nginx is running and the dist directory exists. Optionally curls the API upstream health endpoint to verify connectivity:
```bash
curl -sf "${API_UPSTREAM}/health" || echo "WARNING: API upstream not reachable"
```
This is a warning, not a failure — the web machine may deploy before the API machine is up.

### Upkeep Timers

Both timers (`lumiverb-upkeep.timer`, `lumiverb-upkeep-daily.timer`) stay on the API machine. They curl `http://127.0.0.1:8000/v1/upkeep` using `ADMIN_KEY` from the env file. No change needed — they already target localhost.

### Data and Storage

**API machine owns all data:**
- PostgreSQL databases (control plane + tenants)
- Quickwit indexes (`${DATA_DIR}/quickwit`)
- Object storage (proxy images, thumbnails, video previews at `${DATA_DIR}/storage`)
- Env secrets (`/etc/lumiverb/env`)
- Alembic migration state

**Web machine has no persistent data** beyond:
- The git checkout (`/opt/lumiverb`)
- The built SPA (`src/ui/web/dist`)
- nginx config and TLS certs

If the web machine is destroyed and redeployed, nothing is lost.

### Migration from Monolithic Deploy

Existing machines bootstrapped with `deploy-vps.sh` need a smooth upgrade path. The key constraint: running `update-vps.sh` on an existing machine must never break it.

**Day-one behavior (Phase 3 complete):**

`update-vps.sh` becomes a thin wrapper that calls `update-api.sh` then `update-web.sh` in sequence. On an existing monolithic machine, both scripts find their respective infrastructure already in place (nginx, PostgreSQL, Quickwit, uvicorn, Node.js) and do exactly what the old monolithic script did, just in two halves. The user notices no difference.

`deploy-vps.sh` becomes a thin wrapper that calls `deploy-api.sh` then `deploy-web.sh --api-upstream http://127.0.0.1:8000`. A fresh single-machine deploy produces the same result as today.

**Splitting to two machines (manual, when ready):**

This is a one-time migration when the user decides to separate. The steps:

1. Provision a new web machine
2. Run `deploy-web.sh --domain app.lumiverb.io --api-upstream http://<api-ip>:8000` on the new machine
3. On the API machine, set `API_LISTEN_HOST=0.0.0.0` in `/etc/lumiverb/env` and open port 8000 in the firewall (or scope to the web machine's IP)
4. Restart the API: `systemctl restart lumiverb-api`
5. Update DNS to point `app.lumiverb.io` at the new web machine
6. On the old machine, stop and disable nginx: `systemctl disable --now nginx`
7. Going forward: run `update-api.sh` on the API machine, `update-web.sh` on the web machine

There is no automated migration script — this is a deliberate infrastructure decision with a DNS cutover. The steps are documented but not scripted because the network topology (VPC, WireGuard, public IP) varies per user.

**Rollback:** If the split doesn't work out, point DNS back at the original machine, re-enable nginx, and go back to `update-vps.sh`. The original machine was never modified destructively.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Both scripts target the same machine | Works. nginx proxies to `127.0.0.1:8000`. Functionally identical to current monolithic deploy. |
| API machine reboots while web is up | SPA continues serving from browser cache. API calls fail with connection errors. SPA should show a connection error state (existing behavior for network failures). API recovers on systemd restart. |
| Web machine reboots while API is up | API continues serving CLI clients and native apps. Web users get connection timeout until nginx comes back (systemd auto-start). |
| API migrates database (breaking change) | Deploy sequence matters: `update-api.sh` first, then `update-web.sh` if the UI needs changes for the new API. Same as today — migrations run before restart. |
| Web deployed before API (new feature) | SPA may call endpoints that don't exist yet. API returns 404. SPA should handle this gracefully (show error, not crash). Deploy API first for breaking changes. |
| `deploy-web.sh` run on a machine that already has `deploy-api.sh` | Works — adds nginx + Node.js to the existing API machine. The result is a combined machine (same as current monolithic setup). |
| API machine IP changes | Re-run `deploy-web.sh` with new `--api-upstream`, or manually edit nginx config and reload. |
| TLS cert renewal | certbot auto-renewal runs on the web machine (where nginx is). API machine has no TLS. |
| Multiple web machines (future) | Each runs `deploy-web.sh` with the same `--api-upstream`. Load balancing is the user's responsibility (DNS round-robin, external LB, etc.). |
| CLI client bypasses web machine | CLI talks directly to the API machine (as today). No change — CLI uses `api_url` from `~/.lumiverb/config.json`, which should point to the API machine or a load balancer. |
| Existing monolithic machine runs new `update-vps.sh` | Wrapper calls `update-api.sh` then `update-web.sh`. Both find existing infrastructure in place. Behavior identical to old monolithic script. No migration needed. |
| User splits to two machines then wants to go back | Point DNS back to original machine, re-enable nginx. Original machine was never modified destructively — it still has nginx config, Node.js, and the built SPA. |
| Quickwit on separate machine (future) | Out of scope for this ADR. Would require `QUICKWIT_URL` to point to a remote host. The API already supports this via env var — no code change, just config. |

## Code References

| Area | File | Notes |
|------|------|-------|
| Current bootstrap | `scripts/deploy-vps.sh` | 708 lines, all 16 stages. Split into deploy-api.sh + deploy-web.sh. |
| Current update | `scripts/update-vps.sh` | 201 lines, 9 steps. Split into update-api.sh + update-web.sh. |
| App config | `src/core/config.py` | `Settings` class. Add `api_listen_host` field. |
| API entry point | `src/api/main.py` | uvicorn startup, reads settings. |
| Vite dev proxy | `src/ui/web/vite.config.ts` | Dev-only, no production impact. |
| systemd units | Embedded in `deploy-vps.sh` | Extract to deploy-api.sh. |
| nginx config | Embedded in `deploy-vps.sh` | Extract to deploy-web.sh, parameterize `proxy_pass`. |
| Docker compose | `docker-compose.yml` | Local dev only, no changes needed. |

## Doc References

- `docs/architecture.md` — Update deployment section with split topology diagram
- `docs/cursor-cli.md` — No change (CLI talks to API directly)
- `docs/cursor-api.md` — No change (API contract unchanged)

## Build Phases

### Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Scripts**: New scripts must be idempotent (safe to re-run). Must work on Ubuntu 22.04+.
2. **Tests**: Existing test suite must still pass (`uv run pytest tests/`). No new application code tests are needed (this is infrastructure-only).
3. **Backward compatibility**: Running `deploy-api.sh` + `deploy-web.sh` on the same machine must produce a setup functionally identical to current `deploy-vps.sh`.
4. **Documentation**: Architecture docs updated with the split topology.
5. **Progress**: The phase status table above is updated when a phase completes.

### Phase 1 — API Deploy and Update Scripts

**Deliverables:**
- `scripts/deploy-api.sh`: Bootstrap an API-only machine. All stages from `deploy-vps.sh` except Node.js install, npm build, and nginx config. Adds `API_LISTEN_HOST` env var and `--api-listen-host` / `--api-allow-from` parameters.
- `scripts/update-api.sh`: Pull, sync deps, migrate, restart. No npm/nginx. Subset of current `update-vps.sh`.
- `API_LISTEN_HOST` setting in `src/core/config.py` (default `127.0.0.1`)
- systemd unit for `lumiverb-api.service` uses `API_LISTEN_HOST` for uvicorn bind address
- Firewall opens `8000/tcp` (optionally scoped to `--api-allow-from`)

**Does NOT include:** Web deploy scripts, nginx configuration, Node.js toolchain.

**Read-ahead:** Phase 2 needs the API machine's address to configure `proxy_pass`. Phase 1 must document the expected API_UPSTREAM format (scheme + host + port).

**Done when:**
- [ ] `deploy-api.sh` bootstraps a working API server from a clean Ubuntu 22.04 machine
- [ ] `update-api.sh` updates and restarts the API without touching web/nginx
- [ ] API healthcheck passes after both scripts
- [ ] Existing test suite passes
- [ ] Phase status updated above

### Phase 2 — Web Deploy and Update Scripts

**Deliverables:**
- `scripts/deploy-web.sh`: Bootstrap a web-only machine. Installs nginx + Node.js, clones repo, builds SPA, writes nginx config with parameterized `proxy_pass`, sets up TLS.
- `scripts/update-web.sh`: Pull, rebuild SPA, `nginx -s reload`. Sub-10-second updates.
- nginx config uses `API_UPSTREAM` parameter for `proxy_pass` directive
- Firewall: 22/tcp, 80/tcp, 443/tcp only
- TLS cert support (certbot + archive restore)

**Does NOT include:** Python, uv, PostgreSQL, Quickwit, systemd service units (except nginx).

**Read-ahead:** Phase 3 deprecates the old scripts. Phase 2 must verify that deploy-api.sh + deploy-web.sh on the same machine produces the same result as deploy-vps.sh.

**Done when:**
- [ ] `deploy-web.sh` bootstraps a working web server from a clean Ubuntu 22.04 machine
- [ ] `update-web.sh` rebuilds and reloads in under 30 seconds
- [ ] Web server proxies `/v1/*` to the API machine correctly
- [ ] SPA loads and functions correctly through the split proxy
- [ ] Same-machine deployment (both scripts on one host) works identically to current setup
- [ ] Phase status updated above

### Phase 3 — Retire Monolithic Scripts

**Deliverables:**
- `deploy-vps.sh` becomes a thin wrapper: calls `deploy-api.sh` then `deploy-web.sh` with `--api-upstream http://127.0.0.1:8000`. All parameters forwarded. No duplicated logic.
- `update-vps.sh` becomes a thin wrapper: calls `update-api.sh` then `update-web.sh`. Same behavior as today but implemented via composition.
- Existing deploy command in memory/docs continues to work unchanged
- The wrapper scripts are the **primary interface for single-machine users** going forward. They are not deprecated — they are the recommended path for anyone who doesn't need separate machines. The split scripts exist for when you're ready to separate.

**Migration story:** An existing monolithic machine bootstrapped with the old `deploy-vps.sh` can run the new `update-vps.sh` with no preparation. The wrapper detects existing infrastructure and delegates correctly. See "Migration from Monolithic Deploy" in the Design section for the full upgrade and split workflow.

**Does NOT include:** Removing the wrapper scripts. They remain as the single-machine convenience path indefinitely.

**Done when:**
- [ ] `deploy-vps.sh` and `update-vps.sh` are thin wrappers delegating to the new scripts
- [ ] Existing deploy workflow (`curl ... | sudo bash -s -- --domain app.lumiverb.io ...`) still works
- [ ] Running new `update-vps.sh` on an existing monolithic machine produces identical results to the old script
- [ ] Architecture docs updated with split topology
- [ ] Phase status updated above

## Alternatives Considered

**CDN for static assets (CloudFront, Cloudflare Pages).** Would eliminate the web machine entirely — SPA served from CDN edges, API called directly. Rejected because it requires CORS configuration on the API (browser calls cross-origin), complicates auth token handling, and adds a third-party dependency. The nginx proxy approach keeps everything same-origin and under user control. Could revisit as a future optimization on top of this split.

**Docker Compose with separate containers.** Run API and web as separate Docker services on the same machine. Achieves process isolation but not machine-level separation. Also adds Docker as a dependency on the VPS (currently not required). The script-based approach is simpler and already works with the existing systemd + nginx setup.

**Reverse proxy on the API machine instead of the web machine.** Put nginx on the API machine, serve static files from there, and just have a lightweight redirect/CDN for the web tier. Rejected because it doesn't achieve the goal — the API machine still needs Node.js for builds and still restarts for frontend changes.

**API gateway (Caddy, Traefik) replacing nginx.** Would simplify TLS (Caddy's automatic HTTPS) but introduces a new dependency. nginx is already deployed, well-understood, and the config is minimal. Not worth the migration cost for this change.

## What This Does NOT Include

- **Database replication or read replicas** — PostgreSQL stays on the API machine. If the database needs its own machine, that's a separate ADR.
- **Quickwit on a separate machine** — Stays on the API machine. Already configurable via `QUICKWIT_URL` env var if someone wants to split it later.
- **Container orchestration (Docker, K8s)** — Scripts remain bash + systemd. Containerization is a separate concern.
- **CDN or edge caching** — Static assets are served by nginx on the web machine. A CDN in front of nginx is the user's choice and doesn't require application changes.
- **Auto-scaling** — Fixed machine count. Scaling up means manually running `deploy-web.sh` on another machine.
- **Blue-green or rolling deployments** — Updates are in-place. Downtime is minimal (nginx reload for web, uvicorn restart for API) but not zero for the API tier.
- **Monitoring or alerting** — No Prometheus, Grafana, or health-check services. The health endpoint exists; wiring it to monitoring is the user's responsibility.

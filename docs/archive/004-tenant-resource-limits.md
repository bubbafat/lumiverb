> **Archived 2026-03-29.** This ADR assumed server-side worker queues which no longer exist. All processing now happens client-side via `lumiverb ingest`. The problems described (worker concurrency saturation, job queue abuse, `worker_jobs` table bloat) are no longer applicable. If multi-tenant API rate limiting is needed in the future, it should be a new ADR addressing API request rate limiting, not worker concurrency. Upload size limits (Section 4.1) remain valid and can be extracted into a new ADR if needed.

# ADR-004: Tenant Resource Limits and Fair Use Protection

## Status
Obsolete — archived

## Context

With workers now running remotely with parallel connections (concurrency 4+), a single tenant can saturate the server's CPU, memory, disk I/O, and network bandwidth. This is fine for a single-tenant deployment but becomes a problem as we onboard multiple tenants. A misbehaving or overly aggressive client — intentional or not — could degrade service for everyone.

The 10x speedup from concurrent workers is great, but it means we need server-side guardrails.

## Problems to Solve

### 1. Connection/request rate limits
- A tenant running `--concurrency 20` could monopolize the API server's connection pool.
- No per-tenant rate limiting exists today. A tight claim/complete loop can generate hundreds of requests per minute.
- Burst vs sustained: scanning 3,000 assets is a legitimate burst; sustained high-rate polling is not.

### 2. Upload size limits
- `MAX_UPLOAD_BYTES` is 100 MB (global ceiling) but there are no per-type limits.
- Reasonable limits: proxy ~2 MB, thumbnail ~200 KB, scene_rep ~500 KB, video_preview ~20 MB.
- A malicious client could upload 100 MB "thumbnails" and fill the disk.
- The batch endpoint multiplies this — N files per request.

### 3. Storage quotas
- No per-tenant storage quota. A tenant could upload proxies for millions of assets and consume all disk.
- Block storage on DigitalOcean is finite and shared across tenants.
- Need: per-tenant storage accounting and a soft/hard limit.

### 4. Job queue abuse
- No limit on how many jobs a tenant can have pending. Enqueueing millions of jobs could bloat the worker_jobs table.
- `--force` re-enqueue creates new jobs without bound.

### 5. Concurrent worker limits
- The server has no way to limit how many workers a tenant runs simultaneously.
- Could enforce via max concurrent claimed jobs per tenant (server rejects claim when at limit).

## Proposed Solutions (to be refined)

### Phase 1: Low-hanging fruit (server-side, no client changes)
- **Per-type upload size limits** — enforce in the upload endpoints. Reject with 413.
- **Max concurrent claims per tenant** — reject `GET /v1/jobs/next` with 429 when a tenant has too many claimed (in-progress) jobs. Client retries naturally.
- **Per-tenant request rate limit** — middleware that tracks requests per tenant per window. Return 429 with Retry-After header.

### Phase 2: Quotas and accounting
- **Storage accounting** — track bytes stored per tenant (increment on upload, decrement on delete). Enforce soft limit (warning) and hard limit (reject uploads).
- **Job queue depth limit** — reject enqueue when pending job count exceeds threshold.
- **Plan-based limits** — tie limits to tenant plan (free/pro/enterprise). Store limits on the tenant record.

### Phase 3: Observability
- **Per-tenant metrics** — requests/sec, bytes uploaded, active workers, storage used.
- **Admin dashboard** — surface in web UI settings or a dedicated admin page.
- **Alerts** — notify admin when a tenant is approaching limits.

## Design Principles
- Limits enforced server-side only — never trust the client.
- 429 (Too Many Requests) with Retry-After for rate/concurrency limits. Clients already retry on failure.
- 413 (Payload Too Large) for upload size violations.
- Soft limits log warnings; hard limits reject requests.
- Limits configurable per plan, not hardcoded.

## Decision

Deferred — this ADR captures the design space. Priority: per-type upload size limits (Phase 1) are cheap and should be done soon. Rate limiting and quotas can wait until multi-tenant is closer.

## Consequences

Until this is implemented:
- A single tenant with high concurrency can saturate the server.
- No protection against oversized uploads beyond the 100 MB global ceiling.
- No storage quota enforcement.
- Operators must monitor resource usage manually.

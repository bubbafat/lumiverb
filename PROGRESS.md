# Lumiverb — Build Progress

## Steps

### Phase 1: Foundation (CLI + API)

| Step | Description | Status |
|------|-------------|--------|
| 1 | Control plane schema, Alembic setup, migration tests | ✅ Done |
| 2 | Tenant schema, phase 2 stubs, migration tests | ✅ Done |
| 3 | Config, database layer, repositories, provisioning API | ✅ Done |
| 4 | Library and asset scanner (CLI) | ✅ Done |
| 5 | Proxy and thumbnail worker | ✅ Done |
| 6 | Library soft delete / trash | ✅ Done |
| 7 | EXIF metadata worker | ✅ Done |
| 8 | AI vision worker (Moondream) | ✅ Done |
| 9 | Quickwit search sync worker | ✅ Done |
| 10 | Search API endpoint | ✅ Done |
| 11 | Similarity search | ✅ Done |
| 12 | Video scene segmentation worker | ✅ Done |
| 13 | CLI polish and end-to-end test | ✅ Done |

### Phase 2: Pre-UI Test Coverage

| Step | Description | Status |
|------|-------------|--------|
| 14 | API test coverage gap fill (assets, scans, jobs, video, auth) | ✅ Done |

### Phase 3: Progressive Pipeline

| Step | Description | Status |
|------|-------------|--------|
| 15 | Progressive availability: job priority ordering, asset status lifecycle, video fast-path preview | 🔜 Next |

### Phase 4: Web UI (React + TypeScript + Tailwind)

UI lives at `src/ui/web/`. Vite dev server proxies `/v1/` to `http://localhost:8000`. Stack: React 18, TypeScript, Tailwind CSS v3, React Router v6, TanStack Query v5, @tanstack/react-virtual.

| Step | Description | Status |
|------|-------------|--------|
| 16 | UI scaffold + library management (add/list/delete/trash) | ✅ Done |
| 17 | Admin: multi-key support per tenant + CLI admin commands | ✅ Done |
| 18 | App shell redesign: sidebar (libraries, worker status, filters) + routing | 🔜 Planned |
| 19 | Justified grid with date groups + virtualization + infinite scroll | 🔜 Planned |
| 20 | Lightbox: image + 10s video preview, metadata panel, search term highlighting | 🔜 Planned |
| 21 | Search: query bar, results in justified grid, term highlighting | 🔜 Planned |
| 22 | Similarity: "find similar" from lightbox, results grid | 🔜 Planned |
| 23 | Worker & job status panel (sidebar functional) | 🔜 Planned |
| 24 | User preferences page (localStorage-backed, server-ready) | 🔜 Planned |
| 25 | Timeline scrubber (right-edge, month/year markers, drag to jump) | 🔜 Planned |
| 26 | Selection mode + bulk actions | 🔜 Planned |

### Phase 5: Security

| Step | Description | Status |
|------|-------------|--------|
| 27 | RBAC ADR: tenant roles (admin, editor, viewer), API key role field, middleware enforcement, UI permission gates | 🔜 Planned |

### Phase 6: Mac Agent / Auto-ingest

| Step | Description | Status |
|------|-------------|--------|
| 28 | Watchdog daemon: FSEvents-based file watcher, auto-scan on change, launchd service | 🔜 Planned |

---

## Architecture Decisions Log

| Decision | Rationale |
|----------|-----------|
| Per-tenant isolated Postgres databases | Cross-tenant data leakage architecturally impossible |
| Alembic two-ini setup (control + tenant) | Clean separation, independent migration chains |
| Admin key separate from tenant API keys | Provisioning is an operator action, not a tenant action |
| python-ulid for IDs (ten_, lv_, key_ prefixes) | Sortable, prefixed for debuggability |
| provision_tenant_database via subprocess | Reuses exact same Alembic path as tests |
| Module-scoped test containers | One container per file, unique ULIDs prevent state bleed |
| Library soft delete: status=trashed, cancel jobs on trash | User can recover until empty-trash; pending/claimed worker jobs cancelled immediately on trash |
| Hard delete order: worker_jobs → search_sync_queue → asset_metadata → video_scenes → assets → scans → libraries | FK-safe cascade in a single transaction |
| Quickwit purge: stub until search (Step 9) | purge_library_from_quickwit logs warning and returns; no-op until search implemented |
| Maintenance worker (orphaned files, Postgres/Quickwit sync, stuck scan cleanup) | Deferred |
| Multi-key per tenant | Supports multiple users/devices; keys stored as SHA256 hash, plaintext returned once only |
| UI at src/ui/web/ | Leaves room for other UIs (Mac agent etc.) under src/ui/ |
| Vite proxy for dev | Avoids CORS, UI talks to local Docker stack transparently |
| Authenticated image fetching via useAuthenticatedImage hook | img src can't send auth headers; hook fetches via apiFetch and returns object URL |
| Virtualized justified grid (@tanstack/react-virtual) | 25,000+ assets; justified layout matches Google Photos UX north star |
| Asset status lifecycle: pending → proxy_ready → described → indexed | Enables progressive UI display; each pipeline stage advances status |
| Job priority field on worker_jobs | Proxy jobs always claimed before vision/search-sync; fast time-to-first-display |
| Video fast-path: 10s preview as high-priority job | Full scene segmentation deferred; preview available in seconds |
| Corporate/operator management UI | Out of scope for this project |
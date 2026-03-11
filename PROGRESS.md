# Lumiverb — Build Progress

## Steps

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
| 11 | Similarity search | ⬜ Planned |
| 12 | Video scene segmentation worker | ⬜ Planned |
| 13 | CLI polish and end-to-end test | ⬜ Planned |

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

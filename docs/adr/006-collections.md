# ADR-006: Collections

## Status
Proposed

## Context

Lumiverb has libraries (filesystem-backed containers of assets) but no way to curate subsets of assets across libraries. Users need virtual groupings — "Best of Europe," "Portfolio selects," "Share with client" — that are independent of folder structure.

This is modeled after Google Photos albums:
- Assets can appear in multiple collections
- Collections span libraries
- Deleting a source asset removes it from all collections
- Collections can be shared publicly even when the source library is private
- Collections are a tenant-level concept

Collections are the foundation for future features: ratings/picks (a "Favorites" collection), smart collections (saved auto-updating queries), and client delivery.

## Design

### Data Model

**Tenant database (new tables):**

```
collections
  collection_id    PK, text (col_+ULID)
  name             text, not null
  description      text, nullable
  cover_asset_id   FK → assets.asset_id, nullable (user-set; null = use first item)
  owner_user_id    text, nullable (user who created; NULL = legacy tenant-wide)
  visibility       text, default 'private' (private | shared | public)
  sort_order       text, default 'manual' (manual | added_at | taken_at)
  created_at       timestamptz
  updated_at       timestamptz

collection_assets
  collection_id    FK → collections.collection_id ON DELETE CASCADE, not null
  asset_id         FK → assets.asset_id ON DELETE CASCADE, not null
  position         int, not null (for manual ordering; 0-based)
  added_at         timestamptz
  PRIMARY KEY (collection_id, asset_id)
  INDEX on (asset_id) for fast cascade lookups
```

**Control plane (extend existing):**

```
public_collections
  collection_id    PK, text
  tenant_id        FK → tenants.tenant_id
  connection_string text
  created_at       timestamptz
```

Mirrors the existing `public_libraries` pattern — when a collection is made public, a row is inserted; when made private, it's removed. Public collection access resolves the tenant DB via this table without requiring auth.

### Asset Count

No denormalized `asset_count` column. Asset counts are computed at query time via `COUNT(*) JOIN assets WHERE deleted_at IS NULL`. This is always correct regardless of soft deletes, and avoids count drift. The `collection_assets(collection_id)` index makes this fast. If performance becomes an issue at scale, add caching then.

### Asset Lifecycle

- **Adding an asset to a collection**: Insert into `collection_assets` with `ON CONFLICT DO NOTHING` (idempotent). Position set to `COALESCE(MAX(position), -1) + 1`. Only active assets can be added — reject trashed assets (404).
- **Removing an asset from a collection**: Delete from `collection_assets`. Does not affect the source asset.
- **Soft-deleting (trashing) a source asset**: The `collection_assets` row is NOT removed. Instead, all collection queries JOIN through `assets WHERE deleted_at IS NULL`, so trashed assets disappear from collection views automatically. The `collection_assets` row persists so that restoring the asset also restores its collection membership and position.
- **Hard-deleting a source asset (empty trash)**: `ON DELETE CASCADE` removes the `collection_assets` row permanently.
- **Trashing a library**: All library assets get `deleted_at` set. They disappear from collections via the query-time filter. No special collection logic needed.
- **Deleting a collection**: Deletes the collection and all `collection_assets` rows (CASCADE). Source assets are untouched.
- **Making a collection public then private**: Remove from `public_collections`. URL immediately returns 404. No grace period.
- **Deleting a public collection**: Also removes from `public_collections` control plane table in the same transaction.
- **Empty collection**: Persists. A collection with 0 assets is valid (user may re-populate it).

### Cover Image

- Default: first asset by `position` order
- User can explicitly set `cover_asset_id`
- API always returns a resolved cover (explicit if valid, otherwise first-by-position)
- **Stale cover repair**: resolved on read, not on write. When the API serves a collection and detects `cover_asset_id` points to a deleted or removed asset, it returns the first-by-position fallback and nulls out `cover_asset_id` in the same request (lazy self-healing UPDATE). No cascading hooks on asset deletion, no background jobs. At most one read sees the fallback before the column is cleaned.

### Ordering

- Default sort: `manual` (user-defined `position` values)
- Alternative sorts: `added_at`, `taken_at` (computed at query time, position ignored)
- Reordering: `PATCH /v1/collections/{id}/reorder` accepts `{ asset_ids: [...] }` — server assigns sequential positions. **Must include all asset IDs in the collection** — partial reorder is rejected (400) to avoid ambiguity.
- When `sort_order` is `manual`, the `position` column determines display order
- New assets added to a `manual` collection go to the end
- Concurrent adds: position assigned via `COALESCE(MAX(position), -1) + 1` — composite PK prevents duplicates

### Public Collection Access

Public collections create a new authorization path. An unauthenticated request can access assets that belong to a private library if those assets are in a public collection.

**Auth flow for public collection endpoints:**
1. Request hits `/v1/public/collections/{collection_id}`
2. Server looks up `public_collections` table (no auth required)
3. Routes to tenant DB
4. Returns collection metadata + asset list
5. Proxy/thumbnail endpoints accept `?collection_id=` param for public access

This is a separate URL namespace (`/v1/public/collections/`) — not mixed into the authenticated collection endpoints.

**What's exposed publicly:**
- Collection name, description, asset count
- Asset thumbnails and proxies (via collection membership check)
- Asset metadata (dimensions, media type, taken_at)

**What's NOT exposed:**
- Library structure, names, or paths
- Other collections
- EXIF details beyond what's shown (GPS, camera info — TBD, may want to strip for privacy)
- Source files (never, same as libraries)

### API Endpoints

**Authenticated (tenant auth required):**

```
POST   /v1/collections                         Create collection
GET    /v1/collections                         List collections (name, cover, count)
GET    /v1/collections/{id}                    Get collection detail
PATCH  /v1/collections/{id}                    Update name, description, is_public, sort_order, cover_asset_id
DELETE /v1/collections/{id}                    Delete collection

POST   /v1/collections/{id}/assets             Add assets { asset_ids: [...] }
DELETE /v1/collections/{id}/assets             Remove assets { asset_ids: [...] }
GET    /v1/collections/{id}/assets             List assets (paginated, ordered)
PATCH  /v1/collections/{id}/reorder            Reorder { asset_ids: [...] }
```

All asset mutation endpoints are batch-first — `asset_ids` is always an array, even for single items. This avoids needing separate single/batch endpoints and supports the primary UI flow: select multiple assets, then act.

**Batch add shortcut (create + populate):**

```
POST /v1/collections    { name, asset_ids: [...] }
```

When `asset_ids` is provided on create, the collection is created and populated atomically. This supports the "select photos → Create new collection" flow without two round trips.

**Public (no auth):**

```
GET    /v1/public/collections/{id}             Collection metadata + first page
GET    /v1/public/collections/{id}/assets      Paginated asset list
```

### Selection Model (UI)

The browse grid needs a multi-select mode that is independent of collections but feeds into them. The selection model is:

- **Enter selection mode**: Long-press, checkbox click, or keyboard shortcut
- **Select individual assets**: Click/tap toggles selection
- **Select range**: Shift+click selects all assets between last selected and clicked
- **Select by date group**: Click a date header to select all assets in that group
- **Selection actions toolbar**: Appears when selection is non-empty. Actions include:
  - "Add to collection" → collection picker (existing collections + "Create new")
  - "Remove from collection" (when browsing a collection)
  - Future: rate, tag, trash, download
- **Collection picker**: Modal with searchable list of existing collections, "Create new" at top. Creating new prompts for name, then adds selected assets immediately.

The selection state lives in the browse page (React state, not URL). It's cleared on navigation. The toolbar floats at the bottom of the viewport.

### CLI Support (future)

```
lumiverb collection list
lumiverb collection create <name>
lumiverb collection add <name> --asset-id <id> [--asset-id <id> ...]
lumiverb collection remove <name> --asset-id <id>
lumiverb collection show <name> [--output json]
lumiverb collection export <name> --dest <path>    # copies source files
```

The `export` command is the scripting use case — "copy all source files from this collection to a flash drive." It resolves `rel_path` + library `root_path` to source file locations.

### Web UI

- **Collections page** (`/collections`): Grid of collection cards showing cover image, name, count
- **Collection detail page** (`/collections/{id}`): Same virtualized grid as BrowsePage, but backed by collection assets endpoint. Lightbox works the same. No directory tree sidebar.
- **Add to collection**: From lightbox or bulk selection, "Add to collection" action opens a picker showing existing collections + "Create new"
- **Collection settings**: Name, description, public toggle, sort order, cover image
- **Public collection view**: Standalone page, no nav chrome, shareable URL

### Migration

One Alembic migration in the tenant context:
- Create `collections` table
- Create `collection_assets` table with composite PK and FK cascade
- Index on `collection_assets(asset_id)` for fast cascade lookups

One Alembic migration in the control plane context:
- Create `public_collections` table

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Add asset already in collection | Idempotent — `ON CONFLICT DO NOTHING`, no error |
| Add trashed asset | Rejected (404) — only active assets can be added |
| Trash asset in collection | `collection_assets` row persists; asset hidden by query-time filter. **Restoring the asset restores collection membership and position.** |
| Hard-delete asset (empty trash) | `ON DELETE CASCADE` removes `collection_assets` row permanently |
| Trash library | All assets get `deleted_at`; disappear from collections via filter. No special logic. |
| Cover asset deleted | Lazy self-healing on read — falls back to first-by-position, nulls stale `cover_asset_id` |
| Public collection, all assets gone | Returns empty collection (200, not 404). Collection still exists. |
| Collection name uniqueness | Not enforced. Duplicate names allowed (matches Google Photos). |
| Reorder with partial list | Rejected (400). Must include all collection asset IDs. |
| Very large collection (10K+) | Reorder is expensive. Not a v1 concern — paginate or cap later if needed. |

## Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Tests**: New backend tests for every endpoint and repository method. Edge cases from the table above must be covered as they become relevant. **All tests must pass** — not just new or affected tests, the entire suite (`uv run pytest tests/`). No phase is done until the full suite is clean.
2. **Documentation**: `docs/cursor-api.md` and `docs/cursor-cli.md` updated to reflect new endpoints, models, and commands added in the phase.
3. **Progress**: The phase status table below is updated when a phase completes.
4. **Forward compatibility**: Implementation must read ahead to future phases and ensure the data model, API shapes, and component interfaces are set up correctly. If current work reveals changes needed in a future phase, update that phase's description.
5. **Backward compatibility**: If current implementation invalidates or changes assumptions in a future phase, those phases must be updated in this document before the current phase is marked complete.

## Build Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Backend: data model, migrations, repository, API endpoints (authenticated only) | Done |
| 2 | Collection management UI: list page, detail page, settings, create/delete | Done |
| 3 | Multi-select in browse grid + "Add to collection" flow with picker modal | Done |
| 3.5 | User-scoped collections: ownership, visibility (private/shared/public) | Done |
| 4 | Public collections: control plane wiring, public endpoints, public view page | Pending |
| 5 | Polish: drag-to-reorder, remove from collection, CLI commands, empty states | Pending |

### Phase 1 — Backend + Data Model

- Alembic migration: `collections` and `collection_assets` tables (tenant context)
- SQLModel models: `Collection`, `CollectionAsset`
- Repository: `CollectionRepository` with CRUD, batch add/remove, position management, cover resolution
- API router: `POST/GET/PATCH/DELETE /v1/collections`, `POST/DELETE/GET /v1/collections/{id}/assets`, `PATCH /v1/collections/{id}/reorder`
- Create endpoint supports optional `asset_ids` for atomic create+populate
- All asset mutations are batch-first (`asset_ids` array)
- Idempotent add (`ON CONFLICT DO NOTHING`), reject trashed assets
- Query-time filtering: collection asset queries JOIN through `deleted_at IS NULL`
- Cover image resolution with lazy self-healing
- Tests: CRUD, batch add/remove, idempotent add, trashed asset rejection, cover fallback, soft-delete visibility, hard-delete cascade, reorder validation
- Docs: update `cursor-api.md` with new endpoints

**Does NOT include**: `public_collections` table, public endpoints, UI, CLI. Phase 1 sets up the data model and API that Phases 2-5 build on.

**Read-ahead**: The `collections` table schema must support Phase 4 (public access) — `is_public` column is included now even though the public endpoint isn't built until Phase 4. The API response shapes must support Phase 2 (UI) — include `cover_asset_id` resolution and asset count in list responses from day one.

### Phase 2 — Collection Management UI

- Collections list page (`/collections`): grid of cards with cover thumbnail, name, count
- Collection detail page (`/collections/{id}`): virtualized asset grid (reuse BrowsePage grid components), lightbox
- Collection settings: name, description, sort order, cover image picker
- Create collection (empty), delete collection (with confirmation)
- Navigation: add "Collections" to sidebar, route setup
- API client functions for all collection endpoints
- Tests: component rendering, API integration
- Docs: update Web UI section of this ADR with final component structure

**Does NOT include**: adding assets to collections from the browse page (that's Phase 3), public access (Phase 4).

**Read-ahead**: The detail page grid must accept a generic asset data source so Phase 3 can reuse the same grid for "browsing a collection." The collection card component must support a future "public" badge for Phase 4.

### Phase 3 — Multi-Select + "Add to Collection"

- Selection model in BrowsePage: individual toggle, shift-range, date group select
- Floating action toolbar (appears when selection non-empty)
- "Add to collection" action → collection picker modal (search, create new)
- Single-asset "Add to collection" from lightbox metadata panel
- "Remove from collection" action when browsing a collection detail page
- Tests: selection state management, batch add/remove API calls
- Docs: update this ADR's Selection Model section with final implementation details

**Does NOT include**: public access, drag-to-reorder, CLI.

**Read-ahead**: The selection model is a general-purpose primitive. Phase 5 will add more actions (rate, tag, trash) to the same toolbar. Design the toolbar to accept pluggable action buttons.

### Phase 3.5 — User-Scoped Collections

Collections are now user-owned, not tenant-global. This matches the Google Photos model: my albums are mine, I choose to share them.

- Alembic migration: add `owner_user_id` (text, nullable) and `visibility` (text, default `private`) to `collections`; drop `is_public`
- `visibility` enum: `private` (only owner sees), `shared` (all tenant users can view), `public` (unauthenticated, Phase 4)
- `owner_user_id` set from JWT `sub` or `key:{key_id}` for API key auth
- API: `GET /v1/collections` returns owned + shared collections, with `ownership` field (`own` | `shared`)
- Mutations (create, update, delete, add/remove assets, reorder) restricted to owner (403 for non-owner)
- Read endpoints (get, list assets) allowed for owner + shared visibility
- Legacy collections (NULL `owner_user_id`) treated as shared/tenant-wide
- Backfill: existing `is_public=true` → `visibility=shared`
- `get_current_user_id` dependency added to `src/api/dependencies.py`
- UI types updated: `is_public` → `visibility` + `ownership`

**Does NOT include**: public collection viewing (Phase 4), UI for visibility toggle (Phase 4).

### Phase 4 — Public Collections

- Alembic migration: `public_collections` table (control plane context)
- SQLModel model: `PublicCollection`
- Toggle public/private: `PATCH /v1/collections/{id}` with `visibility=public` manages `public_collections` row
- Public API endpoints: `GET /v1/public/collections/{id}`, `GET /v1/public/collections/{id}/assets`
- Auth bypass: public collection endpoints resolve tenant via `public_collections` lookup, no bearer token required
- Proxy/thumbnail serving: accept `?collection_id=` for public access, verify asset membership
- Public collection view page: standalone, no sidebar/nav, shareable URL
- Privacy: strip library paths, limit EXIF exposure
- Tests: public access without auth, private collection returns 404, public toggle, asset serving via collection membership
- Docs: update `cursor-api.md` with public endpoints

**Does NOT include**: CLI, drag-to-reorder.

### Phase 5 — Polish

- Drag-to-reorder in collection detail page
- Remove from collection (in-context button on collection detail page)
- CLI commands: `collection list`, `create`, `add`, `remove`, `show`, `export`
- Empty states, loading skeletons, error handling
- Tests: reorder validation, CLI command integration
- Docs: update `cursor-cli.md` with collection commands

## What This Does NOT Include

- **Smart collections** (auto-add rules based on search queries) — future, built on top of this
- **Ratings/picks** — separate feature, but "Favorites" will be a collection
- **Collaborative collections** (multi-user editing) — future, requires per-collection roles
- **Collection sharing with specific users** (vs. fully public) — future
- **Nested collections** (collections within collections) — not planned
- **Collection-level metadata/tags** — not in v1

## Alternatives Considered

**Collections as a special type of library**: Rejected. Libraries have filesystem semantics (root_path, path filters, scan_status, ingest pipeline) that don't apply to collections. Sharing the model would require nullable fields everywhere and confusing conditional logic. Distinct models, distinct API, distinct UI.

**Join table without position column**: Rejected. Manual ordering is essential for curation. Without position, the only orders are insertion time or asset metadata — not user intent.

**Per-library collections**: Rejected. Cross-library collections are the whole point. A wedding photographer wants one "Selects" album from photos shot on two cameras stored in different library roots.

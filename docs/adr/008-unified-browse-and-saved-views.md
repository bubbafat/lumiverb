# ADR-008: Unified Browse and Saved Views

## Status

Accepted (all phases complete)

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Backend: cross-library browse endpoint + repository | **Complete** |
| 2 | Unified browse page UI with full filter support | **Complete** |
| 3 | Saved views: data model, CRUD API, sidebar rendering | **Complete** |
| 4 | Sidebar restructure: manual vs. automatic sections | **Complete** |

## Overview

Lumiverb can browse one library at a time. Ratings, collections, and search are all scoped to a single library. Users with multiple libraries (e.g., one per camera, one per year, one per project) have no way to see their full catalog in one view.

The Favorites page (`/favorites`) already proves the cross-library pattern works — it queries all assets across all libraries filtered by `favorite=true`. This ADR generalizes that into a full unified browse with all existing filters, then adds saved views (named filter presets) so users can bookmark useful cross-library queries.

After this ADR:
- `/browse` shows all assets across all libraries with the full FilterBar
- Favorites becomes a saved view shortcut for `?favorite=true` on unified browse
- Users can create saved views like "Best of 2025" (`?star_min=5&date_from=2025-01-01`) that appear in the sidebar
- Future special filters (has face, has GPS, etc.) automatically become available in saved views

## Motivation

- Photographers store assets across multiple libraries (by camera, year, project, location). Reviewing work across libraries requires switching between them manually.
- Rating and color-coding assets is most useful when you can filter across your entire catalog — "show me all my 5-star shots regardless of which library they're in."
- The Favorites page already does cross-library browse but with a hardcoded filter and no FilterBar. Generalizing it is straightforward.
- Saved views are the lightweight alternative to smart collections. No new data model for collection membership — just a name and a URL query string.

## Design

### Unified Browse

A new endpoint and page that queries across all libraries the user has access to. Same filters as `page_by_library` minus `path_prefix` (paths are library-relative and meaningless cross-library). Adds `library_id` filter for narrowing to specific libraries.

### Data Model

**Tenant database (new table):**

```
saved_views
  view_id          PK, text (sv_+ULID)
  name             text, not null
  query_params     text, not null (URL query string, e.g. "star_min=5&color=red")
  icon             text, nullable (emoji or icon name for sidebar display)
  owner_user_id    text, not null
  position         int, not null (sidebar ordering, 0-based)
  created_at       timestamptz
  updated_at       timestamptz
```

One row per saved view per user. `query_params` is the raw URL search params string — the UI navigates to `/browse?{query_params}` when clicked. No parsing, no schema coupling. If filters evolve, old saved views still work (unknown params are ignored).

**Why not reuse collections?** Collections are curated membership lists with position ordering, public sharing, and cover images. Saved views are just bookmarked filter URLs. Different purpose, different lifecycle, different UI. Overloading collections would complicate both.

### Asset Lifecycle

- Saved views have no asset membership — they're query shortcuts. No cascade behavior needed.
- Deleting a user deletes their saved views (`owner_user_id` cleanup, same pattern as ratings).
- Deleting a library doesn't affect saved views. A saved view that filtered to a deleted library simply returns fewer results.

### API Endpoints

**Unified browse:**

```
GET /v1/browse
```

Same query params as `GET /v1/assets/page` except:
- `library_id` is optional (omit for all libraries, provide to narrow)
- `path_prefix` only works when `library_id` is provided
- Response items include `library_id` and `library_name`
- All rating filters supported (`favorite`, `star_min`, `star_max`, `color`, `has_rating`)
- All EXIF filters supported
- Cursor-based pagination (same as page endpoint)

Response shape:

```json
{
  "items": [
    {
      "asset_id": "...",
      "library_id": "...",
      "library_name": "Travel 2025",
      "rel_path": "DSC_1234.jpg",
      ...all existing AssetPageItem fields...
    }
  ],
  "next_cursor": "..."
}
```

**Saved views CRUD:**

```
POST   /v1/views              Create { name, query_params, icon? }
GET    /v1/views              List (owned by current user, ordered by position)
PATCH  /v1/views/{id}         Update name, query_params, icon
DELETE /v1/views/{id}         Delete
PATCH  /v1/views/reorder      Reorder { view_ids: [...] }
```

All endpoints require auth. Views are user-scoped — you only see your own.

### Unified Browse Repository

New `UnifiedBrowseRepository` (not an extension of `AssetRepository`). Builds a query across the `active_assets` view with no `library_id` constraint by default. Supports all the same filters as `page_by_library()`:

- Sort by `taken_at`, `created_at`, `file_size`, `asset_id` (and all EXIF sorts)
- EXIF filters (camera, lens, ISO, aperture, focal length, exposure, GPS)
- Rating filters (favorite, stars, color — LEFT JOIN on `asset_ratings`)
- Media type filter
- Optional `library_id` filter (single or comma-separated list)
- Tag filter (LATERAL JOIN on `asset_metadata`)
- Cursor-based keyset pagination

The query pattern is identical to `page_by_library()` but starts from `active_assets a` without `a.library_id = :library_id`. Library name resolution is done post-query via a batch lookup (same pattern as `list_favorites`).

### Favorites as a Saved View

After this ADR, Favorites is conceptually just a saved view with `query_params = "favorite=true"`. However, it keeps its dedicated sidebar slot and heart icon — it's always present, not deletable, and doesn't appear in the saved views list. The `/favorites` route becomes a redirect to `/browse?favorite=true` (or the FavoritesPage is replaced with the unified browse page).

The existing `GET /v1/assets/favorites` endpoint is retained as a convenience alias but can be deprecated once `/v1/browse?favorite=true` works identically.

### Sidebar Structure

```
Libraries
  ├── Library A
  ├── Library B
  └── Manage libraries

Collections

──────────── (divider)

♥ Favorites            → /browse?favorite=true
★ Best of 2025         → /browse?star_min=5&date_from=2025-01-01&date_to=2025-12-31
● Red label            → /browse?color=red
  All photos           → /browse
  + New saved view
```

The divider separates manual curation (libraries, collections) from automatic/filtered views. Below the divider:
- Favorites is always first (hardcoded, not a saved view row)
- User's saved views follow, in user-defined order
- "All photos" link for unfiltered cross-library browse
- "New saved view" button to save the current filter state

### Creating a Saved View

Two flows:

1. **From the filter bar**: User applies filters on `/browse`, then clicks "Save view" in the FilterBar. Prompted for a name. Current URL params are saved as `query_params`.

2. **From the sidebar**: "New saved view" button opens a modal with name + filter builder. Less likely to be used — most users will filter first, then save.

### UI

**Unified browse page** (`/browse`):
- Same virtualized grid as BrowsePage, same FilterBar, same lightbox, same selection toolbar
- No directory tree sidebar (paths are library-relative)
- Library name shown in date group headers: "Travel 2025 — March 15, 2025" or as a subtle badge on each cell
- FilterBar gains a "Library" dropdown (multi-select) for narrowing to specific libraries
- "Save view" button in FilterBar when filters are active

**Saved view management**:
- Sidebar items are draggable for reorder (or simple up/down buttons)
- Right-click or hover menu: rename, edit filters, delete
- Clicking a saved view navigates to `/browse?{query_params}`

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No libraries | Empty state: "No libraries yet" |
| All libraries trashed | Empty results (active_assets filters them out) |
| Saved view with stale filter params | Unknown params ignored, still works |
| Saved view pointing to deleted library | Returns fewer results, no error |
| Two users create same-named view | Fine — views are user-scoped |
| Very large tenant (100K+ assets) | Same keyset pagination as library browse. Partial indexes on ratings help. |
| Unified browse + GPS filter | Works — GPS data is on the asset regardless of library |
| Unified browse + path_prefix without library_id | Rejected (400) — paths are library-relative |

## Code References

| Area | File | Notes |
|------|------|-------|
| Library browse repo | `src/repository/tenant.py` | `page_by_library()` — pattern to follow (not extend) |
| Favorites endpoint | `src/api/routers/ratings.py` | `list_favorites()` — existing cross-library pattern |
| Favorites page | `src/ui/web/src/pages/FavoritesPage.tsx` | Cross-library UI pattern to generalize |
| Browse page | `src/ui/web/src/pages/BrowsePage.tsx` | Full FilterBar + grid pattern to reuse |
| Asset model | `src/models/tenant.py` | `Asset` — has `library_id` for grouping |
| Sidebar | `src/ui/web/src/components/Sidebar.tsx` | Add divider + saved views section |
| FilterBar | `src/ui/web/src/components/FilterBar.tsx` | Add library filter + "Save view" button |
| API types | `src/ui/web/src/api/types.ts` | Extend or create cross-library response type |
| Router registration | `src/api/main.py` | New browse + views routers |

## Doc References

- `docs/cursor-api.md` — New browse and views endpoints
- `docs/cursor-cli.md` — Future: CLI commands for saved views

## Build Phases

### Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Tests**: New backend tests for every endpoint and repository method. **All tests must pass** — the entire suite (`uv run pytest tests/`).
2. **Types**: Frontend TypeScript must compile cleanly (`npx tsc --noEmit`).
3. **Build**: Vite must build without errors (`npx vite build`).
4. **Documentation**: Relevant docs updated to reflect changes in the phase.
5. **Progress**: The phase status table above is updated when a phase completes.
6. **Forward compatibility**: Read ahead to future phases and ensure interfaces are set up correctly.
7. **Backward compatibility**: Update future phase descriptions if current work changes assumptions.

### Phase 1 — Backend: Cross-Library Browse

**Deliverables:**
- `UnifiedBrowseRepository` in `src/repository/tenant.py` — cross-library `page()` method with all filters, keyset pagination, library name resolution
- `GET /v1/browse` endpoint in new `src/api/routers/browse.py` — same filter params as page endpoint, optional `library_id`, response includes `library_id` + `library_name`
- Validation: `path_prefix` requires `library_id` (400 otherwise)
- Tests: cross-library results, library filter, rating filters, EXIF filters, pagination, path_prefix validation
- Docs: update `cursor-api.md`

**Does NOT include:** Saved views, UI, sidebar changes.

**Read-ahead:** Response shape must support Phase 2's UI — include `library_id` and `library_name` on every item from day one. The endpoint path `/v1/browse` must not conflict with existing routes.

### Phase 2 — Unified Browse Page UI

**Deliverables:**
- `/browse` route and `UnifiedBrowsePage` component
- Virtualized grid with justified layout (reuse BrowsePage grid components)
- Full FilterBar with all existing filters + new "Library" multi-select dropdown
- Lightbox with rating controls
- Selection toolbar with rating + collection actions
- Date group headers include library name
- Ratings overlay on cells
- Favorites page (`/favorites`) redirects to `/browse?favorite=true` or is replaced
- Tests: TypeScript clean, Vite builds

**Does NOT include:** Saved views, sidebar changes.

**Read-ahead:** The FilterBar "Save view" button (Phase 3) needs access to the current URL params. Design the page so the full filter state is always in the URL.

**Notes from ratings ADR:**
- Ratings lookup query pattern from BrowsePage can be reused directly.
- The `parseSearchQuery` utility works unchanged — it extracts `is:favorite` etc. from search text.
- `onChangeFilters` batch prop (from star filter fix) should be used for multi-param filter changes.

### Phase 3 — Saved Views

**Deliverables:**
- Alembic migration: `saved_views` table
- SQLModel model: `SavedView`
- `SavedViewRepository`: CRUD, reorder, delete_for_user
- API router: `POST/GET/PATCH/DELETE /v1/views`, `PATCH /v1/views/reorder`
- User deletion cleanup (same pattern as ratings)
- FilterBar: "Save view" button when filters are active on `/browse`
- Save modal: name input, creates view from current URL params
- Tests: CRUD, reorder, user cleanup
- Docs: update `cursor-api.md`

**Does NOT include:** Sidebar rendering (Phase 4).

### Phase 4 — Sidebar Restructure

**Deliverables:**
- Visual divider between Collections and automatic views section
- Favorites link with heart icon (always first below divider)
- "All photos" link → `/browse`
- Saved views rendered below Favorites, in position order
- "New saved view" button at bottom of section
- Right-click or hover menu on saved views: rename, delete
- Drag-to-reorder or simple position buttons
- Tests: TypeScript clean, Vite builds

## Alternatives Considered

**Extend `page_by_library` to accept optional library_id:** Rejected. The method name, parameter semantics, and SQL structure are all library-scoped. Making library_id optional would require conditionalizing the entire query, the cursor format, and the response shape. A separate repository method is cleaner.

**Smart collections as a new collection type:** Rejected. Collections have membership semantics (assets are explicitly added/removed, positioned, shared). Saved views have no membership — they're query shortcuts. Mixing the two would require "virtual membership" logic, special-case sharing rules, and confusing UI states.

**Store saved view filters as structured JSON instead of query string:** Rejected. The URL query string is already the canonical filter format — the UI reads it, the API accepts it, and the FilterBar renders from it. Saving the raw query string means zero serialization/deserialization code and automatic forward compatibility when new filters are added.

## What This Does NOT Include

- **Cross-library search** (Quickwit) — search is per-library due to index partitioning. Unified browse is a DB query, not a search query. Cross-library search is a separate problem.
- **Shared saved views** — views are user-scoped. Sharing would require visibility semantics (like collections).
- **Saved view folders/categories** — flat list for v1.
- **Auto-updating saved view counts** — no badge showing "42 assets match." Could add later.
- **CLI commands** — future phase if needed.
- **Smart collection triggers** (notifications when new assets match) — future.

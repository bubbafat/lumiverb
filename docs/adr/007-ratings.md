# ADR-007: Ratings — Favorites, Stars, and Color Labels

## Status

Proposed

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Backend: data model, migration, repository, API endpoints | Not started |
| 2 | Lightbox + selection toolbar: rating controls for single and bulk assets | Not started |
| 3 | Browse filters + search syntax: `is:favorite`, `star:3`, `color:red` | Not started |
| 4 | Favorites sidebar shortcut + cross-library favorites view | Not started |

## Overview

Lumiverb has no way for users to mark, grade, or visually categorize their assets. Photographers need three levels of curation feedback during a review session:

1. **Favorites** (heart) — binary yes/no, the quickest gesture. "I like this one."
2. **Star ratings** (1-5) — graduated quality/importance ranking. "This is a 4."
3. **Color labels** (red, orange, yellow, green, blue, purple) — arbitrary categorical markers. "Red = needs retouching, green = ready to deliver."

These map directly to the workflows in Lightroom, Capture One, and Photo Mechanic that photographers already use.

## Motivation

- During a review session, users need to rapidly flag keepers without leaving the grid or lightbox.
- After flagging, users need to filter the browse view to "show me only my 5-stars" or "show me everything I marked red."
- A "Favorites" shortcut in the sidebar provides instant access to flagged assets across all libraries without creating a manual collection.
- Ratings are the foundation for smart collections (future) — "auto-populate this collection with all 4+ star images from 2025."

## Design

### Ownership Model

Ratings are **exclusively user-scoped**. Every rating belongs to exactly one user.

- My favorites are mine. Your favorites are yours. We never see each other's ratings.
- If a user account is deleted, all of that user's ratings are deleted with it.
- Ratings are not shareable, not exportable to collections, and not visible to other tenant users.
- The "Favorites" view is a search filter, not a collection. It cannot be shared or made public.
- API key auth (`key:{key_id}`) gets its own rating namespace — API key ratings are separate from any human user's ratings.

### Data Model

**Tenant database (new table):**

```
asset_ratings
  user_id          text, not null
  asset_id         FK → assets.asset_id ON DELETE CASCADE, not null
  favorite         boolean, not null, default false
  stars            int, not null, default 0 (0 = unrated, 1-5 = rated)
  color            text, nullable (null = no color; red | orange | yellow | green | blue | purple)
  updated_at       timestamptz, not null

  PRIMARY KEY (user_id, asset_id)
  INDEX on (user_id, favorite) WHERE favorite = true   -- fast favorites lookup
  INDEX on (user_id, stars) WHERE stars > 0             -- fast star filter
  INDEX on (user_id, color) WHERE color IS NOT NULL     -- fast color filter
```

**One row per user per asset.** All three rating dimensions live on the same row. Setting a favorite on an unrated asset upserts the row. Clearing all ratings (unfavorite + 0 stars + no color) deletes the row entirely — no zombie rows.

**Why one table, not three:**
- The three dimensions are always queried together (e.g., "my favorites that are also 5-star").
- One upsert is cheaper than three.
- One JOIN in browse queries instead of three.
- The row is small — no performance concern.

**Why not a column on Asset:**
- Ratings are user-scoped. Adding `favorite`, `stars`, `color` to the `assets` table would make them tenant-global, which contradicts the ownership model.
- Multiple users rating the same asset requires a separate table.

### User Deletion Cleanup

Users live in the control plane DB. Ratings live in the tenant DB. No cross-DB foreign key is possible. When a user is deleted (`DELETE /v1/users/{user_id}`), the user management endpoint must also delete all `asset_ratings` rows for that `user_id` in the tenant DB. This is a synchronous cleanup in the same API request — not a background job.

### Asset Lifecycle

- **Soft-deleting (trashing) an asset**: The `asset_ratings` row persists. Trashed assets are excluded from all rating queries via the `active_assets` view JOIN. Restoring the asset restores its ratings automatically.
- **Hard-deleting an asset (empty trash)**: `ON DELETE CASCADE` removes the `asset_ratings` row permanently.
- **Deleting a library**: All library assets get `deleted_at` set. Ratings persist but are invisible. Hard-deleting the library cascades to assets, which cascades to ratings.

### API Endpoints

**Rate a single asset:**

```
PUT /v1/assets/{asset_id}/rating
Body: { "favorite": true, "stars": 4, "color": "red" }
Response: { "asset_id": "...", "favorite": true, "stars": 4, "color": "red" }
```

All fields optional — only provided fields are updated. Omitted fields are unchanged. To clear: `"favorite": false`, `"stars": 0`, `"color": null`.

If the resulting state is all-default (favorite=false, stars=0, color=null), the row is deleted.

**Rate multiple assets (batch):**

```
PUT /v1/assets/ratings
Body: { "asset_ids": ["..."], "favorite": true, "stars": 5 }
Response: { "updated": 12 }
```

Same merge semantics — only provided fields are updated across all listed assets. Batch-first, consistent with collection asset mutations.

**Get ratings for assets (bulk read):**

```
POST /v1/assets/ratings/lookup
Body: { "asset_ids": ["...", "..."] }
Response: { "ratings": { "ast_abc": { "favorite": true, "stars": 4, "color": "red" }, ... } }
```

Returns a map. Assets with no rating row are omitted (not returned as defaults). The UI treats missing = all defaults. This endpoint is called when the browse page loads a batch of assets, so ratings appear on the grid without N+1 queries.

### Browse Integration

Extend `/v1/assets/page` with new query params:

```
?favorite=true              — only favorites
?star_min=3                 — stars >= 3
?star_max=4                 — stars <= 4
?color=red                  — exact color match
?color=red,green            — any of these colors
?has_rating=true            — has any non-default rating
```

Implementation: LEFT JOIN `asset_ratings` on `(user_id, asset_id)` in `page_by_library()`. Filter in WHERE clause. User ID comes from `get_current_user_id` dependency.

### Search Syntax

Parse structured filters out of the search query string before sending the text portion to Quickwit:

| Syntax | Meaning | Maps to |
|--------|---------|---------|
| `is:favorite` | Favorites only | `?favorite=true` |
| `is:rated` | Has any rating | `?has_rating=true` |
| `is:unrated` | No rating row | `?has_rating=false` |
| `star:5` | Exactly 5 stars | `?star_min=5&star_max=5` |
| `star:>3` | More than 3 stars | `?star_min=4` |
| `star:>=3` | 3 or more stars | `?star_min=3` |
| `star:<3` | Less than 3 stars | `?star_max=2` |
| `star:<=3` | 3 or fewer stars | `?star_max=3` |
| `star:0` or `star:none` | Unrated (no stars) | `?star_max=0` |
| `has:star` | Has any star rating | `?star_min=1` |
| `has:color` | Has any color label | `?has_color=true` |
| `color:red` | Red label | `?color=red` |
| `color:none` | No color label | `?color=none` |

**Parsing**: extract all `key:value` tokens from the query string. Remaining text (if any) is the Quickwit full-text query. If only structured filters remain (no free text), skip Quickwit entirely and use the browse endpoint with filters.

Example: `is:favorite sunset beach` → Quickwit query "sunset beach" + filter `favorite=true`.

Example: `is:favorite star:>=4` → No Quickwit query, browse with `favorite=true&star_min=4`.

### Favorites Sidebar Shortcut

A "Favorites" link in the sidebar, below Collections. Clicking it navigates to a favorites browse view.

**Cross-library favorites**: The current browse page is scoped to a single library. Favorites should work across all libraries in the tenant. This requires a new route and a modified browse query:

- Route: `/favorites`
- Endpoint: `/v1/assets/page` extended to work without `library_id` when `favorite=true` (or a new `/v1/favorites` endpoint that wraps it)
- UI: same virtualized grid as BrowsePage, but no directory tree sidebar, no library-specific filters. FilterBar shows only rating filters + text search.

**Alternative (simpler v1)**: If the user is browsing a library, "Favorites" filters that library's assets. If no library is selected, show a prompt to pick one. Defer cross-library favorites to a follow-up. The sidebar link navigates to the current library's browse with `?favorite=true`, or to `/favorites` when cross-library is ready.

### UI Controls

**Lightbox metadata panel** — after the Details section, before action buttons:

```
─────────────────
♥ [heart toggle]     ★★★★☆ [clickable stars]
[color dot picker: ● ● ● ● ● ● ×]
─────────────────
```

- Heart: toggle favorite on click. Filled red = favorite, outline = not.
- Stars: click to set (click same star to clear). Display as 5 stars, filled up to rating.
- Color dots: 6 colored circles + × to clear. Active color has a ring/border.

**AssetCell overlay** — when an asset has a rating, show a small indicator:

- Favorite: small heart icon in top-right corner (opposite the selection checkbox in top-left)
- Star: small star count badge (e.g., "★4") — only if stars > 0
- Color: thin bottom border in the label color

These are always visible (not just on hover) so the user can scan the grid and see their ratings.

**SelectionToolbar** — when assets are selected, add rating action buttons:

- Heart toggle (favorite/unfavorite all selected)
- Star dropdown or inline 5-star picker
- Color picker dropdown

These call the batch rating endpoint.

**FilterBar** — extend with rating filter controls:

- Heart toggle button (filter to favorites)
- Star range (min/max dropdown or clickable stars)
- Color filter (multi-select color dots)

Active rating filters show as chiclets alongside existing filter chiclets.

### Keyboard Shortcuts

| Key | Action | Context |
|-----|--------|---------|
| `F` | Toggle favorite | Lightbox (single asset) |
| `1`-`5` | Set star rating | Lightbox |
| `0` | Clear star rating | Lightbox |
| `6`-`9` | Set color (red=6, orange=7, yellow=8, green=9) | Lightbox |
| `` ` `` | Clear color | Lightbox |

These match Lightroom conventions. Only active when lightbox is open (not in grid, to avoid conflicts with text input).

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Rate a trashed asset | Rejected (404) — asset must be active |
| Rate an asset in another tenant | Impossible — asset lookup is tenant-scoped |
| Two users rate the same asset | Each gets their own row. No conflict. |
| User deleted, ratings remain? | No — explicit cleanup on user deletion |
| API key rates, then key revoked | Ratings persist (keyed to `key:{key_id}`). No auto-cleanup — revoked keys can be re-created. |
| Favorite + 0 stars + no color | Row is deleted (all-default state) |
| Batch rate 1000 assets | Single SQL upsert. No per-asset round trips. |
| Search `is:favorite` with no favorites | Empty results (not an error) |
| Filter `star:>5` or `star:<0` | 400 — invalid range |
| Filter `color:pink` | 400 — invalid color value |
| Cross-library favorites with trashed library | Library assets have `deleted_at`; excluded by active_assets JOIN |

## Code References

| Area | File | Notes |
|------|------|-------|
| Asset model | `src/models/tenant.py` | New `AssetRating` model goes here, before `SystemMetadata` |
| Asset repository | `src/repository/tenant.py` | New `RatingRepository` class; extend `page_by_library()` with rating JOINs |
| Browse endpoint | `src/api/routers/assets.py` | Add rating query params to page endpoint |
| Search endpoint | `src/api/routers/search.py` | Parse `is:favorite` etc. from query string; post-filter by ratings |
| User deletion | `src/api/routers/users.py` | Add rating cleanup on user delete |
| User ID dependency | `src/api/dependencies.py` | `get_current_user_id()` already exists |
| Selection hook | `src/ui/web/src/lib/useSelection.ts` | No changes needed — already generic |
| Selection toolbar | `src/ui/web/src/components/SelectionToolbar.tsx` | Add rating action buttons as children |
| Lightbox | `src/ui/web/src/components/Lightbox.tsx` | Add rating controls to metadata panel |
| AssetCell | `src/ui/web/src/components/AssetCell.tsx` | Add rating indicator overlays |
| FilterBar | `src/ui/web/src/pages/BrowsePage.tsx` | Extend FilterBar with rating filters |
| Sidebar | `src/ui/web/src/components/Sidebar.tsx` | Add Favorites link |
| API client | `src/ui/web/src/api/client.ts` | New rating API functions |
| Types | `src/ui/web/src/api/types.ts` | New `AssetRating` type |
| Migration | `migrations/tenant/versions/` | New migration for `asset_ratings` table |

## Doc References

- `docs/cursor-api.md` — Update with rating endpoints, browse filter params, search syntax
- `docs/cursor-cli.md` — Update if CLI commands are added (Phase 5 future)

## Build Phases

### Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Tests**: New backend tests for every endpoint and repository method. Edge cases from the table above must be covered as they become relevant. **All tests must pass** — not just new or affected tests, the entire suite (`uv run pytest tests/`). No phase is done until the full suite is clean.
2. **Types**: Frontend TypeScript must compile cleanly (`npx tsc --noEmit`).
3. **Build**: Vite must build without errors (`npx vite build`).
4. **Documentation**: Relevant docs updated to reflect changes in the phase.
5. **Progress**: The phase status table above is updated when a phase completes.
6. **Forward compatibility**: Implementation must read ahead to future phases and ensure data model, API shapes, and component interfaces are set up correctly. If current work reveals changes needed in a future phase, update that phase's description.
7. **Backward compatibility**: If current implementation invalidates or changes assumptions in a previous or future phase, those phases must be updated in this document before the current phase is marked complete.

### Phase 1 — Backend: Data Model + API

**Deliverables:**
- Alembic migration: `asset_ratings` table with composite PK, partial indexes, ON DELETE CASCADE
- SQLModel model: `AssetRating`
- Repository: `RatingRepository` with `upsert()`, `delete()`, `get_for_asset()`, `get_for_assets()` (bulk), `batch_upsert()`, `delete_for_user()` (cleanup)
- API router: `PUT /v1/assets/{asset_id}/rating`, `PUT /v1/assets/ratings` (batch), `POST /v1/assets/ratings/lookup` (bulk read)
- Validation: stars 0-5, color in allowed set or null, asset must be active
- All-default row cleanup (delete row when favorite=false, stars=0, color=null)
- User deletion cleanup: extend `DELETE /v1/users/{user_id}` to call `delete_for_user()`
- Tests: upsert, bulk read, batch update, all-default deletion, trashed asset rejection, user cleanup, concurrent ratings by different users
- Docs: update `cursor-api.md` with new endpoints

**Does NOT include:** Browse filter integration, search syntax, UI, sidebar shortcut.

**Read-ahead:** The repository must support the filter queries Phase 3 will need — design `get_for_assets()` to return a dict keyed by asset_id so the browse page can merge ratings into the grid without N+1 queries. The upsert method must handle partial updates (only provided fields change) since Phase 2's UI sends individual field changes.

### Phase 2 — UI: Rating Controls

**Deliverables:**
- Lightbox metadata panel: heart toggle, 5-star picker, color dot picker
- AssetCell overlays: heart icon (top-right), star badge, color bottom border
- SelectionToolbar: bulk favorite toggle, star picker, color picker
- Keyboard shortcuts in lightbox: F (favorite), 1-5 (stars), 0 (clear stars), 6-9 (colors), `` ` `` (clear color)
- Ratings bulk-read on browse page load: call `/v1/assets/ratings/lookup` with visible asset IDs, cache in React Query
- API client functions for all rating endpoints
- TypeScript types for `AssetRating`
- Tests: component rendering, keyboard shortcuts, batch API calls

**Does NOT include:** Browse filtering by rating, search syntax, sidebar shortcut.

**Read-ahead:** The rating state must be available to the FilterBar (Phase 3) — store ratings in a React Query cache keyed by `["ratings", userId]` so the FilterBar can read filter state without prop drilling. The AssetCell overlay design must not conflict with the selection checkbox (top-left) — ratings go top-right and bottom edge.

### Phase 3 — Browse Filters + Search Syntax

**Deliverables:**
- Extend `/v1/assets/page` with rating query params: `favorite`, `star_min`, `star_max`, `color`, `has_rating`
- Extend `page_by_library()` with LEFT JOIN on `asset_ratings` and WHERE clauses
- Search query parser: extract `is:favorite`, `star:N`, `color:X` etc. from query string
- Search post-filtering: apply rating filters to Quickwit results
- FilterBar UI: favorite toggle button, star filter, color multi-select
- Active filter chiclets for rating filters
- URL search params for rating filters (persist across navigation)
- Tests: browse with rating filters, search syntax parsing, combined text+rating queries, invalid filter values
- Docs: update `cursor-api.md` with filter params, document search syntax

**Does NOT include:** Cross-library favorites, sidebar shortcut.

**Read-ahead:** The query parser must be extensible — future ADRs may add more structured filters (e.g., `in:collection`, `type:video`). Design it as a generic `key:value` extractor, not hardcoded to rating filters.

### Phase 4 — Favorites Sidebar + Cross-Library View

**Deliverables:**
- "Favorites" link in sidebar (heart icon), below Collections
- `/favorites` route: cross-library favorites browse page
- Extend `/v1/assets/page` to work without `library_id` when `favorite=true` — query across all libraries the user can access
- Favorites page: virtualized grid (reuse BrowsePage grid), no directory tree, library name shown per asset group
- Asset grouping on favorites page: by library, then by date (or flat date groups spanning libraries)
- Tests: cross-library favorites query, empty state, mixed-library results
- Docs: update sidebar documentation

**Does NOT include:** CLI commands, smart collections, rating export.

## Alternatives Considered

**Ratings as collection membership (Favorites = a special collection):** Rejected. Collections are shareable, have position ordering, and support public access. Ratings are private, unordered, and never shared. Overloading the collection model would require special-casing "system collections" everywhere — visibility checks, sharing UI, reorder logic. A simple per-user table is cleaner and faster.

**Stars on the Asset table:** Rejected. Only works for single-user tenants. Multi-user tenants need per-user ratings, which requires a join table regardless.

**Separate tables for favorites, stars, and colors:** Rejected. All three are always set in the same UI gesture context, queried together, and scoped identically. One table, one JOIN, one upsert.

**Storing ratings in Quickwit:** Rejected. Ratings change frequently and are user-scoped. Quickwit is optimized for append-heavy search indexes, not per-user mutable state. Ratings belong in PostgreSQL.

## What This Does NOT Include

- **Smart collections** (auto-populate from rating filters) — future, builds on this + collections
- **Rating export** (XMP sidecar, Lightroom catalog sync) — future
- **Shared/team ratings** (see others' ratings, consensus view) — future, requires new permission model
- **Rating history/audit trail** — not in v1
- **CLI commands for ratings** — future phase if needed
- **Bulk import of ratings** (from Lightroom, etc.) — future

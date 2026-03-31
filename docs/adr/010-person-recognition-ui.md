# ADR-010: Person Recognition UI

## Status

Proposed

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Face search filter + lightbox bounding box overlay | Done |
| 2 | Clustering API + people CRUD + people page | Done |
| 3 | Cluster management UI + merge/rename/fix | Done |
| 4 | Search by person name + similarity integration | Done |

## Overview

ADR-009 landed face detection: every ingested image has bounding boxes and 512-dim ArcFace embeddings stored in the `faces` table, with an HNSW cosine index ready for similarity queries. But the UI has no way to surface this data — no face filter, no bounding box overlay, no person management.

This ADR delivers the full person recognition experience: filtering images by face presence, viewing face bounding boxes in the lightbox, clustering unnamed faces into people, naming and managing those people, and searching/browsing by person name. The end state is a Google Photos-style people experience where a user names faces once and they're recognized across all libraries in the tenant.

## Motivation

- The `has_faces` backend filter exists but is invisible in the UI — users cannot filter by "photos with people."
- Detected face bounding boxes are stored but never shown — users have no visual confirmation that detection worked.
- 512-dim face embeddings sit unused in an HNSW index — the infrastructure for clustering is ready but no clustering or person management exists.
- Person identification is the #1 feature that transforms a file browser into an intelligent photo library.
- People are a **tenant-level concept** — tagging "Susan" in Library A should not require re-tagging in Library B.

## Design

### Data Model

No new tables. All schema was created in ADR-009:

- **`faces`** — `face_id`, `asset_id`, `bounding_box_json` ({x, y, w, h} as 0-1 fractions), `embedding_vector` (vector(512)), `detection_confidence`, `detection_model`, `detection_model_version`
- **`people`** — `person_id`, `display_name`, `centroid_vector` (vector(512)), `confirmation_count`, `representative_face_id`
- **`face_person_matches`** — `match_id`, `face_id`, `person_id`, `confidence`, `confirmed`, `confirmed_at`
- **`assets.face_count`** — NULL (unprocessed), 0 (no faces), N (N faces)
- **HNSW index** on `faces.embedding_vector` (cosine distance, m=16, ef_construction=64)

**New columns (Phase 2 migration):**
- `faces.person_id` — nullable FK to `people.person_id`. Denormalized from `face_person_matches` for fast JOIN-free queries. The `face_person_matches` table retains full audit history (confidence, confirmed, timestamps).

**Facets extension (Phase 1):**
- Add `has_face_count` to the facets SQL aggregation: `COUNT(*) FILTER (WHERE face_count > 0)`

### API Endpoints

#### Pagination

All list endpoints use cursor-based pagination per project convention (`after` / `next_cursor`). Exceptions:

- `GET /v1/faces/clusters` — **not paginated**. Returns at most `limit` clusters (default 20, max 50) with up to `faces_per_cluster` sample faces each (default 6, max 20). Total response is bounded to ~1000 face references. This is a computed summary, not a raw dump.

#### Phase 1 (existing, wire to UI)

- `GET /v1/assets/facets` — add `has_face_count: int` to response
- `GET /v1/assets/{asset_id}/faces` — already returns `FaceListResponse` with bounding boxes
- `GET /v1/assets/page?has_faces=true` — already implemented
- `GET /v1/browse?has_faces=true` — already implemented
- `GET /v1/search?q=...&has_faces=true` — already implemented

#### Phase 2 (new)

```
GET    /v1/people                          — cursor-paginated, sorted by face count desc
                                             params: after, limit (default 50)
POST   /v1/people                          — create person { display_name, face_ids?: string[] }
GET    /v1/people/{person_id}              — get person with face count and representative thumbnail
PATCH  /v1/people/{person_id}              — update display_name
DELETE /v1/people/{person_id}              — delete person, remove all matches

GET    /v1/people/{person_id}/faces        — cursor-paginated faces with asset thumbnails
                                             params: after, limit (default 50)
GET    /v1/faces/clusters                  — bounded cluster summary (see Pagination above)
                                             params: limit, faces_per_cluster
```

#### Phase 3 (new)

```
POST   /v1/faces/{face_id}/assign          — { person_id } or { new_person_name }
DELETE /v1/faces/{face_id}/assign          — remove face from person
POST   /v1/people/{person_id}/merge        — { source_person_id } — merge source into target
```

**Validation rules:**
- `POST /v1/faces/{face_id}/assign` — if face is already assigned to a person, returns 409 Conflict. Client must explicitly `DELETE` the existing assignment first. This prevents silent reassignment.
- `POST /v1/people/{person_id}/merge` — atomic transaction. Steps: (1) reassign all `face_person_matches` from source to target, (2) update `faces.person_id` for affected faces, (3) recompute target centroid from all matched embeddings, (4) if target's `representative_face_id` pointed to source person's rep, pick highest-confidence face from merged set, (5) delete source person. Concurrent merge requests for the same source person use `SELECT ... FOR UPDATE` on the source to serialize.

#### Phase 4 (extend existing)

- `GET /v1/search?person_id=...` — filter by stable person_id (primary API)
- `GET /v1/browse?person_id=...` — filter by person_id
- `GET /v1/assets/page?person_id=...` — filter by person_id
- Search parser: `person:"Susan"` — resolved client-side to `person_id` via typeahead against `GET /v1/people?q=Susan`. If multiple people match (e.g., two "Sam"s), the typeahead shows disambiguated options (name + face count). The API always uses `person_id`, never display name, to avoid ambiguity.

### UI

#### Phase 1 — Search & Lightbox

**FilterBar (`src/ui/web/src/components/FilterBar.tsx`):**
- "Has faces" checkbox with count from `facets.has_face_count`, mirrors "Has location" pattern exactly
- Chiclet when active, included in clear-all

**Search parser (`src/ui/web/src/lib/parseSearchQuery.ts`):**
- `has:faces` → `{ has_faces: "true" }`

**BrowsePage + UnifiedBrowsePage:**
- Extract `has_faces` from URL search params, pass through to `pageAssets()` / `browseAll()` options

**Lightbox (`src/ui/web/src/components/Lightbox.tsx`):**
- Toggle button: "Show faces" / "Hide faces" in metadata sidebar
- Only visible when `asset.face_count > 0` (stills only, not video)
- Persisted via `useLocalStorage("lv_show_faces", false)` — survives navigation and sessions
- Faces fetched lazily via react-query (`GET /v1/assets/{id}/faces`) only when toggle is on
- Bounding boxes rendered as absolute-positioned divs over the image using fraction-based CSS %:
  ```
  left: x*100%, top: y*100%, width: w*100%, height: h*100%
  ```
- Image wrapped in `<div className="relative inline-block">` so parent shrinks to image size
- Keyboard shortcut: `d` (detect/display) — `f` is taken by favorite (Lightroom convention)
- Border style: `border-2 border-indigo-400 rounded` — subtle, non-distracting
- Phase 1 treats all boxes identically (indigo border, no labels, no interactivity)

#### Phase 2 Lightbox Enhancements

Once `FaceItem.person` is populated:
- **Color-coded boxes**: identified faces use `border-emerald-400` (green), unidentified faces use `border-gray-500` (gray). Replaces the uniform indigo border from Phase 1.
- **Name labels**: identified faces show a small label (`person.display_name`) anchored below the bounding box. Style: `bg-black/70 text-xs text-white px-1 rounded` — readable but unobtrusive.
- **Clickable identified faces**: remove `pointer-events-none` from boxes with a person match. Click navigates to `/people/{personId}`. Unidentified faces remain non-interactive until Phase 3 adds assignment UI.

#### Phase 2 — People Page (named people only)

**Route: `/people`**
- Grid of named person cards: representative face thumbnail, display name, face count
- Sorted by face count descending (most photographed person first)
- Summary banner: "N unnamed face clusters detected" with link/count — but **no cluster display or assignment UI** in Phase 2. Cluster management ships in Phase 3.
- Nav link in sidebar (after Favorites)

**Route: `/people/{personId}`**
- Header: person name (editable inline), representative face, face count
- Grid of all photos containing this person (reuse existing grid/lightbox pattern)

#### Phase 3 — Cluster Management

**Cluster panel (added to `/people` page):**
- Below named people grid, expandable "Unnamed clusters" section
- Shows unassigned face clusters sorted by size (from `GET /v1/faces/clusters`)
- Each cluster: grid of face crop thumbnails (up to `faces_per_cluster` samples)
- Actions per cluster: "Name this person" (creates new), "This is [existing person]" (merge into existing)
- Single-face corrections: reassign face to different person, remove match

**Lightbox — unidentified face interactivity:**
- Gray (unidentified) face boxes become clickable in Phase 3
- Click opens an inline popover: "Name this person" (create new) or "This is [dropdown of existing people]"
- Uses `POST /v1/faces/{face_id}/assign` endpoint

#### Phase 4 — Search by Person

- `person:"Susan"` search syntax in parseSearchQuery — resolved to `person_id` via client-side typeahead against `GET /v1/people?q=Susan`. API queries always use stable `person_id`, never display name. If multiple people share a name, typeahead shows "Susan (42 photos)" vs "Susan (3 photos)" for disambiguation.
- Person name chips in lightbox face overlay (when person is assigned)
- Click person chip → navigate to `/people/{personId}`

### Clustering Algorithm (Phase 2)

Server-side, using pgvector:

1. **Find clusters**: For each unassigned face, find its K=10 nearest neighbors within cosine distance < 0.55 (ArcFace empirical threshold). HNSW query uses `SET LOCAL hnsw.ef_search = 40` (default is 40; sufficient for K=10). Build connected components via union-find. Filter out clusters smaller than `min_cluster_size` (default 2).
2. **Sort by size**: Return clusters largest-first. The largest cluster is the most photographed face.
3. **Representative face**: Pick the face with highest detection confidence in each cluster.
4. **Centroid computation**: When a cluster is assigned to a person, compute `centroid_vector = mean(embeddings)` and store on the `people` row.
5. **Incremental improvement**: When user confirms a match (Phase 3), recompute centroid: `new = (old * count + new_embedding) / (count + 1)`, increment `confirmation_count`. This shifts the centroid toward confirmed faces, improving future nearest-neighbor queries.

**Scalability constraints:**
- **Cap at 5,000 unassigned faces.** If more exist, cluster the 5,000 with highest detection confidence and return a `truncated: true` flag. Users should name the largest clusters first; subsequent calls will pick up newly-unassigned faces.
- **Cost**: One HNSW query per unassigned face × K=10 results. For 5,000 faces this is ~5,000 queries. HNSW lookup is O(log N) with the index; at ~0.1ms per query this is ~500ms total. Acceptable for a synchronous endpoint.
- **Caching**: `GET /v1/faces/clusters` is pure compute — no cached snapshot. Clustering is invalidated by any face assignment or new face detection. Given the sub-second cost this is acceptable for v1. If profiling shows otherwise, add a materialized snapshot invalidated by a `cluster_dirty` flag.

**Known limitation — false merges:** Connected-component clustering can chain-link two different people through intermediate faces that are ambiguous (e.g., siblings, faces at odd angles). This is a known v1 trade-off. Mitigations: (1) the 0.55 threshold is conservative for ArcFace, (2) users can split incorrect clusters in Phase 3 by unassigning faces, (3) future improvement could use stricter intra-cluster density checks. QA should test with faces of similar-looking people (siblings, same ethnicity) to validate threshold quality.

**`faces.person_id` sync:** The denormalized `faces.person_id` column is updated in the same transaction as `face_person_matches` writes (assign, unassign, merge). It is never the source of truth — `face_person_matches` is authoritative. If they diverge, a repair query can reconcile: `UPDATE faces SET person_id = (SELECT person_id FROM face_person_matches WHERE face_id = faces.face_id LIMIT 1)`.

### CLI (if applicable)

No new CLI commands. Existing `lumiverb repair faces` handles backfill. A future `lumiverb cluster` command could trigger server-side clustering, but the initial implementation uses the API endpoint (`GET /v1/faces/clusters`).

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Image with no detected faces | "Show faces" button hidden in lightbox; `has_faces` filter excludes it |
| Video asset | Face overlay not shown (detection is stills-only per ADR-009) |
| Face with no bounding box (NULL) | Skip rendering overlay for that face |
| Person deleted while viewing their page | 404 → redirect to `/people` |
| Merge person A into person B | All face_person_matches updated, person A deleted, centroid B recomputed |
| Same face assigned to two people | Disallowed — `face_person_matches` enforced unique on `face_id` (one person per face) |
| Cluster with only 1 face | Shown separately in "unclustered" section, not in main cluster list |
| `POST /v1/people` with `face_ids` already assigned | 409 Conflict listing the already-assigned face_ids and their current person. Client must unassign first. |
| Concurrent merge of same source person | Serialized via `SELECT ... FOR UPDATE` on source; second request gets 404 (source already deleted) |
| User names a cluster, then finds more faces of same person | Assign new faces to existing person → centroid updates |
| Cross-library person search | People are tenant-scoped; search by person works across all libraries |
| Face overlay on zoomed/panned image | Phase 1 uses CSS % positioning which scales with image; works at any zoom. **Implementation note:** overlay divs must share the same CSS transform ancestor as the `<img>`. If future zoom uses `transform: scale()` on a wrapper, the overlay container must be inside that same wrapper, not a sibling. |
| `object-contain` letterboxing | Avoided by `inline-block` wrapper that shrinks to natural image size |

## Code References

| Area | File | Notes |
|------|------|-------|
| Face model | `src/models/tenant.py:365-422` | Face, Person, FacePersonMatch models |
| Face repository | `src/repository/tenant.py:2243-2305` | FaceRepository (submit, get_by_asset_id) |
| Face API | `src/api/routers/assets.py:922-1010` | Submit/list faces endpoints |
| Facets | `src/api/routers/facets.py` | Needs `has_face_count` aggregate |
| Search filter | `src/api/routers/search.py:99` | `has_faces` param (already implemented) |
| Browse filter | `src/api/routers/browse.py` | `has_faces` param (already implemented) |
| Page filter | `src/repository/tenant.py:482-485` | SQL conditions for `has_faces` |
| FilterBar | `src/ui/web/src/components/FilterBar.tsx` | "Has location" pattern to replicate |
| Lightbox | `src/ui/web/src/components/Lightbox.tsx` | Image display, sidebar, keyboard shortcuts |
| Search parser | `src/ui/web/src/lib/parseSearchQuery.ts` | `has:X` filter pattern |
| localStorage | `src/ui/web/src/lib/useLocalStorage.ts` | Persistent preference hook |
| API client | `src/ui/web/src/api/client.ts` | `pageAssets()`, `browseAll()`, types |
| Types | `src/ui/web/src/api/types.ts` | FacetsResponse (needs `has_face_count`) |
| Similarity | `src/repository/tenant.py:1028-1152` | `find_similar()` pgvector pattern to reuse for clustering |
| HNSW index | `migrations/tenant/versions/f6g7h8i9j0k1_face_detection_columns.py:64-68` | Cosine distance index on face embeddings |

## Doc References

- `docs/cursor-api.md` — Update with people endpoints, clustering API, person search filter
- `docs/architecture.md` — Update people/clustering description
- ADR-009 — Parent ADR; this ADR extends it

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

### Phase 1 — Face Search Filter + Lightbox Overlay

**Deliverables:**
- Backend: `has_face_count` in facets response
- Frontend types: `FaceItem`, `FaceListResponse`, `has_face_count` on `FacetsResponse`
- API client: `hasFaces` in `PageAssetsOptions`, `listFaces()` function
- Search parser: `has:faces` filter pattern
- FilterBar: "Has faces (N)" checkbox + chiclet (mirrors "Has location")
- BrowsePage + UnifiedBrowsePage: wire `has_faces` URL param through to API
- Lightbox: face bounding box overlay with toggle button, `useLocalStorage` persistence, `d` keyboard shortcut
- Tests: facets endpoint returns `has_face_count`; parseSearchQuery handles `has:faces`; regression test for `GET /v1/assets/{id}/faces` response shape (the lightbox depends on this — Phase 2 extends `FaceItem` with person data, so a regression test catches shape breaks early)

**Does NOT include:** Person names on face boxes, people page, clustering, person search

**Read-ahead:** The `FaceItem` type includes a `person` field (null for now) that Phase 2 will populate. The `listFaces()` API client function will be reused by the cluster management UI.

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] TypeScript compiles (`npx tsc --noEmit`)
- [ ] Vite builds (`npx vite build`)
- [ ] Docs updated
- [ ] Phase status updated above

### Phase 2 — Clustering API + People CRUD + People Page

**Deliverables:**
- `PersonRepository` with CRUD + centroid management
- Migration: add `faces.person_id` denormalized column
- Clustering endpoint: `GET /v1/faces/clusters` using pgvector cosine nearest-neighbor
- People CRUD: `GET/POST/PATCH/DELETE /v1/people`, `GET /v1/people/{id}/faces`
- People page (`/people`): grid of named people sorted by face count
- Person detail page (`/people/{id}`): photos of this person
- Sidebar nav link to People page
- Lightbox: populate `person` field on face items; color-coded boxes (green=identified, gray=unknown); name labels on identified faces; click identified face → navigate to `/people/{personId}`

**Does NOT include:** Cluster assignment UI, merge, fix bad tags, search by person

**Read-ahead:** Cluster assignment UI (Phase 3) needs the clusters endpoint and people CRUD from this phase.

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing
- [ ] TypeScript + Vite clean
- [ ] Docs updated
- [ ] Phase status updated above

### Phase 3 — Cluster Management UI + Merge/Rename/Fix

**Deliverables:**
- Assign endpoint: `POST /v1/faces/{face_id}/assign`
- Unassign endpoint: `DELETE /v1/faces/{face_id}/assign`
- Merge endpoint: `POST /v1/people/{person_id}/merge`
- Cluster management panel on People page: name clusters, assign to existing person
- Single-face correction: reassign face, remove bad match
- Centroid recomputation on confirm/merge

**Does NOT include:** Search by person name, similarity integration

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing
- [ ] TypeScript + Vite clean
- [ ] Docs updated
- [ ] Phase status updated above

### Phase 4 — Search by Person Name + Similarity Integration

**Deliverables:**
- `person:"name"` search filter in parseSearchQuery
- Backend: `person_id` and `person` query params on search/browse/page endpoints
- Person name chips in lightbox face overlay (click → navigate to `/people/{id}`)
- Person name as a factor in similarity scoring (boost images with same named person)

**Does NOT include:** Automatic face recognition on new ingests (future — would auto-assign faces near known centroids)

**Similarity integration detail:** "Boost images with same named person" is an **in-app rerank**, not a Quickwit index change. When the similarity endpoint returns CLIP-based candidates, a post-processing step checks if any candidates share a `person_id` with any face in the source image. Matching candidates get a score boost (e.g., distance *= 0.85). This only activates when the source image has at least one identified face — it does not apply when no faces are present. This is a lightweight change to the existing `find_similar()` reranking pipeline, not a new search index.

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing
- [ ] TypeScript + Vite clean
- [ ] Docs updated
- [ ] Phase status updated above

## Alternatives Considered

**Client-side clustering (WASM/JS):** Rejected. The face embeddings are in Postgres with an HNSW index — server-side clustering via pgvector is simpler, faster, and doesn't require shipping embeddings to the browser.

**Per-library people:** Rejected. The user explicitly requires tenant-scoped people — tag Susan once, recognized everywhere. The `people` table is already in the tenant database (not partitioned by library).

**Dedicated clustering model (DBSCAN, etc.):** Deferred. Starting with simple greedy connected-component clustering using pgvector nearest-neighbor queries. If quality is insufficient, can swap in DBSCAN or hierarchical clustering later without API changes.

**`f` keyboard shortcut for face toggle:** Rejected — `f` is already "favorite" (Lightroom convention). Using `d` (detect/display) instead.

## What This Does NOT Include

- **Automatic face recognition on ingest** — auto-assigning faces to known people during `lumiverb ingest`. Future enhancement: when a face's embedding is within threshold of a known person's centroid, auto-assign with `confirmed=false`.
- **Video face detection** — ADR-009 scoped detection to stills only. Video frame extraction for face detection is a separate concern.
- **Face crop thumbnails** — Showing cropped face images (vs full image thumbnails with bounding boxes). Could improve the cluster management UX but adds image processing complexity.
- **Duplicate detection** — Explicitly deferred to a separate ADR per ADR-009.
- **Face-based deduplication** — "These two photos have the same faces" as a similarity signal.

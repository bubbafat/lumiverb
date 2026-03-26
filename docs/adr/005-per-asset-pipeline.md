# ADR-005: Per-Asset Pipeline and Atomic Ingest

## Status
Proposed

## Context

The current pipeline processes assets in stage-ordered waves: proxy for all assets, then EXIF for all, then vision for all, then embeddings for all. Each stage is a separate worker with its own job queue, claim/complete API cycle, and network round-trips.

This design made sense when workers and server were co-located — job queues were cheap (local DB) and artifacts were on shared disk. Now that workers run remotely:

- **Wasted bandwidth**: The vision worker downloads the proxy it just uploaded two stages ago.
- **Sequencing failures**: Downstream stages can race ahead before upstream stages finish (proxy not yet uploaded when vision starts).
- **Per-stage overhead**: Each job requires claim → process → upload → complete — four API calls per stage per asset. For 3,000 assets across 5 stages, that's 60,000 API calls.
- **No locality**: The proxy image is in memory on the client after resize, but gets discarded and re-fetched later.

The natural model for a remote worker is a per-asset pipeline: process one asset through all stages in a single pass, keeping intermediate results in memory.

## Proposed Architecture

### Per-asset pipeline (client-side)

For each asset, the client runs a single-pass pipeline:

```
1. Scan → server creates/confirms asset record, returns asset info
2. Resize source → produce proxy (keep in memory, don't discard)
3. Extract EXIF from source (parallel with resize or sequential)
4. Send proxy to vision AI → get description + tags (proxy already in memory)
5. Compute embeddings from proxy (proxy still in memory)
6. Upload everything to server in one request
```

Steps 2-5 are pure client-side compute with no server interaction. Step 6 is a single API call that atomically delivers all results.

### Atomic ingest endpoint

A new `POST /v1/assets/{asset_id}/ingest` (or `POST /v1/ingest`) endpoint that accepts all pipeline outputs in one multipart request:

```
POST /v1/assets/{asset_id}/ingest
Content-Type: multipart/form-data

Fields:
  proxy:        <file>          # JPEG/WebP image bytes
  exif:         <json>          # EXIF metadata JSON
  vision:       <json>          # {model_id, description, tags}
  embeddings:   <json/binary>   # CLIP + text vectors
  width:        <int>           # source image dimensions
  height:       <int>
```

Server-side, this endpoint:
1. Normalizes the proxy (re-encode to WebP, enforce 2048px max, don't upscale)
2. Generates and caches the thumbnail (512px WebP) from the normalized proxy
3. Stores the EXIF metadata
4. Stores the vision description and tags
5. Stores the embedding vectors
6. Updates the asset record atomically (proxy_key, thumbnail_key, status, etc.)
7. Enqueues search sync

If any step fails, the whole ingest is rejected — no partial state.

### Existing per-field APIs remain

The individual endpoints stay for:
- **Editing**: Update just the description, re-run vision on one asset, etc.
- **Server-side workers**: If a server-side worker needs to update one field.
- **Backward compatibility**: Existing CLI versions continue to work until deprecated.
- **Video pipeline**: Video processing has different stage dependencies and may stay stage-based longer.

### Server-side normalization (proxy + thumbnail)

Regardless of the pipeline rearchitecture, the server should normalize uploads:
- **Proxy**: Re-encode to WebP, enforce 2048px long edge max (don't upscale), reject oversized uploads.
- **Thumbnail**: Generated server-side on-demand from the proxy. Not uploaded by the client. 512px long edge, WebP, cached to disk on first request.

This simplifies the client (no thumbnail generation, no WebP encoding required) and guarantees consistent storage.

## Phased Rollout

Each phase leaves the system fully working. Phases can be deployed and validated independently.

### Phase 1: Atomic ingest endpoint
**Changes**: Server only. New endpoint alongside existing APIs. Fully backward compatible.

- Add `POST /v1/assets/{asset_id}/ingest` endpoint.
- Accepts multipart: `proxy` (file, required), `exif` (JSON, optional), `vision` (JSON, optional), `embeddings` (JSON/binary, optional).
- Server normalizes proxy to WebP (2048px long edge max, no upscale).
- Server generates and caches thumbnail (512px WebP) from normalized proxy.
- Stores all provided metadata atomically — asset record updated in one transaction.
- Enqueues search sync.
- Returns the complete asset state.
- EXIF, vision, and embeddings are optional: a client that only has the proxy can ingest now and enrich later via the existing per-field APIs.

**Validation**: Integration tests for the new endpoint. Existing pipeline and APIs unaffected.

### Phase 2: Client-side scan + ingest pipeline
**Changes**: Client only. Server already has everything it needs from Phase 1.

- New CLI command (or new mode of `lumiverb pipeline`): scan + ingest in one pass.
- For each asset: discover file → resize → extract EXIF → call vision AI → compute embeddings → POST /v1/assets/{id}/ingest.
- Proxy stays in memory across all stages — no download, no re-fetch.
- Concurrency via thread pool (N assets processed in parallel).
- Progress reporting per-asset instead of per-stage.
- Scan creates/confirms the asset record first (`POST /v1/scans` or similar), then ingests.

**Validation**: Run against a library. Compare results with stage-based pipeline. Verify assets are fully populated after one pass.

### Phase 3: Existing upload/complete APIs become edit APIs
**Changes**: Server + client. Existing APIs stay but their role changes.

- The individual artifact upload and job-complete endpoints become the way to **edit** existing asset data (re-run vision on one asset, update EXIF, replace a proxy, etc.).
- Atomic ingest is the primary path for new assets. The server is always in a consistent state — no partial assets.
- Update CLI commands: `lumiverb vision --asset <id>` re-runs vision for one asset and calls the edit API. Similar for EXIF, embeddings.
- Document the distinction: ingest = create with full data, edit = update individual fields on existing assets.

**Validation**: Existing edit workflows continue to work. No breaking changes.

### Phase 4: Remove stage-based job queues for images
**Changes**: Server + client. Breaking change for old CLI versions.

- Remove individual worker commands for image processing (proxy, exif, ai_vision, embed).
- Remove image job queue infrastructure (enqueue, claim, complete for image stages).
- Job queue remains for video stages (scene detection, video-vision, video-preview).
- Remove thumbnail upload from client — server generates it from proxy.
- Clean up `RemoteArtifactStore.write_artifacts_batch` and related batch endpoints that were transitional.

**⚠ Breaking**: Old CLI versions that use stage-based workers will no longer work for image processing. Server and client updates must be coordinated. Call this out in release notes.

### Phase 5: Server-side proxy normalization for all paths
**Changes**: Server only.

- All proxy upload paths (ingest endpoint, edit API, legacy artifact upload) normalize to WebP 2048px.
- All thumbnail reads generate on-demand from proxy if cached file is missing.
- This is the cleanup phase — ensures consistency regardless of how the proxy arrived.

### Phase 6 (future): Video pipeline consolidation
- Video has more complex dependencies (scene detection → per-scene vision → assembly).
- May stay stage-based or get a per-asset pipeline with sub-stages.
- Not in scope for this ADR.

## Open Questions

1. **Resumability**: If the client crashes mid-library, how does it know which assets are complete? Options: (a) query asset status before processing — skip assets that already have a proxy, (b) idempotent ingest — re-run overwrites, cheap for already-complete assets. Leaning toward (a) with (b) as the safety net.
2. **Embedding format**: Binary vectors or JSON arrays? JSON is fine for now; optimize later if bandwidth matters.
3. **Scan + ingest atomicity**: Should there be a single `POST /v1/ingest` that creates the asset record AND stores all data in one call? Or keep scan (create record) separate from ingest (store data)? Separate is simpler — scan is lightweight metadata, ingest is heavyweight multipart.
4. **Per-type upload size limits**: Should the ingest endpoint enforce per-field limits (proxy < 5 MB, EXIF JSON < 100 KB, etc.)? Yes — see ADR-004.

## Decision

Proceed with phased rollout starting from Phase 1 (atomic ingest endpoint). Each phase is independently valuable and leaves the system fully working through Phase 3. Phase 4 is the only breaking change and requires coordinated deployment.

## Consequences

- **Phase 1**: Atomic ingest available. Server always stores consistent assets. Proxy normalization and thumbnail generation move server-side. No client changes needed.
- **Phase 2**: Dramatic reduction in API calls (from ~20 per asset to 1-2). No proxy re-download. Client pipeline is simpler and faster.
- **Phase 3**: Clean separation — ingest for creation, edit APIs for updates. Server is always in a good state.
- **Phase 4**: Simplified codebase, less infrastructure. Old CLIs break.
- **Long-term**: The per-asset model is a better fit for remote workers, eliminates partial-state bugs, and scales naturally with client concurrency.

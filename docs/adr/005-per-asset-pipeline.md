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

### Phase 1: Server-side proxy normalization and thumbnail generation
**Changes**: Server only. No client changes required. Fully backward compatible.

- Modify `POST /v1/assets/{id}/artifacts/proxy` to re-encode uploaded proxy to WebP, enforce 2048px max.
- Add on-demand thumbnail generation to `GET /v1/assets/{id}/artifacts/thumbnail` — if thumbnail file doesn't exist but proxy does, generate from proxy (512px WebP), cache, and serve.
- Update web UI to accept WebP thumbnails (should already work — browsers support WebP natively).
- Remove thumbnail upload from the proxy worker (it just uploads the proxy; server handles the rest).
- Update content type for proxy/thumbnail responses.

**Validation**: Run existing proxy worker. Server normalizes on upload. Web UI grid loads thumbnails lazily. All existing tests pass.

### Phase 2: Atomic ingest endpoint
**Changes**: Server only (new endpoint). No client changes required. Existing pipeline continues to work.

- Add `POST /v1/assets/{asset_id}/ingest` endpoint.
- Accepts proxy + EXIF + vision + embeddings in one multipart request.
- Server normalizes proxy, generates thumbnail, stores all metadata atomically.
- Enqueues search sync.
- Write integration tests for the new endpoint.

**Validation**: Hit endpoint manually or with a test script. Existing stage-based pipeline is unaffected.

### Phase 3: Client-side per-asset pipeline
**Changes**: Client only. Server already has everything it needs from Phase 2.

- New CLI command: `lumiverb pipeline-v2` (or replace `lumiverb pipeline`).
- For each asset: scan → resize → EXIF → vision → embed → ingest (single API call).
- Proxy stays in memory across stages — no re-download.
- Concurrency via thread pool (N assets processed in parallel).
- Progress reporting per-asset instead of per-stage.

**Validation**: Run against the same library. Compare results with stage-based pipeline. Measure round-trip reduction.

### Phase 4: Deprecate stage-based workers for images
**Changes**: Remove old code paths.

- Remove individual worker commands for image processing (keep for video).
- Remove thumbnail upload from `RemoteArtifactStore` (server generates it).
- Remove job queue infrastructure for image stages (proxy, exif, ai_vision, embed).
- Job queue remains for video stages and for retry/resumability tracking.

**⚠ Breaking**: Old CLI versions will no longer work for image processing after this phase. Server update and client update must be coordinated. Call this out in release notes.

### Phase 5 (future): Video pipeline consolidation
- Video has more complex dependencies (scene detection → per-scene vision → assembly).
- May stay stage-based or get a per-asset pipeline with sub-stages.
- Not in scope for this ADR.

## Open Questions

1. **Resumability**: If the client crashes mid-asset, how does it know which assets are complete? Options: (a) server tracks ingest status per asset, (b) client queries asset status before processing, (c) idempotent ingest — just re-run and the server overwrites.
2. **Partial ingest**: Should the ingest endpoint accept subsets (e.g. proxy + EXIF but no vision)? Useful for assets where vision is disabled or fails. Probably yes — make vision and embeddings optional fields.
3. **Scan integration**: Should scan + ingest be one API call (`POST /v1/ingest` with path + library + all data), or should scan remain separate? Separate is simpler and keeps scan as a lightweight metadata operation.
4. **Embedding format**: Binary vectors or JSON arrays? Binary is more compact but harder to debug. JSON is fine for now; optimize later if bandwidth becomes a concern.

## Decision

Proceed with phased rollout starting from Phase 1. Each phase is independently valuable and leaves the system working.

## Consequences

- **Phase 1**: Proxy worker gets simpler. Thumbnails become a server concern. WebP saves storage and bandwidth. No client coordination needed.
- **Phase 2**: New ingest endpoint available but optional. Existing pipeline unchanged.
- **Phase 3**: Dramatic reduction in API calls and bandwidth for image processing. Client keeps proxy in memory across stages.
- **Phase 4**: Simplified codebase. Old CLI versions break — requires coordinated release.
- **Long-term**: The per-asset model is a better fit for remote workers and scales naturally with client concurrency.

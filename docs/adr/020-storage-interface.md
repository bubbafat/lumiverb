# ADR-020: Storage behind one interface

## Status

Proposed (2026-10-16), for Robert's review.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Leaks fixed now: face crops and scene frames go with their clip; a deleted library's folder goes with it | Not started |
| 1 | The `Store` interface and its filesystem provider; contract tests | Not started |
| 2 | The API's own code through the `Store` (no paths outside the provider) | Not started |
| 3 | Producers get inputs and scratch from the runner; caches with caps | Not started |
| 4 | Originals behind a read-only `Originals` interface | Not started |
| 5 | Clients keep only bounded caches of their own | Not started |

## Overview

Robert, 2026-10-15: "I would expect in the server the cache management is handled by an api that abstracts the storage from the plugins. They should never be accessing a file directly but rather reading and writing data through that API. Our first implementation is the 'file system' cache provider but there could be s3 bucket provider or similar someday." And the same day: "anything transient is cleaned up and the server and client only ever talk through the API."

Today the code has a storage class, `LocalStorage`, but most callers ask it for a path and do the work themselves. The scheduler reads the API's files in place by path, which it finds by querying the account's database itself. Producers make and delete their own temporary files. Some caches have no limit. Some derived files are never deleted.

After this ADR, everything Lumiverb keeps on the server goes through one interface, by key. Each piece of data has an owner and an end. A producer gets what it needs from its job and never sees a path it chose. The first provider is the filesystem we have. An S3 provider becomes one more provider, not a rewrite.

## Motivation

Found by reading the code on 2026-10-16 (`src/server`, `src/producers`, `src/processing`, `src/client`):

- **Callers go around the storage class.** `LocalStorage` has `write`, `exists` and `delete`, but nothing to read, stream, list or size. Callers instead take `abs_path` or `write_path` and do the I/O themselves. That happens in:
  - artifact uploads, each with its own tmp file and rename (`routers/artifacts.py`);
  - ingest, which writes without a tmp file and can leave a half-written preview (`routers/ingest.py`);
  - face crops (`routers/assets.py`, `routers/people.py`);
  - playback cuts, a whole folder managed by hand (`routers/playback.py`);
  - the missing-file repair, which also calls the private `_under` (`missing_artifacts.py`);
  - the daily cleanup, which builds its own `LocalStorage` (`search/cleanup.py`);
  - the preview mover (`storage/move.py`);
  - ranged streaming (`routers/assets.py` `_stream_file_with_range`).
- **The scheduler reads the API's files in place.**
  - `scheduler/service.py` `_stored_file` queries the account's database for a key, asks `get_storage().abs_path()` and passes a local `Path` to the preview and analysis-proxy caches.
  - This only works because the API and the scheduler share `DATA_DIR`, `storage.json` and `STORAGE_PROVIDER=local`.
  - It's the right result reached the wrong way: no copy is kept, but through a back door.
- **Producers handle files.**
  - Every producer saves through the API, which is right. But they make their own temp files: OCR, descriptions, scene frames, transcripts' audio, video faces' frames, the analysis render.
  - Two read originals without saying so (`storage=True`): faces (`faces/work.py` `face_proxy`) and the preview cache's fallback (`proxy_cache.py`). Clip, OCR and descriptions all use that fallback.
- **Caches without limits.**
  - The preview cache (`ProxyCache`) is never evicted. It's used by the scheduler when it isn't beside the API, and by the CLI at `~/.cache/lumiverb/proxies`.
  - The model caches (CLIP's Hugging Face cache, InsightFace, Whisper) are never pruned.
  - Only the analysis-proxy cache has a cap (`LUMIVERB_ANALYSIS_CACHE_GB`).
- **Derived files never deleted.**
  - Face crops: not by the trash purge, not by the daily cleanup (`face_crops` isn't in its list), and not when a re-detection drops a face.
  - Scene frames: not by the trash purge (only the daily cleanup's orphan sweep catches them).
  - A deleted library's previews folder is only reported. Its `storage.json` entry stays.
  - A killed InsightFace copy leaves a `.copying-*` folder.
- **Dead or misleading parts.**
  - `storage/artifact_store.py` has no callers.
  - `get_storage()` ignores `STORAGE_PROVIDER`, while `docs/cursor-api.md` lists gcs and s3 values that don't exist.
- **Written three times.** On one machine an analysis proxy is rendered into the scheduler's cache, uploaded (spooled in the API's tmp), written beside its final place and renamed, and then the render is deleted.
- **Client and server share files.** On Linux the `lumiverb` user's CLI is pointed at the scheduler's cache folder (`cache_dir.py`). The CLI's preview cache is described as the one "the Mac app browses".

## Design

### 1. Four kinds of data, each with an owner and an end

| Kind | What | Owner | Ends |
|---|---|---|---|
| **Kept** | Derived artifacts: previews, thumbnails, video previews, analysis proxies, scene frames, face crops | The server's `Store` | With its clip (deleted for good), its face, or when its producer makes it again; the daily sweep removes anything no row names |
| **Cached** | Copies for speed: playback cuts, the scheduler's copies when it isn't beside the API, model weights | A `Cache`: the `Store`'s interface plus a policy | A size cap, least recently used first; emptied at start when it can't be trusted |
| **Scratch** | A job's work files: frames, audio, a render in progress | The job (the runner) | When the job ends, however it ends; the scratch root is emptied at start |
| **Originals** | The user's media in library folders | The user (never written: ADR-016 principle 1) | Never by Lumiverb |

Install state stays files that `lumiverb-server` manages: the env file, `storage.json`, `network.json`, upkeep and backup state, logs. These are configuration, not data a producer handles, and are out of scope. So are the database backups, which have their own folder and keeping rule (`backups.py`).

### 2. The `Store`: what Lumiverb keeps, by key

```python
class Store(Protocol):
    def open(self, key: str) -> BinaryIO: ...                  # read, seekable (ranges); NotFound
    def read(self, key: str) -> bytes: ...
    def size(self, key: str) -> int: ...
    def exists(self, key: str) -> bool: ...
    def write(self, key: str, data: bytes | BinaryIO) -> None: ...   # atomic: whole or not at all
    def delete(self, key: str) -> None: ...                    # gone wherever it is; fine if missing
    def list(self, prefix: str) -> Iterator[str]: ...          # for the sweep and moves
    @contextmanager
    def local_file(self, key: str) -> Iterator[Path]: ...      # for ffmpeg, libvips, ONNX
    @contextmanager
    def writing(self, key: str, suffix: str = "") -> Iterator[Path]: ...  # a tool writes it; saved on exit
```

- **Keys stay what they are:** `{tenant}/{library}/{kind}/{bucket}/...`. The key builders move from `LocalStorage` to the `Store` module unchanged, so no row changes.
- **`local_file`** is the one way to hand a stored file to a tool that needs a path. The filesystem provider yields the real path (no copy). An object-store provider downloads into scratch and removes it on exit.
- **`writing`** is how a tool writes one. The provider gives a temporary path and saves it atomically when the block ends without an error: rename on a filesystem, upload on an object store.
- **Two failures, never confused:**
  - `NotFound`: the key has no file. The missing-file repair acts on it.
  - `Unavailable`: the storage can't be reached (a chosen folder's drive unplugged, a bucket down). Work waits, and nothing is called missing. This keeps what `.lumiverb-previews` markers and `write_check` do today.
- **Where previews live belongs to the filesystem provider:** `storage.json`, a library's own folder, the marker and the mover. Callers never learn which folder a key is in. During a move, reads find either copy and writes go to the new one, as `LocalStorage` does now.
- **`get_store()` honours `STORAGE_PROVIDER`.** `local` is the filesystem provider; an unknown value refuses to start. `docs/cursor-api.md` stops listing providers that don't exist. `artifact_store.py` goes.
- **Serving.** API routes stream `store.open(key)` with ranges. On a filesystem, `local_file` may hand the path to `FileResponse` as a shortcut; that's the provider's choice, not the route's.

### 3. A `Cache`: a `Store` with a policy

```python
class Cache(Store):
    cap_bytes: int            # evicts least recently used past this; never a file a local_file holds
    trusted_at_start: bool    # False: emptied when its owner starts
```

- Every cache names its cap and where it lives. The current caches become:
  - **Playback cuts**, kind `playback`. Today they're a hand-managed folder with a 10-minute sweep. They get a cap, and are cleared when a public page's cap changes or a clip goes, as now.
  - **The scheduler's copies**, used only when it isn't beside the API. The preview cache gets a cap. The analysis-proxy cache keeps `LUMIVERB_ANALYSIS_CACHE_GB`.
  - **Model weights:** CLIP, InsightFace and Whisper, in one models folder with no cap. They're pinned by model name, and the ones no setting names are pruned.
- **Beside the API, the scheduler has no copies.** It reads the `Store` directly (decision 4). On another machine it fills its caches through the API.

### 4. Producers never see a path they didn't get from the job

The runner hands each job what it may use, and nothing else:

```python
class Inputs:
    @contextmanager
    def preview(self, clip) -> Iterator[Path]: ...         # the stored preview, as a local file
    @contextmanager
    def analysis_proxy(self, clip) -> Iterator[Path]: ...
    def preview_bytes(self, clip) -> bytes: ...
    @contextmanager
    def original(self, clip) -> Iterator[Path]: ...        # only for producers with storage=True

job.scratch: Path   # a folder of its own; removed when the job ends
```

- **Beside the API, inputs come from the `Store` by key.** The key comes with the job, from the queue that already reads the database. Elsewhere they come from the scheduler's `Cache`, filled through the API. The producer can't tell which, and doesn't need to.
- **Scratch replaces every `tempfile` and `TemporaryDirectory` in `src/producers`.** The runner removes it however the job ends. `lumiverb-server run` already empties `DATA_DIR/worker-tmp` at start, and that becomes the scratch root.
- **Outputs still go through the API**, as today. A producer whose output a tool writes (the analysis render) writes it into scratch, and the upload streams it from there.
- **Originals are read only by producers that declare `storage=True`, through `inputs.original`.** The faces producer and the preview fallback stop reading originals behind the scheduler's back. When storage is asleep, they wait or use the stored preview.
- **A test holds `src/producers` to this, as one holds `src` to libvips:** no imports of `tempfile`, `shutil`, `cache_dir`, `get_storage`/`get_store` or `resolve_source_path`, and no `open()` on a path.

### 5. Originals: a separate, read-only interface

This answers the open question: originals don't go through the `Store`. They aren't Lumiverb's (they're never written), and they differ in four ways:
- they're named by library and relative path, not by key;
- each machine maps them differently (`LUMIVERB_ROOT_MAP`);
- they may be unmounted or asleep (`reachable_root`);
- each library names who reads it (ADR-018 decision 13).

```python
class Originals(Protocol):
    def reachable(self, library) -> bool: ...
    def walk(self, library, scope: str) -> Iterator[Found]: ...      # the scan's listing
    def stat(self, library, rel_path: str) -> Stat: ...
    @contextmanager
    def local_file(self, library, rel_path: str) -> Iterator[Path]: ...
```

- The read side has the same shape as the `Store`'s, so a reader on another machine (ADR-018) can be a provider later.
- `roots.py`, `resolve_source_path` and the scan's walk move behind it.
- Nothing writes through it, and nothing outside it opens an original.

### 6. Everything transient has an end

- **Face crops** are deleted with their face: when a re-detection drops it, and when its clip is deleted for good. The daily sweep covers `face_crops`.
- **Scene frames** are deleted with their clip's purge, and when a scenes redo swaps them (that already happens).
- **A deleted library's previews folder** goes once the delete commits, and its `storage.json` entry goes with it.
- **Atomic writes** are the provider's own. Its `.tmp` files are swept by the provider, not by each caller's rules: `.part`, `.moving` and `.tmp` handled in one place.
- **Caches** have caps (decision 3). **Scratch** ends with its job and at start (decision 4).

### 7. Clients keep only bounded caches of their own

- **The CLI's scan keeps no preview cache.** It's a client: it asks the API. `~/.cache/lumiverb/proxies` goes. The Mac app keeps its own previews.
- **The CLI on a Linux server stops sharing the scheduler's cache folder.** It gets its own, or none.
- **The Mac app's caches get caps,** as iOS's already have (200 MB, least recently used).

### 8. No S3 provider until one is needed

The interface is shaped so one fits:
- callers never rename, link or `stat` a stored file;
- atomic writes, listing by prefix and `local_file` are the provider's job.

One contract test suite runs against every provider (only the filesystem one for now), so an S3 provider later passes the same tests or isn't done. If one is ever built, `fsspec` or boto3 can serve inside it.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| A chosen previews folder's drive is unplugged | Reads and writes raise `Unavailable`; work waits; nothing is called missing or repaired (as the marker check does now) |
| A key with no file | `NotFound`; the missing-file repair makes it again where it can (a thumbnail from its preview), as now |
| Previews are being moved | Reads find the file in either folder; writes go to the new one; deletes remove both (as now, inside the provider) |
| Killed mid-write | Nothing partial is ever visible under the key; the provider's leftovers are swept by it |
| A cache is full | The least recently used entries go, never one a `local_file` block holds |
| A job is killed | Its scratch folder is removed with the job, or at the next start |
| Storage is asleep and a producer needs the original | It waits (`storage=True`), as analysis proxies, capture and probe do now; producers that don't declare it use the stored preview |
| An object store's `local_file` | Downloaded into scratch, removed on exit; a crash leaves it for the start-up sweep |
| Two writers of one key | The last whole write wins (as with rename today) |
| A face re-detected away | Its crop file goes with its row |

## Code References

| Area | File | Notes |
|------|------|-------|
| Storage today | `src/server/storage/local.py` | `LocalStorage`: key builders, `abs_path`, `write_path`, `write`, `exists`, `delete`; becomes the filesystem provider |
| Dead code | `src/server/storage/artifact_store.py` | No callers; goes |
| Previews location | `src/shared/previews_location.py`, `src/server/storage/move.py` | Become the filesystem provider's own |
| Provider setting | `src/server/config.py` (`STORAGE_PROVIDER`), `lifecycle/init.py` | Honoured by `get_store()` |
| Path bypasses | `routers/artifacts.py`, `routers/ingest.py`, `routers/assets.py`, `routers/people.py`, `routers/playback.py`, `missing_artifacts.py`, `search/cleanup.py`, `upgrade/steps/backfill_artifact_sha256.py` | Phase 2 |
| In-place reads | `src/server/scheduler/service.py` (`_stored_file`, `stored_preview`, `stored_analysis_proxy`, `forget_preview_copies`), `scheduler/account.py` | Phase 3 |
| Caches | `src/processing/proxy/proxy_cache.py`, `src/processing/proxy/analysis_cache.py`, `src/processing/cache_dir.py` | Phase 3 |
| Producers' files | `src/producers/*/work.py` (ocr, vision, scene_vision, transcript, video_faces, analysis_proxy, faces), `src/producers/runner.py` (`Work.original`) | Phase 3 |
| Model caches | `workers/faces/insightface_provider.py`, `server/embeddings/clip_provider.py`, `workers/embeddings/clip_provider.py`, `workers/transcripts/child.py` | Phase 3 |
| Originals | `src/processing/roots.py`, `src/shared/io_utils.py` (`resolve_source_path`), `src/processing/ingest.py` (`_walk_library`), `src/shared/readers.py` | Phase 4 |
| Leaks | `routers/trash.py` (`purge_assets`), `search/cleanup.py` (`_ARTIFACT_SUBDIRS`), `repository/tenant.py` (`submit_faces`), `routers/libraries.py` (`delete_libraries_for_good`) | Phase 0 |
| Clients | `src/processing/scan.py` (`cache_previews`), `src/processing/cache_dir.py`, the Mac app's `MacProxyDiskCache` / `MacThumbnailDiskCache` | Phase 5 |

## Doc References

- `docs/architecture.md`: storage and caches.
- `docs/cursor-api.md`: drop the storage providers that don't exist; nothing in the API changes.
- `docs/proxy-cache-protocol.md`: rewritten or retired in phase 5.
- `CLAUDE.md`: the "Where previews live", "Proxy gen / cache" and "Image caches" rows.

## Build Phases

### Requirements

Those of `docs/adr/000-template.md`: tests for every change, the full suite passing, docs and this table updated, and read-ahead into later phases. Each phase is its own PR (or a few), released and checked on the Mac before the next.

### Phase 0 — Leaks fixed now

These are bugs whatever happens to the rest.

**Deliverables:**
- Deleting clips for good removes their face crops and scene frames.
- A re-detection that drops a face removes its crop.
- The daily sweep covers `face_crops`.
- Deleting a library for good removes its previews folder after the commit, and its `storage.json` entry.

**Done when:** tests show each file gone with its row, and a sweep of a test account finds no orphans.

### Phase 1 — The `Store` and its filesystem provider

**Deliverables:**
- The `Store` protocol, `NotFound` / `Unavailable`, and `get_store()` honouring `STORAGE_PROVIDER`.
- The filesystem provider: `LocalStorage`, the previews location and the mover, inside it.
- The contract test suite.
- `artifact_store.py` removed.

**Does NOT include:** changing callers (phase 2).

### Phase 2 — The API's own code through the `Store`

**Deliverables:**
- Uploads, ingest (atomic now), face crops, ranged streaming, the missing-file repair, the daily sweep and the backfill step use the `Store`.
- Playback cuts become a `Cache`.
- A test holds everything outside `src/server/storage/` to no `abs_path` or `write_path`.

### Phase 3 — Producers' inputs and scratch, caches with caps

**Deliverables:**
- `Inputs` and `job.scratch` from the runner.
- The scheduler reads the `Store` by key, with no database query of its own for a path.
- The preview and analysis-proxy caches become `Cache`s with caps, and model weights go in one folder.
- The faces producer and the preview fallback read originals only through `inputs.original`.
- The import test for `src/producers`.

### Phase 4 — Originals behind `Originals`

**Deliverables:** the interface, with `roots.py`, `resolve_source_path` and the scan's walk behind it. Nothing outside it opens an original.

### Phase 5 — Clients

**Deliverables:**
- The CLI's scan keeps no preview cache.
- The Linux CLI has its own cache.
- The Mac app's caches have caps.
- `docs/proxy-cache-protocol.md` updated or retired.

## Alternatives Considered

- **Keep paths and add discipline.** That's how eight callers came to work around `LocalStorage`. Rejected: an interface without paths is the discipline.
- **`fsspec` as the interface.** It brings S3 and others for free. But it speaks in paths everywhere, and the guarantees we rely on (atomic whole-file writes, a drive unplugged told apart from a file missing, moves between folders) aren't the same across its backends. Rejected as the interface; it can serve inside an object-store provider.
- **Originals through the same `Store`.** Rejected (decision 5): they aren't ours, they're named by library and path, mapped per machine, may be asleep, and are read by the library's reader.
- **The scheduler always downloads over the API, even beside it.** Simple and uniform, but it copies every preview and analysis proxy the API already has on the same disk. Rejected: beside the API it reads the `Store`, which is the API's own code, not a back door.

## What This Does NOT Include

- An S3 or other object-store provider (decision 8).
- Moving the database backups or install state behind the `Store`.
- Remote readers of originals (ADR-018 phase 3); this only gives them an interface to implement.

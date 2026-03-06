# Reference: Worker Base Pattern & Lease System
> Frozen reference from `media-search` PoC. Do not modify.
> Source: `src/workers/base.py`, `src/workers/ai_worker.py`, `src/workers/metadata_worker.py`

## What It Does

All workers in the system share a common base that provides:
- Pull-based work claiming with PostgreSQL `FOR UPDATE SKIP LOCKED`
- Lease expiry for abandoned-job recovery
- Heartbeat thread for liveness tracking
- Command handling (pause / resume / shutdown / forensic_dump)
- Graceful SIGINT / SIGTERM shutdown
- Pre-flight schema version check

---

## Core Pattern: `BaseWorker`

```python
class MyWorker(BaseWorker):
    def process_task(self) -> bool:
        # Claim work from DB
        # Do work
        # Return True if work was done, False/None if queue empty
        ...
```

The run loop:
1. Check DB for command (`pause` / `resume` / `shutdown` / `forensic_dump`)
2. If paused, sleep 1s and loop
3. Call `process_task()`
4. If work done → sleep 0.1s and loop immediately
5. If no work → sleep `worker_idle_poll_seconds` (default 5.0s)

With `once=True`, keeps calling `process_task()` until it returns `False` — no idle sleep.

---

## Lease Mechanism

**Claiming work (atomic):**
```sql
UPDATE asset
SET status = 'processing',
    worker_id = :worker_id,
    lease_expires_at = now() + interval '10 minutes'
WHERE id IN (
    SELECT id FROM asset
    WHERE status = :target_status
    ...
    FOR UPDATE SKIP LOCKED
    LIMIT :batch_size
)
```

`FOR UPDATE SKIP LOCKED` is non-negotiable — it prevents race conditions when multiple workers run in parallel without any central dispatcher.

**Recovery:** Any asset with `status = 'processing'` and `lease_expires_at < now()` is "abandoned" and eligible for re-claiming by any healthy worker. The recovery check runs before each claim query.

**Heartbeat:** A daemon thread updates `worker_status.last_seen_at` every 15 seconds. Workers with `last_seen_at > 60s ago` are considered stale.

---

## Transient vs Permanent Failures

**AI Worker (`ai_worker.py`) handles two failure classes:**

Transient (inference service down — reset to pre-claim status for retry):
```python
_TRANSIENT_EXCEPTIONS = (
    MoondreamUnavailableError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.RetryError,
    urllib3.exceptions.MaxRetryError,
    urllib3.exceptions.NewConnectionError,
)
```

Permanent (unexpected error — asset marked `poisoned`):
- All other exceptions
- Asset written to `poisoned` status with `error_message` for human review
- Worker logs at `ERROR` level with full traceback

---

## Worker Scoping

Workers can run in two modes:

- **Library-scoped:** `library_slug` set → claims only from that library
- **Global:** `library_slug = None`, `global_mode = True` → claims across all libraries

The model-aware claiming path (`target_model_id`) only claims assets whose library's effective target model matches the worker's model:
```
effective_model = COALESCE(library.target_tagger_id, system_default_model_id)
```

---

## Repair Pass (`ai_worker.py`)

Run with `--repair` flag before the normal work loop. Finds all assets analyzed with a model that no longer matches their library's effective target, resets them to `proxied` for re-analysis.

Batch size for repair: 500 assets per query.

---

## Metadata Worker Phases

`MetadataWorker` has two distinct phases with separate claim queues:

**`exif` phase:**
- Claims assets with `metadata_status = NULL` (need EXIF)
- Reads source file path from `library.absolute_path / asset.rel_path`
- Runs `exiftool -json -n` via a persistent long-lived process (avoids startup overhead)
- Strips `MakerNote*` keys to keep `raw_exif` size bounded
- Writes `raw_exif` (full) and `media_metadata` (normalized) to DB

**`sharpness` phase:**
- Claims assets with `metadata_status = 'exif_done'`
- Reads the **thumbnail** (not the source or proxy) via `LocalMediaStore`
- Computes Laplacian variance sharpness, normalized to `[0.0, 1.0]` against `SHARPNESS_MAX_VARIANCE = 1000.0`
- Detects faces via MediaPipe BlazeFace (short range, `min_detection_confidence = 0.5`)
- Writes `has_face`, `face_count`, `sharpness_score` to DB
- If thumbnail missing: resets asset to `exif_done` for retry (does not poison)

---

## Concurrent Batch Processing (AI Worker)

The AI Worker uses `ThreadPoolExecutor` to process a batch concurrently:

```python
with ThreadPoolExecutor(max_workers=len(assets)) as executor:
    futures = {executor.submit(_process_one, a): a for a in assets}
    for future in as_completed(futures):
        ...
```

Memory pressure check: before calling `analyze_image`, the worker queries how many other local workers are active. If `active_count > 0`, it passes `should_flush_memory=True` to the analyzer to hint that VRAM should be freed between calls.

---

## Schema Version Guard

Every worker calls `_check_compatibility()` on startup. It reads `system_metadata.schema_version` and compares it to `BaseWorker.REQUIRED_SCHEMA_VERSION = "1"`. Workers refuse to start if the DB schema is ahead or behind.

---

## Signal Handling

SIGINT and SIGTERM both set `_shutdown = True` and `should_exit = True`. The run loop checks these flags before each iteration. On shutdown:
1. Final `process_task()` call completes normally
2. Worker row is deleted from `worker_status` table
3. Heartbeat thread is joined with 2s timeout

---

## Worker Types (PoC)

| Worker | Claims | Output |
|--------|--------|--------|
| `ScannerWorker` | filesystem walk | `asset` rows as `pending` |
| `ProxyWorker` | `pending` images | JPEG proxy + thumbnail, `proxied` |
| `VideoProxyWorker` | `pending` videos | scenes, head-clip, `proxied` |
| `AIWorker` (light) | `proxied` images | Moondream description+tags, `analyzed_light` |
| `AIWorker` (full) | `analyzed_light` images | adds OCR, `completed` |
| `VideoWorker` | `proxied` videos with scenes | scene AI descriptions, `completed` |
| `MetadataWorker` (exif) | any `completed`/`proxied` | raw EXIF + normalized metadata |
| `MetadataWorker` (sharpness) | `exif_done` | sharpness score + face count |
| `SearchSyncWorker` | `search_sync_queue` | pushes to Quickwit |

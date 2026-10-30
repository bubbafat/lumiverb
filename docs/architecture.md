# Lumiverb — Architecture Document
*Version 1.0 | Working document — name TBD*

> **Direction:** [ADR-016: Operating Model](adr/016-operating-model.md) sets where Lumiverb is going. This document describes the system as built. Where the two disagree, ADR-016 wins.

---

## 1. Product Overview

Lumiverb is an AI-powered photo and video library management system for professional photographers, videographers, and serious hobbyists. It provides intelligent search, similarity discovery, and metadata enrichment across large personal media libraries (25,000+ assets).

It is not a photo editor. It is not a cloud backup service. It is the intelligent index and search layer that sits on top of your existing file storage.

### Core Value Proposition

- Natural language search across your entire library ("misty morning shoot in the mountains")
- Visual similarity search ("find more like this")
- AI-generated scene descriptions for every asset, including video
- Full support for RAW files, high-resolution video, and professional formats
- Privacy-first: source files never leave your infrastructure

### Deployment Modes

The same codebase supports two deployment modes:

| Mode | Operator | Cost | Infrastructure |
|---|---|---|---|
| Cloud-hosted | Product team | ~$12/month subscription | Not built: the original plan (GCP: Cloud Run, Cloud SQL, Cloud Storage) |
| Self-hosted | User | Free / one-time fee | Built: one machine, a Mac (Homebrew) or a Linux box (systemd), section 11 |

This follows the Immich model: fully open source, no open-core, with a hosted option for convenience. What's built is the self-hosted mode; ADR-016 sets how it runs.

---

## 2. Architectural Principles

1. **API-first.** Every capability is exposed via the REST API. The CLI, web UI, and Mac agent are all API clients. Nothing bypasses the API.
2. **Originals are read-only.** Anything that reads source files (scanning, probing, proxy generation) runs on a machine that can read them: the server's scheduler through a read-only mount (ADR-016), or the CLI's scan on the machine that holds them. The macOS app reads none. Originals are never written, moved, renamed or uploaded; the API never receives source files.
3. **Stateless API server.** The API server holds no local state. It can be replaced, scaled, or moved without data loss.
4. **Per-tenant isolation.** Each tenant has a dedicated database. Cross-tenant data leakage is architecturally impossible.
5. **Regenerable caches.** Proxies, thumbnails, search indexes and AI outputs are all regenerable from source. Human data in Postgres (person names, corrections, trash, projects, filters, settings) is irreplaceable and is never overwritten automatically by derived data (ADR-016).
6. **Storage behind one interface** (ADR-020). What Lumiverb makes is kept in a `Store`, by key, and nothing else learns where (section 3.7); a library's originals are reached only through `src/processing/originals.py`. The filesystem is the one provider; another (S3, say) would be a provider and nothing else.

---

## 3. System Components

### 3.1 Control Plane

A single shared Postgres database (tiny):

```
tenants           — tenant_id, name, plan, status, vision_model_id,
                    transcript_model_id, created_at
ai_machines       — machine_id, tenant_id, name, api_url, api_key, jobs, at_once,
                    enabled, built_in, online, status_error, models, checked_at,
                    created_at
api_keys          — key_id, key_hash, tenant_id, name, label, scopes,
                    role, created_at, last_used_at, revoked_at
users             — user_id, tenant_id, email, password_hash, role,
                    created_at, last_login_at
password_reset_tokens — token_hash, user_id, expires_at, used_at
public_libraries  — library_id, tenant_id, created_at
public_projects   — project_id, tenant_id, created_at
revoked_tokens    — jti (PK), revoked_at. Server-side JWT revocation.
tenant_db_routing — tenant_id, connection_string, region
installed_plugins — each plug-in installed on the server: its version, manifest,
                    default on or off and what was accepted (ADR-023)
plugin_install_log — every install, update, uninstall and failure, by whom
plugin_server_settings — each plug-in's server settings
```

The control plane handles: tenant provisioning, API key validation, JWT revocation tracking, and routing requests to the correct tenant database. It never stores media metadata. A user with `server_admin` set is a server admin: what's the install's rather than an account's (CLAUDE.md "Auth / tenant resolution").

Where an account's database is, is its `tenant_db_routing` row, for every caller: a request (the middleware), a public link (which names only its account), and work outside one, such as a response's background task, upkeep for every account or the scheduler (`src/server/database.py` `tenant_database_url`, behind `get_tenant_session(tenant_id)`). A restore rewrites that row. The env file's `TENANT_DATABASE_URL_TEMPLATE` names only a new account's database (`src/server/accounts.py`), so nothing else reads it (`tests/test_account_database.py`).

### 3.2 Tenant Database (per tenant)

Each tenant gets a dedicated Postgres database on the same Postgres instance (scales to hundreds of tenants before needing separate instances).

**Core tables:**

```
libraries         — library_id, name, root_path, status, scan_status,
                    last_scan_at, is_public, revision (int), created_at,
                    updated_at
assets            — asset_id, library_id, rel_path, content_hash (scheme:value), file_size,
                    file_mtime, media_type, width, height, duration_sec,
                    proxy_key, proxy_sha256, thumbnail_key, thumbnail_sha256,
                    video_preview_key, video_indexed (bool),
                    exif (JSON), exif_extracted_at, camera_make,
                    camera_model, taken_at, gps_lat, gps_lon, iso,
                    exposure_time_us, aperture, focal_length,
                    focal_length_35mm, lens_model, flash_fired, orientation,
                    availability, status, error_message,
                    created_at, updated_at, deleted_at, search_synced_at
video_scenes      — scene_id, asset_id, scene_index, start_ms, end_ms,
                    rep_frame_ms, proxy_key, thumbnail_key,
                    rep_frame_sha256, description, tags (JSONB),
                    sharpness_score, keep_reason, phash, created_at,
                    likeness (vector: its picture's CLIP vector), likeness_model,
                    words (tsvector, computed: description and tags)
asset_search      — asset_id, described, note, ocr_text, transcript_text,
                    path_words, snippet; everything / shown / shown_capped
                    (tsvectors, computed; ADR-019)
transcript_segments — asset_id, seq, start_ms, end_ms, text, words (tsvector, computed)
video_index_chunks — chunk_id, asset_id, chunk_index, start_ms, end_ms,
                    status, worker_id, claimed_at, lease_expires_at,
                    completed_at, error_message, created_at
asset_metadata    — metadata_id, asset_id, model_id, model_version,
                    generated_at, data (JSONB)
asset_embeddings  — embedding_id, asset_id, model_id, model_version,
                    embedding_vector vector(512), created_at
system_metadata   — key, value, updated_at
```

**Views:**
- `active_assets` — non-trashed assets (deleted_at IS NULL). All pipeline queries use this view. Trashing a library sets deleted_at on all its assets, removing them from this view immediately.

**Key constraints:**
- `video_index_chunks`: a failed chunk stays failed for the rest of its run (the client ends the run at it, and the clip's scenes job is charged a failure); the next run's `POST /v1/video/{asset_id}/chunks` puts it back to `pending`, under the scheduler's back-off.
- `video_scenes`: unique on `(asset_id, scene_index)`; the server numbers a clip's scenes as each chunk completes, after those stored.
- `assets`: unique constraint on `(library_id, rel_path)` — ingest upserts by this key.

**Artifact lifecycle:**
- When a library is trashed (`DELETE /v1/libraries/{id}`), all its assets get `deleted_at` set (soft-deleted). Trashed assets are excluded from search sync sweeps.
- If a proxy or thumbnail key points to a missing file, the endpoint returns 404 and clears the stale key from the asset record.
- On empty-trash (hard delete), DB rows are removed in FK-safe order, then (once committed) every file stored for those clips: previews, analysis proxies, scene frames, face crops, playback cuts. A re-detection that drops a face removes its crop the same way; a library deleted for good takes its previews folder (`storage/move.py` `forget_library`). The daily cleanup sweeps whatever is left (`search/cleanup.py`, face crops included).

**Face detection tables (ADR-009):**

```
faces             — face_id, asset_id, bounding_box_json,
                    embedding_vector vector(512), detection_confidence,
                    detection_model, detection_model_version, embedding_model,
                    crop_key, frame_ms, seen_ms int[], created_at
people            — person_id, display_name, created_by_user,
                    centroid_vector vector(512), confirmation_count,
                    representative_face_id, created_at
face_person_matches — face_id, person_id, confidence,
                    confirmed (bool), confirmed_at
```

Faces are found by the scheduler with InsightFace (buffalo_l), in a process of its own: photos by the faces producer, in their proxies (`POST /v1/assets/batch-faces`), and videos by the video faces producer (`src/producers/video_faces`). Each has a level (`src/producers/faces/levels.py`; Settings → Processing → Faces: Photos and Videos, Off / Low / Normal / High). A photo's says which faces count (Low only clear ones, Normal what was always found, High smaller and softer ones too: the gates on confidence, size and sharpness); Off finds none, in new clips or old, through the producer's `applies` (as a library's Infer location off does), so nothing is missing or waits, and found faces and names stay. A video is looked at in frames from its analysis proxy, chosen by its level (Low, its default: each scene's picture; Normal: three a scene; High: those and a frame every 3 s); the faces found are grouped into the people seen (cosine distance under 0.4 to a person's mean, never two faces of one frame to one person), and each person is saved as one face (`POST /v1/video/{id}/faces`): the face shown, the frame it's from (`frame_ms`), every time they were seen (`seen_ms`) and its crop, cut from that frame. The `assets.face_count` column tracks detection status (NULL = unprocessed, 0 = no faces, N = N faces; a video's, the people seen). A photo's face crops (128x128 WebP) are generated server-side from the asset proxy on submission.

Person clustering uses cosine distance on ArcFace embeddings (threshold 0.55) with a materialized cache in `system_metadata` (dirty flag invalidation). The People page shows clusters for naming. When a user names a cluster, all faces in it are assigned to a person. New faces are auto-assigned to known people on ingest if centroid distance < 0.45. The upkeep sweep periodically propagates assignments for untagged faces.

**pgvector:**

The `pgvector` extension is enabled on the Postgres instance at provisioning time. Enables `vector` column type and approximate nearest-neighbor index (`hnsw`) for similarity queries. Used for asset embeddings (CLIP ViT-B-32), video scenes' pictures' CLIP vectors (`video_scenes.likeness`, the same model), face embeddings (ArcFace via InsightFace), and face-to-face similarity for person clustering.

### 3.3 API Server

Python + FastAPI + SQLModel. Stateless. Runs on the install's machine behind nginx: a Mac's one Homebrew service (`lumiverb-server run`) or a Linux install's systemd units (section 11).

Responsibilities:
- Authenticate requests via API key or JWT → route to tenant DB
- Security headers middleware (CSP, X-Frame-Options, X-Content-Type-Options)
- Text that isn't text anywhere a request sends it (a NUL character, a lone surrogate, a URL's bytes that aren't UTF-8) is a 400 `not_text`, never the database's 500: the URL before sign-in, a JSON or form body after it, a file's bytes never (`src/server/api/no_nul.py`)
- Rate limiting on auth endpoints (in-memory sliding window per IP)
- JWT token lifecycle: 1-hour access tokens with 7-day refresh window, server-side revocation via `revoked_tokens` table
- Library and asset CRUD
- User management (email/password auth, JWT sessions)
- Atomic ingest (proxy + metadata in one request)
- File serving (thumbnails, proxies, video previews — never source files), through the Store (section 3.7)
- Search (PostgreSQL full-text search in the account's database, ADR-019)
- Similarity search endpoint (pgvector nearest-neighbor on CLIP embeddings, person-aware reranking)
- People and face management (clustering, assignment, merge)
- Video chunk coordination (scene segmentation metadata)
- Search sync (each clip's search row and each transcript's segments, written on a save and by a timestamp-based sweep)
- Upkeep (search sync, face propagation, orphaned file cleanup, revoked token cleanup)

### 3.4 The CLI and the Mac app

API clients. The CLI (`src/client/cli/`) scans libraries (`lumiverb scan`: proxies, thumbnails, EXIF, previews, uploaded through `POST /v1/ingest`), browses, searches and administers. It doesn't process: `lumiverb enrich` asks the scheduler to do a producer's work now (`POST /v1/producers/run`). A scan (the CLI's or the scheduler's) says what it found and the server decides what's new, changed, moved, skipped and missing (ADR-018 decision 12): `src/processing/scan.py` walks, lists the files to a scan session on the server, hashes what the plan asks about and uploads what it's told to; `src/server/scans.py` compares, one statement over the whole listing. The macOS app browses: it scans nothing and processes nothing (its built-in AI retired in ADR-016 phase 2, its own scan after it: the scheduler scans every library and makes every artifact). Neither has direct Postgres or object storage access.

The processing both the scheduler and the CLI's scan use lives in `src/processing/`: the API client (`api.py`), this machine's settings (`machine.py`: library root map, caches, how video is rendered), a library's originals (`originals.py`: whether they can be read now, their walk, and each file for the block that reads it; nothing else finds one), scanning (`scan.py`, `ingest.py`), the account's AI machines (`ai_pool.py`, `job_guard.py` and its guards), failure reports, the producers' settings as the server says them, and the media tools (`video/`, `proxy/`, `workers/`). Nothing there imports the CLI or the server, and nothing in `src/server` imports `src/client`.

Images are opened with libvips through `src/processing/libvips.py` only. A file is opened by its exact name, never with load options read from it (libvips reads a name ending in `[...]` as options), and only with the loaders libvips trusts: importing the module blocks the rest (ImageMagick, PDF, SVG, libraw, JPEG XL and others) in that process, on libvips 8.13 or later. An older one is refused: importing the module raises `TooOld` (an `ImportError` naming the version found and the one needed), at every try. Nothing imports it as a process starts; the first scan does, so every scan fails with it (the scheduler's as that library's scan failing, which Settings › Processing shows; `lumiverb scan` says it), while the API and the work from stored previews go on. `deploy-api.sh` won't install processing with one (Ubuntu 22.04's is 8.12), and `update-api.sh` says so. libvips tells a format by a file's content, not its name, so it's what's in the file that decides. A library's JPEG, PNG, WebP and TIFF are read by libvips, a DNG rawpy can't read as the TIFF it is, RAW by rawpy and BMP by Pillow (libvips reads BMP only with ImageMagick). The API opens no image with libvips: it re-encodes previews with Pillow (`src/server/preview_image.py`).

### 3.5 Processing: producers and the scheduler

All processing runs on the server, in the scheduler (`src/server/scheduler/`, systemd unit `lumiverb-scheduler`, beside the API, sharing its database). It ranks every job of every account (tier, then oldest first), fills each pool's free slots, and saves results through the API's own routes with a per-account key it makes at start. What's due comes from the reconciler (`repository/lineage.py`): missing, made from a file that changed, or (tier 4) made with another producer version or settings than now.

**Producers.** Each kind of derived artifact has one producer, named by what it makes (ADR-024: `lumiverb.image.description`; an installed plug-in's work by its plug-in's id), one folder each: `src/producers/<artifact>/`. Its `__init__.py` declares a `ProducerSpec` (`src/producers/contract.py`); its `work.py` holds its `Work`. Everything that lists producers reads the registry (`src/producers/__init__.py`): lineage and the reconciler, the scheduler's kinds, queue, pools and runners, the AI jobs and where each account keeps their models, the repair summary (what's missing, by name), what goes with an artifact when it's made again, pauses, and `GET /v1/producers` (Settings → Processing renders it as it comes). The list can change while the API and the scheduler run: an installed plug-in's work is a producer added then (ADR-023 phase 2: `src/server/plugin_producers.py` makes it from the plug-in's manifest, and its job, `src/producers/plugin_work.py`, hands the plug-in its clips' declared inputs over its socket and saves what it gives back through the API; `use_added`, which takes only a plug-in's work: no originals, nothing made at a scan, no AI job, a pool of its own). What's worked out from the list is worked out again when it changes (`src/producers/live.py`: lineage's rules and `lineage_join()`, what's missing of each, the scheduler's kinds), and the scheduler takes up a kind added at its next tick, with its runner and its pool's slots (`Scheduler._take_up_kinds`); one taken away is listed no more, and a job of it still running finishes. `tests/test_added_producers.py`.

**The runner.** A producer supplies `make(clip)` (the artifact, or raise `Failed` / `Waits` / `NotTried`; a `Waits` says why, `SCAN` when a scan comes first, else `HELD`, which Settings → Processing shows) and `save(client, made)`. Everything else is the runner's (`src/producers/runner.py`), the same for every producer: stopping (once the scheduler stops nothing more is made or saved, through a client that refuses), saving (right after making, or a job's clips together for a producer that sets `together`, or each part as a generator yields it), no NUL in what's sent, and whose an error is. The clip's (the API refused what was made: 400, 413, 422) is charged now and backs off (5 minutes, doubling up to a day, given up after 10 tries); the API or its database away (401, 403, 408, 429, 502-504, a transport error) charges nothing; anything else is a crash, uncharged but counted, and charged after `CRASHES_BEFORE_CHARGE` in a row; two clips crashing in a row leave the rest of the job waiting. An AI producer hands its failures to its job's guard, which charges a clip only when a machine checks out fine.

**Pools and AI jobs.** A spec names its `Pool` (`src/producers/pools.py` for today's shared ones): so many slots, or this machine's setting that sizes it (`sized_by`), or an AI job's machines (`job`: each account's own, sized by its online machines times the job's `per_request`), and whether its jobs hold back AI machines sharing the GPU (`gpu_hold`). An `AiJob` declares its label, its guard (how its machines are checked and called), its default model and whether the scheduler's own machine can do it; the account's model for it is kept under its name (`tenants.ai_job_models`).

**Models.** The GPU models are the scheduler's, not an account's (`scheduler/models.py`): CLIP loaded once per model and weights, and one face-detection process (ONNX Runtime leaks, so it's replaced after so many photos), shared by every account and let go of when unused. A photo it hasn't answered for in 300 s (`DETECT_TIMEOUT_SEC`, `src/producers/faces/detect.py`) is given up as a crash (counted, uncharged) and the process replaced.

**Pausing.** One switch per processing action (Upkeep, each producer), paused and resumed on its own, with a scope: `work` (nothing of it starts) or a producer's `redo` alone (its stale clips wait). Both are rows of `producer_pauses`, read by the scheduler every second on one path; a new producer version stops its redo until an admin resumes it, and so do new defaults a release brings (a prompt, say) when they change what the account makes it with (`lineage.hold_new_defaults`: remaking through a paid AI machine costs money, and nobody asked). Scans are each library's own (ADR-018 phase 1: `libraries.scans_paused_at`/`_by`, `lineage.pause_scans`): a paused library is left out of the scheduler's scan pass, and a scan of it can't start (409 `scans_paused`), whoever reads it; its reported changes wait, and everything else in it goes on. Pause all (the "all" row) pauses every switch and every library's scans; resuming one of them while it's on leaves the rest paused, each on its own (`lineage._end_pause_all`). Who reads a library's files is its `reader` (`src/shared/readers.py`: "server", this server, until devices come): the scheduler keeps only this server's libraries (`Account.set_libraries`), so a device's is never looked at, scanned or read here.

The rule: a pause stops its own work only. What other work depends on (whether a library's storage can be read, a new version's hold) is the scheduler's to find out, never a job's. Its refill looks at each library's storage itself every 30 seconds, and at once for a library not looked at since it started (`scheduler/account.py` `look_at_storage`, off the dispatching thread), the way the scan pass looks (`originals.reach` with a timeout; an empty folder is an unmounted share): work that reads the originals goes by the latest look, and storage a job found gone is back in use at the next. It looks for new producer versions and new defaults in every producer made again, paused or not, at each account's first refill and every minute (`service.py` `_find_new_versions`; listing a redo checks again). So pausing Scans stops only looking for new and changed files; the scan still looks at a library again right before it walks one. Pause all (its own `all` row, `lineage.Pauses.everything`) is for working on the hardware: nothing touches the library drives then, the storage look included, and on resuming the look comes before any storage work.

**No silent waits** (Robert, Oct 12). Whenever due work is held back, Settings → Processing says why on the producer's row. Every reason has a code in `src/shared/waits.py` (paused, the scheduler not running or starting or unable to list, the AI machines, what it's made from, a library's storage, a redo stopped by a new version or an admin, a scan coming first, a try that couldn't finish, a failure waiting its turn or given up); `GET /v1/producers` lists the ones holding some of each producer's work, with how many clips (`waits`, `src/server/producer_waits.py`), from the account's database and the scheduler's status; the web says each in a sentence (`src/ui/web/src/lib/waits.ts`). What only the scheduler knows it writes into its status: the clips it holds back after a try and why (a `Waits` names its reason, `SCAN` or `HELD`; the dispatcher keeps it with the clip's hold), the AI jobs whose machines it can't use, whether it has read the settings, and kinds whose listing failed. A new gate in the scheduler needs a code there and a gate in `tests/test_no_silent_waits.py`, which runs each through the real scheduler and database: closed, the clip isn't handed out and its row says why; opened, it's handed out and the reason is gone.

**Settings.** The scheduler reads this machine's way of processing from its environment (`src/server/scheduler/settings.py`: `LUMIVERB_ROOT_MAP`, `LUMIVERB_ANALYSIS_PROXY_*`, `LUMIVERB_GPU_DECODES`, `LUMIVERB_RENDER_CONCURRENCY`, `LUMIVERB_ANALYSIS_CACHE_GB` and `LUMIVERB_PREVIEW_CACHE_GB` (its caches' caps, 50 and 2: past them the least recently used go, never one a job holds; only for what the API doesn't keep on its disk), `LUMIVERB_FACE_BATCHES_PER_PROCESS`, `LUMIVERB_FULL_SCAN_EVERY_MIN` (each library scanned in full every 15 minutes unless set), `LUMIVERB_API_URL`; `/etc/lumiverb/env` on Linux), never the CLI's config.

**Video scene indexing** is saved as it's found: the scenes producer works out the video's scene window from its length and the account's Detail (`src/producers/scenes/detail.py`: `clamp(duration / target, min, max)`, evened to whole windows), initializes chunks of whole windows (`POST /v1/video/{asset_id}/chunks`), claims each (`.../chunks/next`), cuts it into scenes (one a window unless a real cut, whose picture holds, splits one; each scene's picture the sharpest of 5 keyframes spread across it) and completes it with its scenes, all through the runner's client. When the last chunk completes, the server merges neighbouring scenes alike (the discard pass: each scene's picture against its run's first, by an 8 × 8 wavelet hash, 64 bits, within 17 / 14 / 10 bits at Low / Normal / High; a cut is a keyframe 24 bits from the one before whose picture then holds within 12. `detail.py` has the measurements on 60 of Robert's videos behind them: the 16 × 16 pHash before merged 1 of 333 neighbouring scenes that were the same scene, and cut 93 times at hands and forks, none of them real) and only then marks the video `video_indexed`, so scene descriptions (one vision call a scene) only see the scenes kept. Found again (a redo), a scene whose picture is the frame of a scene the clip shows keeps that scene's description and its CLIP vector (`VideoSceneRepository.keep_descriptions`: a scene is described and embedded from its one frame), so only new frames are described and embedded; with every scene so, the new ones are shown at once, and the video's own description is written again unless they're the scenes it showed. People see the two producers as one, Scene descriptions, with one setting, Detail (Low / Normal / High); raising it redoes nothing by itself: the videos below it are counted, with Redo. Once every scene of a video is described, Video descriptions (`src/producers/video_description`) writes the video's own description and tags from them: the scenes' descriptions, in order with when each starts, sent as text to the same machines and model (`describe_text`) after the video's length, each without its sentences about mood (`work.py` `_plain`, for scenes described before scenes had their own prompt), the prompt after them: a gist, what the video is of, and 3 to 5 details to find it by (`VIDEO_PROMPT`, its reply's `gist` and `details` joined as the description by the caption client's `_parse_description`). Scenes are described with their own prompt (`SCENE_PROMPT`: a frame of a video, plainly, signs copied, a place named only when written in it, a dark frame said in a few words), not the photos'; both prompts came from a study of 70 of Robert's videos (Oct 29), where the description had been a list of the scenes, a sentence each, and the video's names a place only when a scene quotes it, never what isn't there (Qwen and Claude Haiku compared on those videos). The scenes' settings are fixed but for their longest answer (`ai_settings(room_fixed=False)`, its default 1,000 as the photos' and the text read's since v0.1.156, when at 500 a fifth of Claude Haiku 5.5's photo descriptions were cut off and asked again): a model that thinks first spends it thinking, and a reply cut off at it with nothing in it is the machine's settings at fault (`captions/base.py` `OutOfRoomError`, an endpoint fault: vision work waits, saying to raise it), one cut off after some of an answer the clip's. A long video's scenes are shortened to about 2,000 tokens (`work.py` `MOST_CHARS`: without their tags, then each scene's first sentence, then evenly spaced scenes, the first and last always), so they, the prompt and the answer fit in the 4,096 tokens Ollama gives a model by default: a machine cuts a request longer than its context from the start, and the model, its prompt gone, answers something else. It's kept where a photo's is (`asset_metadata`, saved by `batch-vision` under its own producer), so the clip shows it, search finds it and a person corrects it the same way; its scenes described again describe it again (`redo_also`).

**Scenes' CLIP vectors** (`src/producers/scene_likeness`, `lumiverb.video.scene_likeness`, "Visual search in videos"): each scene's picture, its frame at `rep_frame_ms` read from the analysis proxy, embedded by the CLIP job's machines with the model photos' vectors are made with (the built-in one, or an AI worker: ADR-022), so a scene is compared as a photo is. Its scenes are the describer's (`GET /v1/video/{id}/scenes?describing=true`: a redo's found scenes while it has them, else the ones shown), and its `made` reads the same set, so a video whose shown scenes never had vectors isn't handed out again and again while a redo's scenes wait to be described. Saved 50 scenes a request (`POST /v1/video/{id}/scene-likeness`, a long video's progress kept), each vector in `video_scenes.likeness` with its model in `likeness_model`; a scene gone meanwhile is passed by; the video's lineage is recorded once every one of its scenes has its vector (`lineage.made_now`, the producer's own applies and made). It's in no `redo_also`: found again, a scene on a frame already embedded keeps its vector, and a video with a new frame is missing again by its `made` whatever its lineage says, while starting its lineage over would make a video whose vectors were all kept stale, and its redo would embed every scene again. A redo of it (made another way than now) embeds every scene again: a scene's vector says its model, not how it was made.

**Capture facts and location guesses** (ADR-017): the `lumiverb.capture_time` producer reads a clip's `taken_at` (the camera's wall clock, as if UTC), its zone (`taken_at_offset_min`) and its GPS accuracy again from the original, for clips scanned before the scan sent them (one exiftool a job, its own pool). The `lumiverb.location.guess` producer (CPU pool, only in the libraries where Infer location is on: each library's, `libraries.infer_location`) places a clip without GPS from the nearest fixes before and after it in its library (the file's GPS or a person's location, never a guess), with each device's clock corrected per day from the scenes it shares with other devices (CLIP pairs); the math is `src/producers/location/infer.py`, the reads and the write `repository/locations.py`. A fix that appears, moves or goes (a scan, the capture facts read again, a person setting or clearing one) has the guesses around it checked again, each kept unless a surer one is found or its fix is wrong now (ADR-017 "As built"). Turning a library's off removes its guesses (`routers/libraries.py`, `locations.stop_inferring`, asked first: 409 `guesses_exist`).

#### Adding a producer

A producer ships as one folder (ADR-016's test, `tests/test_producer_contract.py::test_a_producer_is_one_folder`):

1. `src/producers/<artifact>/__init__.py` sets `PRODUCER = ProducerSpec(...)`: `makes` (the name of what it makes, ADR-024: a `Data`, `lumiverb.` then words, under `lumiverb.image.` or `lumiverb.video.` when one kind of clip has it, with its fields and where it's stored) and `reads` (the names it's made from; `reads_others` for other clips'), `artifact` (the name of what it makes, `names.<NAME>`: also its lineage's producer, its job's kind and its work's list), `version` (bump when its output changes), `media`, `title`, `applies` and `made` (SQL on `active_assets a`, reading only what its reads and makes are stored in: `tests/test_data_names.py`), its output-affecting `settings`, `redo_also` (what goes with it when it's made again), `plugin` (the plug-in it belongs to, ADR-021: it applies only while that's on in the account), and to be scheduled `run` (`"src.producers.<artifact>.work:<Class>"`), `tier`, `pool`, `batch`, `storage` (it reads the originals: its reads say `lumiverb.original`) and `unit` (`clip` or `second`). What it waits for (`needs`, the artifacts its reads are made by) is worked out from its reads, never declared. It imports only `contract.py`, `prompts.py`, `pools.py` and `names.py`; then `uv run python -m src.producers --write` writes `docs/data-names.md` again (a test fails until it's what the code says).
2. `src/producers/<artifact>/work.py` defines the `Work`: `make(clip)` returns what `save` sends (use `with self.inputs.original(clip) as source:` for the file on storage, only in a producer with `storage=True` (`inputs.originals(clips)` for many at once), `self.inputs.preview_bytes(clip)` and `with self.inputs.analysis_proxy(clip) as path:` for its preview and analysis proxy (`src/server/scheduler/inputs.py`: the Store by key beside the API, else this machine's caches; storage that can't be reached waits), `self.lineage(clip, used=...)` for what a save records, and `self.scratch` / `self.scratch_folder()` for any work file: a frame, audio, a render; never a temporary file of its own; a file to upload goes to the client as its path, opened for the request: no producer opens a file, `tests/test_producers_hold_no_files.py`), and `save(client, made)` posts it through the given client. The job's scratch goes when the job ends, however it ends, and at the scheduler's start; a file in it that outlives one clip's `make` (a frame saved after) is let go of in `done`.
3. A server route that takes the artifact, with `require_lineage(body.lineage, "<artifact>")`, records it with `lineage.record(...)`.

Whose an error is, as the runner judges it:

- An API error raised inside `make()` (a proxy fetched from the server, say) is judged by `whose()`, like a save's: refused (400, 413, 422) is the clip's; the API away is nobody's; anything else is a crash.
- `OSError` from `make()` is this machine's (no ffmpeg or ffprobe, a disk or mount error): a crash, counted, never the clip's. That holds for probe, CLIP, transcripts and OCR; only output that can't be read (ffprobe refusing the file, a proxy PIL can't open) is the clip's.
- Scenes with no analysis proxy in this machine's cache wait for it (so do their descriptions and vectors); a transcript with none is charged.
- A save that fails with anything but an API error (the cache's disk full) is a crash, counted.
- A scene whose frame can't be read, or that the model describes as nothing (or whose picture the CLIP machine can't read), is its video's failure, charged once the video's other scenes are saved.

Nothing else is edited: the kinds and their redo kind, the queue, the pool (declare a new `Pool` in the folder, or name a shared one), an AI job (declare an `AiJob` with its guard in the folder), the account's model storage, the page filter and summary count, Settings → Processing, pauses and failures all follow. A producer that reads the originals (EXIF, say) sets `storage=True` and asks `self.inputs.original(clip)` (no other may): jobs then go only to libraries whose storage was reachable at the scheduler's latest look, and the file missing or the storage gone is handled for it.

**Search sync:**
Search sync is timestamp-based: a clip's `search_synced_at` and `transcript_synced_at` say when its search row and transcript segments were last written. A save writes them (`try_sync_asset`, `sync_clips` after a batch, `index_transcript_segments`); the `POST /v1/upkeep/search-sync` sweep writes any still stale. The CLI `lumiverb maintenance search-sync --all` runs it for the account (`--force`: everything again). Scenes need none: their words are computed with their rows.

### 3.6 Search (PostgreSQL)

Text search is PostgreSQL's full-text search in each account's database (ADR-019): a clip's own words (`asset_search`), its scenes' (`video_scenes.words`) and its transcript's segments (`transcript_segments`), each searched among the clips in sight, so trash, archive and restore need nothing from search. `src/server/search/text_search.py` is the only code that knows the engine (English stemming, prefixes, phrases, field weights); swapping in another (ParadeDB's BM25, say) would be a migration and that file. It's in the database it searches: a backup and restore covers it, and there's no service to run.

### 3.7 Stored files and caches (ADR-020)

Everything Lumiverb makes from a clip (its preview and thumbnail, a video's preview and analysis proxy, scene pictures, face crops) is kept in a `Store` by key (`src/server/storage/store.py`). Keys are `{tenant}/{library}/{kind}/{bucket}/...`, built only by the key builders there; where a key is kept is the provider's alone. Two failures are never confused: `NotFound` (the key has no file: the missing-file repair acts on it) and `Unavailable` (the storage can't be reached now, a drive unplugged: work waits and nothing is taken for missing; the API answers 503 `storage_unavailable`).

The one provider is the filesystem's (`files.py` `FileStore`): a write is never seen half done, and goes where its key goes when it's saved. Its folder is the previews folder: `DATA_DIR` until a server admin moves it (Settings › Storage), or a library's own (Library settings › Previews), recorded in `DATA_DIR/storage.json`; a move runs in the background and a key is read from either folder meanwhile (`storage/move.py`). `STORAGE_PROVIDER` names the provider, and config refuses any other.

A `Cache` is a Store with a cap (`cache.py` `FileCache`): least recently used first, never a file a job holds. The account's playback cuts (`LUMIVERB_PLAYBACK_CACHE_GB`) and, on a scheduler that doesn't run beside the API's disk, its analysis proxies and previews (`LUMIVERB_ANALYSIS_CACHE_GB`, `LUMIVERB_PREVIEW_CACHE_GB`). A job's own work files are its scratch (`Work.scratch`), gone when the job ends. Clients keep no copies they decide from: the CLI caches nothing, and the Mac app's caches are capped.

The API serves stored files through it (`src/server/api/streaming.py` `stream_stored`, with ranges), and writes and deletes through it; outside `src/server/storage/` nothing takes a stored file's path (`tests/test_store_only.py`). Every provider and cache is held to `tests/test_store_contract.py`.

### 3.8 Plug-ins (ADR-023, ADR-024)

A plug-in is a program of its own, in any language, run outside Lumiverb's processes. Its manifest (`lumiverb-plugin.json`, checked by `src/plugins/manifest.py` before any of its code runs) says its settings with their types and bounds, the inputs it reads, the outputs it gives back, what it downloads and whether it uses the internet. The server admin installs it on the machine (`lumiverb-server plugins install FOLDER`, copied read-only into `DATA_DIR/plugins/<id>/program/`), on or off by default; each account's admin turns it on or off, accepting what it asks for.

The plug-in runner (`src/server/plugin_runner.py`: a Mac's `run` child, Linux's `lumiverb-plugins.service`) starts each one that's on somewhere as a process of its own (`plugin_launch.py`: niced, each file it writes capped, no core files, a clean environment; its data folder's size watched), talks to it over its socket (`plugin_client.py`), watches its memory, and restarts it with back-off. A plug-in never gets a database or an API key: Lumiverb pushes it what it reads and keeps what it gives back.

Its work is a producer (`src/server/plugin_producers.py`, named by the plug-in's id): the scheduler hands it its clips' inputs (`src/producers/plugin_work.py`, one `POST /v1/work`), checks its outputs against the manifest and keeps them in the account's `plugin_outputs` with the inputs' hash, so a clip whose inputs change is due again. Its outputs are shown with the clip while it's on (the Lightbox's facts) and, those it marks searchable, found by search (`plugin_outputs.words`). A plug-in can also answer lookups (`src/server/plugin_lookups.py`: `places`, for Set location). Lumiverb's own plug-ins (`src/own_plugins/`: GeoNames) are installed by Lumiverb itself as migrating's last step (`src/server/own_plugins.py`), off by default. Lumiverb's own data and each plug-in's outputs have names (ADR-024: `lumiverb.*` for Lumiverb's, the plug-in's id for its own: `geonames.landmark`), listed in `docs/data-names.md`.

---

## 4. Data Flow

### 4.1 Ingest Flow

```
Local filesystem
    → CLI (or the scheduler) scans directory, discovers media files
    → POST /v1/libraries/{id}/scans, then /found (every file: path, size, mtime) and /plan:
      the server says what to upload, hash first, leave unchanged or skip
    → Hashes what the plan asks about → /hashes: changed, unchanged, moved, skipped
    → /finish: what wasn't found is archived as missing; then what's new or changed is uploaded
    → For each image:
        → Generate WebP proxy (2048px max) + thumbnail (512px) in memory
        → Extract EXIF metadata (camera, GPS, taken_at, duration)
        → Run vision AI (OpenAI-compatible) → description + tags
        → POST /v1/ingest (multipart: proxy + all metadata, atomic)
        → API normalizes proxy, generates thumbnail, stores everything
        → API writes the clip's search row (try_sync_asset)
        → Asset appears fully populated on first creation
    → For each video (stage 1):
        → Extract poster frame, generate proxy/thumbnail
        → Extract EXIF metadata
        → Generate 10-second preview MP4
        → POST /v1/ingest (atomic)
    → For each video (stage 2 — scene indexing):
        → POST /v1/video/{asset_id}/chunks (init 30-sec chunks)
        → Loop: claim chunk → segment scenes → extract rep frames → complete
        → When all chunks complete, server marks video_indexed=true
```

### 4.2 Search Sync Flow

```
On a save (a description, OCR, a correction, a note, a move, a transcript):
    → API writes the clip's asset_search row (try_sync_asset; sync_clips after a batch)
      and marks search_synced_at in the same transaction
    → A transcript's segments are rewritten with it (index_transcript_segments)

Periodic sweep (CLI or upkeep endpoint):
    → POST /v1/upkeep/search-sync
    → Server finds clips whose search_synced_at is stale, and transcripts whose transcript_synced_at is
    → Writes their rows a batch at a time by id, each marked synced only if nothing wrote the clip meanwhile
```

### 4.3 Search Flow

```
Client calls GET /search?q=...&library_id=...
    → API runs the text search in the account's database (text_search.py)
    → API applies the other filters to its candidates, ordered by relevance
    → Returns paginated asset list with thumbnails
```

### 4.4 Similarity Flow

```
Client calls GET /v1/similar?asset_id=...&library_id=...
    → API fetches a photo's CLIP embedding from asset_embeddings,
      or a video's scenes' (video_scenes.likeness, up to 12, first to last)
    → Compares them with photos' embeddings and videos' scenes' in one pass each
      (AssetEmbeddingRepository.find_alike; excluding self), a video by its
      scene most alike, whose start is the hit's at_ms
    → Applies optional scope filters (date range, media type, camera)
    → Returns ranked similar assets with similarity scores
```

---

## 5. Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| API server | Python 3.12, FastAPI, SQLModel | Familiar, Cursor-optimised, proven in PoC |
| Database | PostgreSQL 18 + pgvector | Per-tenant isolation, JSONB for metadata, vector similarity (phase 2) |
| Migrations | Alembic | Standard, works with SQLModel |
| Search | PostgreSQL full-text search | In the database it searches: one source of truth, no service to run (ADR-019) |
| Stored files | A `Store` by key: the filesystem (ADR-020) | One interface; another provider would be a provider and nothing else |
| AI inference | OpenAI-compatible (configurable) | Supports any vision model (e.g. qwen3-visioncaption-2b via LM Studio) |
| CLIP embeddings | open-clip-torch (ViT-B/32) | 512-dim vectors for similarity search |
| CLI | Python, Typer | Shares models with API server |
| Web UI | React, TypeScript, Tailwind | Required for media grid, virtualized scroll |
| Mac app | Swift / SwiftUI | A browse client in the menu bar (the server's scheduler scans) |
| Install | Homebrew service on a Mac (`lumiverb-server run`), systemd units on Linux | One machine runs the API, the scheduler, the plug-ins, PostgreSQL and nginx (ADR-016) |
| Plug-ins | Programs of their own, any language, over a socket (ADR-023) | Isolated from Lumiverb's processes |
| Auth | Hybrid: email/password + JWT (web), API keys (CLI/automation). 1h access tokens, 7d refresh window, server-side revocation. Rate-limited auth endpoints. | Self-hosted, no external auth service dependency |

---

## 6. Multi-Tenancy Model

### 6.1 Tenant Isolation

Each tenant has a dedicated Postgres database. The control plane routes each API request to the correct database using JWT claims or API key → tenant_id → connection_string lookup.

Benefits:
- GDPR export = `pg_dump tenant_db`
- GDPR delete = `DROP DATABASE tenant_db`
- Zero cross-tenant data leakage (architectural guarantee)
- Per-tenant backup, restore, and migration
- Schema migrations can be applied tenant-by-tenant

### 6.2 Tenant Provisioning

Admin-provisioned initially. Self-service signup added later (same underlying provisioning logic, adds registration endpoint + UI).

Provisioning steps:
1. Create record in control plane `tenants` table
2. Create tenant database
3. Run Alembic migrations against tenant database
4. Create initial API key
5. Return connection details

### 6.3 Multi-Region (Future)

Pattern: per-tenant region affinity. Tenant picks region at signup. Control plane routes to home region. No cross-region replication needed for tenant data — only the control plane DB needs multi-region replication (tiny).

Staged rollout:
- Now: single region (GCP us-central1)
- ~100 tenants: add second region as passive replica
- ~300 tenants: per-tenant region selection at signup
- ~500 tenants: multi-region HA as premium tier

---

## 7. Storage Economics

*The original plan's estimates for the cloud-hosted mode, which isn't built.*

### 7.1 Per-Asset Storage

| Asset type | Size | Notes |
|---|---|---|
| Thumbnail (JPEG 400px) | ~20 KB | Served in grid views |
| Proxy (JPEG 2048px) | ~200 KB | Served for AI inference and detail views |
| Postgres metadata | ~8 KB | Per asset including AI description |
| Search rows | ~1.5 KB | Per asset, in Postgres |
| **Total** | **~230 KB** | Proxies dominate at 87% |

### 7.2 At Scale

| Corpus size | Storage | Monthly cost (GCS) |
|---|---|---|
| 100K images | ~23 GB | ~$0.53 |
| 1M images | ~230 GB | ~$5.30 |
| 834 customers × 25K avg | ~4.8 TB | ~$110 |

### 7.3 Infrastructure at 834 Customers (~$10K MRR)

| Component | Monthly cost |
|---|---|
| Object storage (4.8 TB) | ~$110 |
| Cloud SQL (Postgres) | ~$100 |
| Cloud Run (API server) | ~$50 |
| Vision AI API (tenant-paid) | ~$100–200 |
| **Total** | **~$500–600** |
| **Net revenue** | **~$9,400–9,500** |

---

## 8. Redundancy and Backup

### 8.1 What Must Be Backed Up

Only Postgres is irreplaceable: people's work (names, corrections, ratings, notes, projects, trash, settings) is there. Everything else is made again from the originals:
- Previews, thumbnails and the rest of what's stored → the scheduler makes what's missing (the missing-file repair, the next scan)
- Search rows → the search sweep writes them again (`lumiverb maintenance search-sync --all --force`)
- AI descriptions and the rest of what producers make → remade by the scheduler (Settings › Processing)

### 8.2 Postgres Backups

`src/server/backups.py`: every database (the control plane and each account's) as `pg_dump` archives, a folder each with a manifest, in the backups folder (Settings › Backups). Nightly, started by the every-account upkeep when none has run since 03:00, kept 7 days plus 4 weekly; a Mac's service and a Linux update also back up before an upgrade's migrations (3 kept). `lumiverb-server restore` puts one back beside the live database, then swaps it in. It reads only the backup's own files: each manifest entry's file a plain name in the backup's folder, each account's id as Lumiverb makes them (`ten_` and a ULID: it names the database put back), checked before anything changes (`verify`, `_entry_problem`). Search is in the databases, so it comes back with them.

### 8.3 Stored Files

Not backed up: everything in the Store is made again from the originals (8.1). The originals are the user's own, never written by Lumiverb (principle 2), and backed up however they back up their files.

---

## 9. API Design Principles

*(Full API specification in separate document: `api-design.md`)*

- RESTful resource-oriented design
- All endpoints require authentication (`Authorization: Bearer {jwt_or_api_key}`)
- Tenant context derived from JWT claims or API key — never passed as parameter
- Pagination via cursor (not offset) for large collections
- Consistent error envelope: `{error: {code, message, details}}`
- OpenAPI spec generated from FastAPI route definitions
- Versioning via URL prefix: `/v1/...`
- File uploads via multipart form (proxies, thumbnails)
- File serving through the API from the Store (`stream_stored`, with ranges): never signed URLs to a storage service

---

## 10. Build Sequence

*The original plan's phases, kept as history. What's built since is in the ADRs (`docs/adr/`).*

### Phase 1: Foundation (CLI + API)
- Control plane + tenant provisioning
- Tenant database schema + Alembic migrations
- Core API endpoints (libraries, assets, ingest, search)
- Local agent CLI (ingest, repair, search, similar)
- Client-side vision AI integration
- Search sync + Quickwit integration (Quickwit since replaced by PostgreSQL's own search, ADR-019)
- Docker Compose for self-hosted deployment

### Phase 2: Search + Discovery
- Full-text search endpoint
- Similarity search endpoint
- Video scene indexing (CLI-driven, server-coordinated chunk allocation)
- Enhanced ingest: sharpness scoring, face detection
- CLI search commands

### Phase 3: Web UI
- React + TypeScript + Tailwind
- Virtualized media grid
- Search interface
- Asset detail view (with AI description, metadata)
- Library management

### Phase 4: Mac Agent

Superseded: the native macOS app is a browse client. The server's scheduler scans every library and makes every artifact (ADR-016, ADR-018); the app watches, scans and ingests nothing. The plan as it was:

- Native Mac app or Electron
- Background filesystem watching
- Menu bar status
- Library configuration UI
- Auto-ingest on file changes

### Phase 5: User Accounts (Done)
- Email + password auth with JWT sessions (see ADR: user-accounts.mdc)
- CLI bootstrap: `lumiverb create-user`
- Password reset via SMTP
- Operators add users via CLI; no public signup flow in v1
- Settings page with API key management (admin/editor)

---

## 11. Self-Hosted Deployment

One machine runs the whole install: the API, the scheduler, the plug-in runner, PostgreSQL (with pgvector) and nginx in front of the web app and the API.

- **A Mac:** Homebrew (`brew install lumiverb`), one service, `lumiverb-server run` (`src/server/lifecycle/`): it starts Lumiverb's own PostgreSQL and nginx, migrates, and supervises the rest; `brew upgrade` is picked up without a restart. The README says how.
- **Linux:** `scripts/deploy-api.sh` and `deploy-web.sh` set up systemd units and nginx; `scripts/update.sh` updates (`docs/linux-server.md`).

Configuration is the install's env file (`lumiverb-server init` writes a Mac's). No cloud dependencies. `docker-compose.yml` is only the development database.

---

## 12. What Was Proven in the PoC

The following algorithms and patterns are extracted from the PoC codebase (`media-search`) and reimplemented in the new architecture:

- Video scene segmentation and representative frame extraction
- Vision AI integration and analysis pipeline
- BM25 similarity search with adaptive threshold
- Quickwit schema and index management (since replaced by PostgreSQL's own search, ADR-019)
- EXIF extraction, sharpness scoring, face detection
- Proxy format: WebP (compact, widely supported)

The PoC codebase is frozen as a reference. The new codebase is a clean start — no migration path, no backward compatibility burden.

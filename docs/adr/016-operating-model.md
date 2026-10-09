# ADR-016: Operating Model

## Status

Accepted — Phase 1 built, in review

This ADR sets the direction for Lumiverb. Where another doc in this repo disagrees with it, this ADR wins and that doc is out of date. The working version, with discussion and the decision log, is the "Lumiverb Operating Model" doc (https://claude.ai/artifact/UuLm2upRoZKRbrvR59SvqM).

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Groundwork and safety: this ADR, doc updates, stop overwriting human data, safe scanning | Built, in review |
| 1 | Projects and send to editor (v1) | Built, in review, with project trash. Left: real editor imports on a Mac; the macOS and iOS rename |
| 2 | The brain: one machine schedules all processing and reads storage read-only | Built; installed on the brain and ingesting. In review (PR #9). Left: the macOS app's change reports and retiring its AI (Swift), finishing the fresh ingest |
| 3 | Lineage and reconciliation | In progress: lineage and the reconciler built (in review); the vision model is chosen in Settings → AI. Next: one producer per artifact (OCR apart from descriptions) |
| 4 | Producers and endpoints | Not started |

## Overview

Lumiverb is the step between "I shot 400 clips" and "the right clips are in my editor." Its job is find, collect, send: search and enrichment exist so footage can be found fast, and the hand-off to Resolve, Premiere or Final Cut is where that work pays off.

Two principles shape everything else. Lumiverb never owns originals: it reads media where it lives and owns only what it derives and what the user tells it. Everything Lumiverb derives is rebuildable: proxies, thumbnails, transcripts, descriptions and embeddings are a cache of the footage, not a record of it.

Today processing happens wherever a client happens to run (the Python CLI or the macOS app), several artifacts have two producers that overwrite each other, nothing records how a derived file was made, and human data is overwritten in five places. This ADR replaces that with one machine (the brain) that schedules all processing, one producer per artifact, lineage on every derived artifact, and reconciliation instead of manual enqueueing.

## Motivation

- **Human data gets lost.** Re-running face detection deletes confirmed person assignments. Un-assigning a face leaves no record, so upkeep re-assigns it within 5 minutes. Dismissed people are forgotten after re-detection. A rescan un-trashes files the user trashed. Manual transcripts get overwritten. (Code references below.)
- **Two producers, last write wins.** Faces, CLIP embeddings and transcripts are each produced by both the Python CLI and the macOS app, with different runtimes or models. Whichever ran last owns the value. Mac-only models (Apple Vision, FeaturePrint) can't run anywhere else, which matters most for embeddings that must be uniform.
- **Nothing knows what is stale.** Only vision, CLIP and face detection record a model. Proxies, previews, scenes, transcripts and OCR record nothing, and no settings are recorded anywhere. After a model or setting change there is no way to find what needs rebuilding.
- **Processing depends on which machine is awake.** Enrichment runs only while a client is running on a machine that can read the files, so footage on a sleeping Mac waits.
- **The hand-off doesn't exist.** There is no way to get a set of clips into an editor, and video "proxies" are one still frame plus a 10-second preview, not full length.

## Design

### Principles

These are the rules other docs defer to.

1. **Originals are read-only.** Lumiverb reads originals in place. It never writes, moves or renames them, and never uploads them to the API or object storage. Only proxies and derived data are stored.
2. **Three kinds of data.** Every piece of data is exactly one of these:

   | Kind | Examples | Owner | Regenerable? |
   |---|---|---|---|
   | Originals | Camera video, audio, stills | Storage | No. Lumiverb only reads them. |
   | Human data | Projects, labels, person names, corrections, trash, path filters, settings | The user | No. Never overwritten automatically. |
   | Derived data | Probes, proxies, thumbnails, embeddings, transcripts, descriptions, faces | Lumiverb | Yes, at any time. |

3. **Human data is never overwritten automatically.** A correction to a derived value (naming a face cluster, fixing a description, un-assigning a face) is human data layered over the derived value. When the derived value is regenerated, the correction still wins.
4. **Derived data is a cache with lineage.** Every derived artifact is a function of four inputs: the original (its SHA-256), the producer, the producer's version, and the settings that affect its output. Each artifact records that lineage. If any input changes, the artifact is stale and can be rebuilt without anyone deciding what the right value was.
5. **Desired-state reconciliation.** The system compares what should exist with what does and sorts every artifact into missing, stale or current. Missing work runs right away at high priority. Stale work is flagged and upgraded in the background when the user approves. Retries, failures, partial runs and empty outputs collapse into one idea: an artifact that isn't current yet.
6. **One producer per artifact kind.** A producer declares the milestone it needs (never another producer by name), which assets it applies to (a condition such as "only video"), which of its settings change its output, and which scarce resources it uses. AI producers also name a model and an endpoint. Producers are first-party for now.
7. **Milestones, not a chain of workers.** Discovered (the asset exists and has an identity) → Probed (technical facts known) → Rendered (proxies and thumbnails exist) → Enriched (AI outputs exist) → Indexed (searchable). A producer whose prerequisites can never exist is disabled with a stated reason instead of failing on every asset.
8. **Paths are resolved, not stored.** The catalog stores a library root and each asset's path relative to it, Unicode-normalized to NFC. A full path is built when needed, for whoever needs it: the brain's mount, an edit station's mount, or an export prefix. No machine-specific absolute path is treated as truth.
9. **Surprising outcomes need explicit agreement.** When an action may do something the user doesn't expect (re-embedding a whole library, replacing edited values), the prompt spells out the outcome and the confirm button names the action ("Re-embed all 12,408 assets"), not a default Yes. **The API requires that agreement, not just the UI:** the request must carry the user's choice, or it's refused (409, with the facts), so every client (web, CLI, macOS, iOS) has to show the facts and ask. Moving an asset or a project to the trash is reversible and needs none.
10. **Deleting goes through the trash.** Assets and projects follow one pattern: delete → trash → restore or delete forever. Libraries have a trash but no restore yet. Deleting for good is a separate, explicit step, and it says what else it touches (the projects a clip is in) before it runs.

### Roles

Lumiverb has three roles. They can be separate machines or share one.

| Role | Does | Today |
|---|---|---|
| Storage | Holds the originals. Lumiverb reads from it and never writes to it. | A Mac Studio's NVMe DAS |
| Brain | Runs the catalog (API, Postgres, Quickwit), schedules all producers, holds derived media, serves the web UI. Reaches storage over the network, read-only. | A Linux box with an NVIDIA GPU |
| Edit stations | Where cutting happens, and where the web UI is used to find and collect. | The Mac Studio (masters), a MacBook (proxies) |

The macOS app keeps browsing. Its built-in AI (Apple Vision faces and OCR, CoreML ArcFace, FeaturePrint, whisper.cpp) retires in phase 2. Because the brain can't watch a network mount for changes, the macOS app reports the file changes it sees on storage.

### AI inference runs at named endpoints

Where inference runs is configuration, not architecture.

- A named endpoint has a URL, an optional key and a concurrency limit. Any API URL can be used, including Ollama on a Mac. One endpoint can pool several machines serving the same model.
- Each AI producer picks a model and an endpoint, optionally per media type. Local GPU and encoder slots are scheduled the same way.
- Lineage records the model, not the endpoint, so moving work between machines makes nothing stale. Changing an endpoint or a concurrency limit is not an output-affecting change.
- No model may be platform-specific: no Apple Vision, CoreML or FeaturePrint.
- Embedding models (CLIP, faces) must be uniform: one per library, served from any number of endpoints. Descriptions, OCR and transcripts can vary by media type.

### Projects and collections

These are two different things, and the names are reserved:

- A **project** is transient and many-to-many: a named, unordered set of whole assets for one job, such as "Customer Video 123". Create it, find media, send it to the editor, relink if needed, then archive or delete it. Archive means done but kept: it leaves the lists and pickers and still opens and exports. Delete moves it to the trash; restoring it can bring back its trashed clips too, and deleting it for good never touches its clips. A clip can be in many projects. Static projects hold hand-picked clips; smart projects are saved searches. **What the code calls "collections" today are projects**, and phase 1 renames them everywhere (tables, API routes, CLI, web, macOS and iOS).
- A **collection** is a long-lived container where each piece of media lives once. It doesn't exist yet; the name is kept free for it.

### v1: find, collect, send

v1 gets whole clips into the editor. Find is today's search, similarity and enrichment. Collect is projects. Send exports a project as a bin of master clips: FCP7 XML (shown as "DaVinci Resolve / Premiere Pro") or FCPXML ("Final Cut Pro"), pointing at `file://{prefix}/{rel_path}`, where the prefix is the library's ingest root unless the export supplies another (a travel SSD). No per-machine path state is kept on the server, and proxies are never exported.

The editor owns ordering, in/out points, subclips, markers, timelines and camera clock correction. Lumiverb hands off file references and does not carry editorial decisions or fix capture metadata.

### Analysis proxies

DAM proxies are full-length analysis proxies (low resolution, with audio) for transcription, scene detection and vision. They are not edit proxies; the editor's proxies are managed outside Lumiverb.

### Upgrades and corrections (phase 3)

Only output-affecting changes (a model, a prompt, a setting in the lineage hash) start an upgrade:

1. "NNN assets were made with the old model or settings. Upgrade them now?" No means not now: they stay listed as stale, with an Upgrade action on the producer.
2. If any carry user edits: "M of them have your edits." **Keep my edits** (the default: regenerate underneath and re-apply them), **Replace my edits** (kept in history), or **Skip edited assets**.
3. A uniform model (CLIP, faces) can't be upgraded in part. The prompt says every asset will be re-embedded in the background, similarity and clustering use the old model until it finishes, and face names carry over. The confirm button names the action; Cancel keeps the current model.

Proposed (not yet decided): whole-value overrides (a description, OCR text) win and keep the regenerated value underneath; tag edits are stored as adds and removes on top of model output; corrections tied to a region (faces, transcript words, scenes) are anchored in media terms (a time range or bounding box), re-applied by overlap after regeneration, and each ends as re-applied, satisfied or needs review, never dropped silently. Faces get full anchoring first, including "not this person".

### What changes from today

| Today | Under this ADR |
|---|---|
| Processing runs on the machine that holds the files (CLI or macOS app) | The brain schedules all processing; storage is mounted read-only |
| macOS app enriches with Apple Vision, CoreML, FeaturePrint, whisper.cpp | Retired in phase 2; the app browses and reports file changes |
| Two producers each for faces, CLIP and transcripts | One producer per artifact |
| `lumiverb enrich` and `MISSING_CONDITIONS` decide what runs | A reconciler computes missing, stale and current |
| Library scanner | Discovery: gives each asset its identity. Not a producer |
| Workers: EXIF, proxies, video index, vision, CLIP, transcription, faces, search sync | Producers at Probed (EXIF), Rendered (proxies, video index), Enriched (vision, CLIP, transcription, faces) and Indexed (search sync) |
| Asset trash, path filters, person names | Human data |
| "Collections" | Projects |
| Tenant isolation, API keys, roles | Unchanged |

## Edge Cases

| Scenario | Behavior |
|---|---|
| User trashes an asset, then a rescan finds the file still on disk | Stays trashed. Trash is human data. |
| A file goes missing (unmounted share, moved), then comes back | Proposed: restored automatically. Missing-on-disk is a fact about storage, not a user decision, so it is stored apart from user trash. Today both use `deleted_at`, which is why ingest clears it. |
| Storage unmounted, or mounted but empty, during a scan | No deletions. A scan that would delete more than 5% of a library's assets and more than 50 files skips deletions and warns, as the macOS app does. |
| Same file name in NFD (macOS) and NFC (Linux) form | One asset. `rel_path` is NFC everywhere. |
| Face re-detection on an asset with confirmed faces | Confirmed assignments, "not this person" records and dismissals survive. |
| User un-assigns a face | A negative record keeps upkeep from re-assigning it to that person. |
| Re-transcription of a video with a manual transcript | The manual transcript stays. |
| Endpoint or concurrency limit changes | Nothing becomes stale. |
| CLIP or face model changes | The whole library is re-embedded after explicit agreement; there is no partial state. |
| Storage asleep (Mac Studio off) | Proposed: enrichment continues from analysis proxies; only discovery, probing and rendering wait. |
| Producer whose prerequisites can never exist | Disabled with a stated reason, not failing per asset. |

Known limitations after phase 0 (accepted for now; each is a follow-up):

| Limitation | Effect | Follow-up |
|---|---|---|
| A "not this person" on a face that re-detection doesn't find again is dropped with the face | If a later detection of the same face isn't paired, it can be auto-assigned to that person again | Narrow; revisit with region-anchored corrections (phase 3) |
| Re-detection pairs a face with its old self only within embedding distance 0.4 | The same face seen more than 0.4 apart becomes a second face next to the kept confirmed one (nothing is lost); identical twins closer than 0.4 can still be paired | Tune the gate on real Apple Vision vs InsightFace data |
| Trash is tracked by path | A trashed file that is renamed or moved on disk comes back as a new asset | Use the stored SHA-256 to recognise it |
| The macOS scanner doesn't read the trashed list | It re-uploads trashed files and logs a 409 for each, every scan; no data changes | Fix in Swift, or moot once scanning moves to the brain (phase 2) |
| Assets marked missing have no listing or purge UI | They wait, with their human data, until the file returns or someone purges them by id | A "missing files" view |
| Two rows whose paths differ only in Unicode form are left as they are by the migration | The NFD one is marked missing on the next scan | Merge them by hand; the migration logs the count |
| `recreate-search-indexes` and forced `search-sync` accept any tenant key | They only rebuild derived data | Require admin with the other upkeep routes |

## Code References

Phase 0 targets, read from the repo on 2026-10-07:

| Area | File | Notes |
|---|---|---|
| Face re-detect wipes confirmations | `src/server/repository/tenant.py` `submit_faces` | Deletes all faces and their `face_person_matches`, confirmed or not |
| Un-assign leaves no record | `src/server/repository/tenant.py` `unassign_face` | Upkeep's propagation re-assigns within 5 minutes |
| Dismissals lost after re-detect | `src/server/repository/tenant.py` `cleanup_empty_dismissed`; `src/client/cli/repair.py` (`redetect-faces` calls `/v1/upkeep/cleanup-dismissed`) | Dismissed people with zero matches are deleted |
| Rescan un-trashes | `src/server/api/routers/ingest.py` (`existing.deleted_at = None`) | Scanner soft-deletes missing files with the same `deleted_at` as user trash (`src/client/cli/scan.py` `_detect_deletions`) |
| Manual transcripts overwritten | `src/server/api/routers/assets.py` `submit_transcript` | `source` is accepted but never stored |
| No mass-deletion guard in Python | `src/client/cli/scan.py` `run_scan` | macOS has one: `clients/lumiverb-app/Sources/macOS/Scan/ScanPipeline.swift` (5% / 50 files) |
| No NFC normalization in Python | `src/client/cli/ingest.py` (file discovery, `rel_path`) | macOS normalizes in `ScanPipeline.swift` |
| What runs today | `src/server/repository/tenant.py` `MISSING_CONDITIONS`; `src/client/cli/repair.py` | Replaced by the reconciler in phase 3 |
| Closest model for reconciliation | `src/server/upgrade/` | Upgrade-step registry |
| Projects (formerly "collections") | `src/server/api/routers/projects.py` | Smart evaluation caps at 1,000 assets with no `next_cursor` |

## Doc References

Updated alongside this ADR so they no longer contradict it:

- `docs/architecture.md` — principles and processing model
- `docs/adr/014-native-clients.md` — macOS enrichment and "inference stays client-side" superseded
- `docs/cursor-cli.md`, `.cursor/rules/cli.mdc` — where the CLI runs; source files are never uploaded
- `.cursor/rules/architecture.mdc` — this ADR wins on conflict; stale "Phase 1 scope" rules removed
- `CLAUDE.md` — topology
- `README.md` — privacy line

Each phase updates `docs/cursor-api.md` and `docs/cursor-cli.md` for the endpoints and commands it changes.

## Build Phases

### Requirements

Every phase follows the requirements in `docs/adr/000-template.md`: tests for every endpoint and repository method, the full suite passing, the web build clean, docs and this table updated, and read-ahead into later phases. Each phase ends at a gate that can be checked on the brain, and no phase needs anything from a later one.

### Phase 0 — Groundwork and safety

Make the repo agree with this ADR and stop losing human data before anything new is built.

**Deliverables:**
- This ADR and the doc updates listed above
- Fix the five human-data overwrites (Code References)
- Safe scanning from a network mount: port the macOS mass-deletion guard (skip deletions above 5% of assets and 50 files) to `src/client/cli/scan.py`; normalize `rel_path` to NFC in Python
- Land open work: the stale `/v1/browse` and `/v1/search` tests and the broken CLI `search` command; the `maintenance cleanup` 500

**Done when:**
- [ ] The test suite passes with no known failures
- [ ] Tests show that re-running faces or transcripts keeps confirmed assignments, un-assigns, dismissals, trash and manual edits
- [ ] A Python scan of an unmounted or empty root deletes nothing

### Phase 1 — Projects and send to editor (v1)

A project becomes a bin in Resolve, Premiere or Final Cut, with media online.

**Deliverables:**
- Rename "collections" to projects everywhere: tables, API routes, CLI, web, macOS and iOS UI, docs
- Project lifecycle: active, archived, deleted. Archived projects leave the sidebar and pickers but keep their clips and can still be exported
- Probe video and audio with one `ffprobe -show_streams -show_format` pass at ingest; store duration, frame rate (including drop-frame), start timecode, codec, rotation-aware dimensions, audio channels and sample rate in typed facets; backfill existing assets
- Evaluate smart projects with real pagination: fix the 1,000-asset cap, the `taken_at` cursor (it encodes `added_at`), and rating filters that differ per viewer
- Export providers for FCP7 XML and FCPXML, registered by id
- An export endpoint writing `file://{prefix}/{rel_path}`; a per-user default format that starts empty
- A split Export button on the project page (first use opens the format chooser with "Make default") and a CLI export command

**Done when:** one project mixing two cameras and frame rates imports into Resolve, Premiere and Final Cut with every clip online at the right duration; a smart project over 1,000 clips exports whole; an archived project can still be exported.

**Where it stands:** built, and the full suite passes with every marker. Tests show a smart project over 1,000 clips exporting whole and an archived project exporting. Both export files are read back by OpenTimelineIO's FCP7 and FCPXML adapters with every clip finding its media, but the real imports into Resolve, Premiere and Final Cut still need a check on a Mac. The macOS and iOS apps aren't renamed yet (they can't be built here); the API keeps `/v1/collections` and a legacy `collection_id` for them until they are. Browsing the result on a phone led to fixes for phone layouts and to project trash (principles 9 and 10): delete, trash, restore or delete forever, with the API requiring the user's say before surprising deletes and restores.

### Phase 2 — The brain

The brain reads storage, schedules all rendering and enrichment, and is where Lumiverb lives.

**Deliverables:**
- Install the production stack on the brain (`scripts/deploy-api.sh`, `scripts/deploy-web.sh`), reachable over the LAN and Tailscale
- Mount storage read-only; map each library's root to that mount in the brain's own config, not the database
- Render full-length analysis proxies at the Rendered milestone; transcription, scenes and vision read these instead of originals
- Run scan and enrich as a service on the brain (since phase 4, the scheduler); the storage machine reports file changes it sees
- Retire the macOS app's built-in AI steps
- Ingest the whole library fresh

**Done when:** footage copied to storage becomes searchable and transcribed without starting anything on an edit station, and enrichment continues from proxies while storage sleeps.

**Where it stands:** the code is built and checked on the dev stack. A clip copied into a mapped library and reported the way the macOS app will report it was transcribed and searchable 12 seconds later. With the storage unreachable, the worker scanned nothing, kept the report, and still transcribed and found scenes in a clip from its analysis proxy. When the storage came back, it scanned the reported folder. Production, dev and tests run Postgres 18, which Ubuntu 26.04 ships with pgvector. `docs/brain-setup.md` has the install, which needs sudo, and the Swift changes for the macOS app, which can't be built here. The brain is installed from this branch and ingesting its first library. Playback grew out of it: the web plays whole videos from their analysis proxies (a mix of every audio track first), public pages 10 seconds unless raised, and public pages show visitors only what the page itself does. Five independent review rounds; the last found nothing blocking.

### Phase 3 — Lineage and reconciliation

Every derived artifact says how it was made, and the system works out what is missing or stale.

**Deliverables:**
- A lineage record on every artifact kind (probe, proxy, thumbnail, preview, scenes, vision, OCR, CLIP, faces, transcripts, search documents): producer, producer version, output-affecting settings hash, source SHA-256
- A reconciler replacing `MISSING_CONDITIONS` and the `enrich` repair loop
- One producer per artifact; OCR stored apart from vision descriptions
- Human corrections kept beside the derived values they correct
- Per-producer counts of current, missing and stale, with the upgrade flow above

**Done when:** changing one producer setting marks exactly the artifacts it affects as stale, approving rebuilds them at low priority, no human data changes, and named faces keep their names through a face-model switch.

**Where it stands:** lineage is built. The registry (`src/shared/producers.py`) names one producer per artifact kind with its version and the settings that affect its output; the account's settings live on the server, and the worker reads them and makes artifacts with them. Every write records its lineage (`artifact_lineage`); a write that doesn't say (the macOS app today) counts as an unknown producer's, so it's stale. The migration fills lineage in from what existing artifacts already say. Settings that are a machine's way of working (the analysis proxy's encoder, like an endpoint) stay out of the hash, and a worker keeps no setting that changes output: the vision endpoint and model are chosen in Settings → AI, which lists what the endpoint offers. The reconciler hands the worker what's missing (judged by whether the artifact exists) and what was made from a file whose content has since changed; stale from a settings change waits for approval. Failures back off on the server (5 minutes, doubling to a day); a vision endpoint that can't be used pauses vision work without charging any clip, and Settings shows why. One producer per artifact (OCR stored apart from descriptions) and corrections followed. The producers page (Settings → Processing, `lumiverb producers`) shows each producer's current, missing, stale and failing counts and its settings; an admin upgrades what's stale, everywhere or in one library or project, answering the API's questions about clips with edits (keep, replace into history, skip) and, for CLIP and faces, confirming the count. An upgrade is the clips stale when it's approved, made after anything missing; a settings change before it finishes drops the rest. Not yet: making scenes, proxies and previews again in place, and keeping the old CLIP or face model searchable while an upgrade runs (CLIP and face models can't be changed from settings yet).

### Phase 4 — Producers and endpoints

Today's workers become producers that declare what they need.

**Deliverables:**
- The producer contract: milestone, applicability condition, output-affecting settings, scarce resources, and for AI producers a model and endpoint (optionally per media type)
- Settings declared on the producer class generate the settings page and its validation; output-affecting settings feed the lineage hash
- Named endpoints and local GPU and encoder slots, each with a concurrency limit; missing beats stale, and anything the user is waiting on beats background work
- CLIP, faces and Whisper move behind endpoints
- Producers live in versioned folders

**Done when:** a new producer, such as audio waveform thumbnails, ships as one folder with no pipeline edits, its settings page renders from the class, and a producer whose prerequisites can never exist shows as disabled with the reason.

**Orchestration (decided Oct 9):** the server runs all processing. A scheduler service on the brain (`src/server/scheduler/`, `lumiverb-scheduler.service`), beside the API and sharing its database, keeps one ranked queue of every job in every library and account: tiers first (1 see it: scans, previews, probes; 2 prepare: analysis copies; 3 find it: the AI and the rest that makes clips findable; 4 redo stale work), then oldest first. Each resource is a pool with so many slots (probes, renders, CLIP, the face process, scenes, and each account's AI machines); a free slot takes the best job that can use it, so a lower tier fills what a higher one leaves idle. It replaces the worker: distribution comes from storage and AI machines, not from workers on other machines. Producers become plug-ins it runs, with a public interface so outside producers can be added.

**Where it stands:** the scheduler is built (piece 1): the queue straight from each account's database (the reconciler's due clips), the dispatcher, scans from reported changes and a daily full scan (moved from the worker), and a runner per kind that reuses the worker's per-clip code and saves through the API's routes, with a key the scheduler makes itself. The update script replaces `lumiverb-worker` with it. Redo on change is built (piece 2): stale work is the scheduler's tier 4, a new model in Settings → AI asks once with the count and is the approval, admins can stop and resume a producer's redo, and the upgrade step (approvals, narrowing, the edits question, correction history) is gone. Next: failures you can see, sharing the brain's GPU, then the producer plug-ins.

## Alternatives Considered

**Keep processing on the machine that holds the files (ADR-014's model).** It needs that machine awake, gives faces, CLIP and transcripts two producers with different runtimes or models, and ties embeddings to platform-specific models. Rejected in favor of a brain that schedules everything and endpoints that can still run on a Mac.

**Keep explicit runs (`lumiverb enrich`, enqueue, retry-failed).** Missing-only checks have no notion of stale output after a model change, and failures, retries and empty outputs each need their own handling. Rejected in favor of reconciliation.

## What This Does NOT Include

Deferred until whole clips prove too coarse: in/out points and subclips, markers carrying scene or transcript moments, ordered projects and stringouts, nested bins, and round-trip from the editor.

Later, in any order: an "in use in N projects" counter, long-lived collections, merging related files (RAW+JPG, INSV pairs, sidecars), custom metadata fields, new media handlers (audio waveforms, 360° video), preview polish, MCP access, third-party producers.

Non-goals: correcting camera clocks or capture metadata; writing to, moving or renaming originals; editing inside Lumiverb.

## Open Questions

Proposed answers; each needs a decision before its phase.

| Question | Proposed answer | Phase |
|---|---|---|
| When does the old DAM (ResourceSpace) switch off? | After phase 2, once find, projects and send work on the brain against the whole library | 2 |
| Storage unreachable? | Keep enriching from analysis proxies; only discovery, probing and rendering wait | 2 |
| Re-sending a smart project? | A new bin each time, since the export is a file | 1 |
| Can a project span libraries? | Yes; the code already allows it | 1 |
| How do corrections survive output that changes shape? | Anchored in media terms and re-applied by overlap (see Upgrades and corrections) | 3 |
| Can an upgrade be narrowed to one library or project? | Yes | 3 |
| Third-party producers? | Not for now; first-party only | 4 |

# ADR-024 phase 1: Lumiverb's own data named (the plan)

## Status

Draft (2026-10-19), for the builder, the reviewer and the tester to agree before any code, and for Robert to make one decision (section 5). Robert's phase 1: "refactor builtins to align to the new naming schema". ADR-024's decisions 1, 2, 8 and 12 are his; this plan only says how.

## What phase 1 does, and doesn't

It names Lumiverb's own data in plain words, `lumiverb.*`, and makes every built-in producer declare by name what it reads and what it makes. It writes those names into a page generated from the producers. It maps protocol 1's inputs to them.

It doesn't change what a person sees, how any clip is made, or what any plug-in is handed. It doesn't rename our plug-ins' outputs (`geonames.town`, `example.*`: phase 2). It doesn't let plug-ins read each other's data, or check installs against the graph (phase 3), or add `has:` (phase 4).

## Where it starts

Each built-in producer is a `ProducerSpec` (`src/producers/<artifact>/__init__.py`), keyed by its artifact id. The id is everywhere: lineage, pauses, settings, the API, the scheduler's queue, the device and scan protocol, the web app. Its other ids don't match it in places: the `producer` recorded in lineage (`ffprobe`, `exiftool`, `preview`, `analysis-proxy`, `scene-detect`, `scene-vision`, `insightface`, `insightface-video`, `whisper`), the job `kind` (`render` for analysis proxies), the `flag` (`missing_video_scenes`, `missing_embeddings`, `missing_transcription`). What a producer is made from is `needs`, artifact ids. Protocol 1's inputs are four words (`src/plugins/manifest.py` `INPUTS`: `location`, `capture_time`, `metadata`, `preview`).

The `missing_*` flags aren't search filters any more: `GET /v1/filters/capabilities` lists none and the old asset page answers 410. They're the scheduler's work lists (`MISSING_FLAGS`) and `GET /v1/assets/repair-summary`'s fields. Phase 4's `has:` is where they'd become names.

## 1. The names

### The rules

- `lumiverb.` then words, dots between: each word lowercase letters, digits and underscores, starting with a letter (an output key's grammar, `manifest._KEY`). 
- A name that only one kind of clip has says which: `lumiverb.image.*` or `lumiverb.video.*`. One both have has no media level (ADR-024 decision 2). A test holds each producer's name to its `media`.
- Plain words first (decision 12): what a person would call it, the word Lumiverb's own screens use where there is one.
- One name for each thing a reader could want. A name can sit under another (`lumiverb.location.gps` under `lumiverb.location`): the longer is a part or a source of the shorter, and both are names.
- A name says what the data is, not who makes it: `lumiverb.video.transcript`, never `lumiverb.whisper`. The producer that makes it can change (another engine) and the name stays.

### The list

Each built-in producer makes one name. A few names are made by Lumiverb itself, not by a producer: the scan (what the file says), a person (a location they set, a person they named), or Lumiverb combining others (`lumiverb.location`).

| Name | What it is | Fields (version 1) | Made by | Today's id | Protocol 1 input |
|------|-----------|--------------------|---------|-----------|------------------|
| `lumiverb.original` | The file itself, in its library. Never handed to a plug-in (the API serves no originals) | a file | the library | — | — |
| `lumiverb.metadata` | Its media type, size, length, camera and lens | `media_type`, `width`, `height`, `duration_sec`, `camera_make`, `camera_model`, `lens` | the scan (a video's length, its probe) | — | `metadata` |
| `lumiverb.capture_time` | When it was taken, and its time zone's offset, as the file says | `utc`, `offset_min` | the capture producer (or the scan, which says so) | `capture` | `capture_time` |
| `lumiverb.preview` | Its preview image and thumbnail: a still, for a video too | an image (and its thumbnail) | the scan | `proxy` | `preview` (photos) |
| `lumiverb.location` | Where it was: a person's location, else the file's GPS, else an applied guess (never a suggestion), with how sure | `lat`, `lon`, `sure_m`, `source` (`person`, `file` or `guess`) | Lumiverb, from the three below | — | `location` |
| `lumiverb.location.person` | A location a person set: a point, a place, or the same as another clip's | `lat`, `lon`, `place` (its id, label and credit, when set from one) | a person | — | — |
| `lumiverb.location.gps` | The file's own GPS | `lat`, `lon`, `accuracy_m` (when the file says) | the scan (the capture producer reads the accuracy for older clips) | — | — |
| `lumiverb.location.guess` | Where it probably was, from the clips taken around the same time: applied, or a suggestion to take | `lat`, `lon`, `radius_m`, `applied` | the location producer | `location` | — |
| `lumiverb.video.format` | A video's container, codecs, size, rotation, frame rate, timecode and audio | `duration_sec`, `container`, `video_codec`, `width`, `height`, `rotation`, `frame_rate`, `start_timecode`, `drop_frame`, `audio_codec`, `audio_channels`, `audio_sample_rate` | the probe producer | `probe` | — |
| `lumiverb.video.preview` | Its short preview video (the first seconds): moving, where `lumiverb.preview` is a still | a video | the scan | `video_preview` | — |
| `lumiverb.video.analysis_proxy` | The copy of a video Lumiverb analyses (smaller; the browser plays it) | a video | the analysis proxy producer | `analysis_proxy` | — |
| `lumiverb.video.scenes` | Where its scenes start and end, each with its picture | a list of `start_ms`, `end_ms`, `picture` (an image) | the scenes producer | `scenes` | — |
| `lumiverb.video.scene_descriptions` | Each scene described, with tags | a list of `start_ms`, `end_ms`, `description`, `tags` | the scene descriptions producer | `scene_vision` | — |
| `lumiverb.video.transcript` | What's said in it, with times | `language`, and a list of `start_ms`, `end_ms`, `text` | the transcripts producer, or a person (a transcript they uploaded) | `transcript` | — |
| `lumiverb.video.faces` | The faces seen in it: one for each person seen, and when | a list of `box`, `frame_ms`, `seen_ms` (a list of times), `vector`, `crop` (an image) | the video faces producer | `video_faces` | — |
| `lumiverb.image.description` | A photo described, with tags | `description`, `tags` (a list of texts) | the descriptions producer, or a person | `vision` | — |
| `lumiverb.image.text` | The text read in a photo | `text` | the OCR producer, or a person | `ocr` | — |
| `lumiverb.image.likeness` | What visual search and "Find similar" compare: the photo's CLIP vector | `vector`, `model` | the visual search producer | `clip` | — |
| `lumiverb.image.faces` | The faces found in a photo | a list of `box`, `vector`, `crop` (an image) | the faces producer | `faces` | — |
| `lumiverb.people` | Who's in it, photo or video: the people its faces are named as | a list of `person` (their id and name), `confirmed` (a person said so, or Lumiverb matched them) | a person, or Lumiverb (a face matched to a person) | — | — |

### What a reader is handed is what the clip shows

A name hands what the clip shows now. Where a person wrote it, that's theirs: a description, tags, the text read, a transcript, a location. What a person writes is the one copy (`src/server/repository/corrections.py`), and machine output never replaces it. So "made by" says "or a person" where one can, and the page says which fields a person writes.

Who a face is belongs to `lumiverb.people`, not to the faces names. A face is found by a producer; who it is is a person's naming, or Lumiverb's match of it to someone already named. The faces names keep what was found: the box, the vector, the crop.

### Fields and versions

Every name is version 1. A name's version is its shape's: it goes up only when what a reader is handed changes shape (decision 7). It isn't the producer's version, which goes up when the producer makes something better and asks for a remake.

The fields above are version 1's, written now as the contract the page states. They're a draft until phase 3 first hands a name to a plug-in. Until then a field can still be corrected without a new version; from then on a change of shape is a new major version. Each field's type is in decision 7's words (text, number, point, time, list), plus `vector` and files (an image, a video). The page gives each field's type, and phase 3 checks them.

### Left for later, on purpose

What a person keeps about a clip that no producer makes and no reader needs yet isn't named in phase 1: its file name and path, its rating and favorite, its note, its projects, and whether it's archived or in the trash. A later phase names one when a reader needs it, by the same rules.

### The words that weren't obvious

- **`probe` → `lumiverb.video.format`.** What it keeps is the video's technical format (`video_facets`). The screens say "Video facts", but a fact is what a plug-in's output is (ADR-023), so "format" doesn't collide. Also considered: `lumiverb.video.technical`, `lumiverb.video.streams`.
- **`clip` → `lumiverb.image.likeness`.** Plain words for a CLIP vector: what visual search compares to find what looks alike. "Embedding" is jargon, and "clip" is the model's name and a word for a video. Also considered: `lumiverb.image.visual_signature` (it reads as a hash, and Lumiverb has content hashes), `lumiverb.image.visual_search` (the screen's title, but it names a use, not data).
- **`capture` → `lumiverb.capture_time`.** It also reads the GPS's accuracy, which is part of `lumiverb.location.gps`. Its name is what readers want of it.
- **`location` (the producer) → `lumiverb.location.guess`.** "Guess" is the word the Lightbox uses. A suggestion is a guess not applied yet; the page says so.
- **`proxy` → `lumiverb.preview`.** "Proxy" in Lumiverb means two things (the preview, and the analysis proxy). The screens call it the preview.
- **`thumbnail` and `scene_rep`** (upload types, not producers) are parts of `lumiverb.preview` and `lumiverb.video.scenes`, not names of their own.
- **`image`, not `photo`.** A photo is `image` everywhere a clip's media is said (`media_type`, the manifest's `media`), so the names say the same. The page says so, for a reader who'd look for `photo`.

The ADR's first list had no name for the original, a person's location, the people in a clip or `capture`. These are added. Its `lumiverb.image.clip_vector` and `lumiverb.video.probe` are worded as above, and `lumiverb.capture_time` is kept.

## 2. Each producer declares what it reads and makes

`ProducerSpec` gains:

- `makes`: a `Data` (`src/producers/contract.py`): its `name`, `about` (the page's "What it is"), its fields with their types, its `version`, and `stored`: the tables and columns it's kept in (`video_facets`; `assets.taken_at` and `assets.taken_at_offset_min`);
- `reads`: every name it's made from or chosen by, from its own clip: what its `applies`, `made` and `recheck` SQL read, and what its work is handed;
- `reads_others`: the names it reads of other clips. Only location guesses do: the times and locations of the clips taken around each one.

`needs` stops being declared. It's worked out from `reads`: each name read that a producer makes is a need, through its artifact. So the queue's waits, Processing's "N wait for Scenes" and `redo_also` behave exactly as now. A name read that the scan, a person or Lumiverb makes adds no need. It's there or not when the clip is listed, and `applies` says what a clip without it is: not one the producer applies to (scenes and transcripts without a length, location guesses without a capture time).

`part_of` and `redo_also` stay declared, not worked out from `reads`: they're choices, not data. Scenes remade remake their descriptions but not the faces in the video, though both read scenes. Worked out from `reads`, every scene remake would remake video faces too. Nothing else about a producer changes: its kind, tier, flag and pool stay as declared.

| Producer | Reads (its own clip) | Needs (worked out, as today) |
|----------|----------------------|------------------------------|
| probe | original | — |
| capture | original | — |
| proxy, video_preview | original (at the scan) | — |
| analysis_proxy | original | — |
| scenes | video.analysis_proxy, metadata (its length) | analysis_proxy |
| scene_vision | video.analysis_proxy, video.scenes | analysis_proxy, scenes |
| vision, ocr, clip, faces | preview | proxy |
| video_faces | video.analysis_proxy, video.scenes, metadata (its length) | analysis_proxy, scenes |
| transcript | video.analysis_proxy, metadata (its length) | analysis_proxy |
| location | capture_time, location.gps, location.person (a clip with either isn't guessed) | capture |

(`lumiverb.` left off.) Location guesses also read the clips around each one (`reads_others`): their `capture_time`, `location.gps` and `location.person`. That's why `lumiverb.location.person` is a name: a guess can't read `lumiverb.location`, which includes guesses (a circle). What a producer reads that isn't a clip's data isn't a name: its own settings (`producer.<artifact>`), its library's (Infer location), the clip's id, library and media type, and lineage.

Lumiverb's names no producer makes are one list beside the producers (`src/producers/names.py`), each a `Data` with who makes it. `lumiverb.location` and `lumiverb.people` say what they're made from too, so the graph has no gaps.

**Held by a test.** `reads` and `stored` are declared by hand, so a test holds them to the SQL that runs. It reads each producer's `applies`, `made` and `recheck`, and finds every table, and every column of the clip (`a.<column>`), they read. Each must be stored by a name the producer reads or makes, or be on the short list of what isn't data (above). A producer reading a column no name of its reads is stored in fails it, naming the column. What a producer's work is handed is its `Work.inputs` (`src/server/scheduler/inputs.py`: the preview, the analysis proxy, the original), and a second test holds those to `reads` the same way.

The registry check (`src/producers/__init__.py`) refuses what isn't so:
- a name not in the grammar, or not `lumiverb.*`, or under the wrong media;
- two producers making one name;
- a name read that nothing makes;
- a circle.

An installed plug-in's producer reads the names of its protocol 1 inputs (section 3). Its `needs` come out as now (`proxy` when it reads the preview). It makes nothing named in phase 1: its outputs are named in phase 2.

`GET /v1/producers` gains each producer's `makes` (its name), `reads` and `reads_others` (names), beside `artifact` and `needs`, which stay. `GET /v1/plugins`' `reads` stays the words a person reads on its card.

## 3. Protocol 1's inputs

Protocol 1 stays as it is. A manifest still says `"inputs": ["location"]`, and the plug-in is still handed `{"location": {"lat", "lon", "sure_m", "source"}}`. Each input stands for a name: `location` → `lumiverb.location`, `capture_time` → `lumiverb.capture_time`, `metadata` → `lumiverb.metadata`, `preview` → `lumiverb.preview`. That's one table in `src/plugins/manifest.py` beside `INPUTS`, and the page shows it.

Phase 3's `reads` takes the names, and a protocol 1 manifest's `inputs` are read as those names. A plug-in written for protocol 1 keeps working, as decision 2 and the ADR's design say.

For phase 3, written down now: the hash a plug-in's output is kept with (`clip_inputs.hash_sql`) labels each input with protocol 1's word (`'location'`, `'capture_time'`, `'metadata'`). Those labels stay. Hashing under the names would change every clip's hash and make every plug-in's output due again. The same for what an install accepted: `installed_plugins.permissions` keeps `{"inputs": [...]}` in protocol 1's words, which `manifest.asks_more` compares. Read as names, every update would ask for acceptance again. Reading inputs "as names" means mapping them where they're read, never rewriting either.

## 4. The page

`docs/data-names.md` lists every `lumiverb.*` name. It is written from the producers and `src/producers/names.py` by `uv run python -m src.producers.names --write`, never by hand. For each name it gives:
- what it is, its fields with their types, and its version;
- which fields a person can write;
- what clips have it (photos, videos, both);
- who makes it (the producer's title, the scan, a person, Lumiverb);
- what reads it (of its own clip, or of others);
- the protocol 1 input it stands for;
- its id in today's API (section 5).

A fast test fails when the page isn't what the code would write, and says the command. Another test fails when a producer is added without a name, or its name has no `about`.

The page is linked from cursor-api.md (the Plug-ins API, and "Writing a client"), from ADR-024, and from CLAUDE.md's producers row. Two lists kept by hand have drifted from the producers: cursor-api's job kinds (`GET /v1/producers/queue`) and the web's job labels (`ProcessingSection.tsx` `KIND_LABELS`) leave out `capture`, `location` and `video_faces`. Phase 1 adds them, and a test holds the doc's kinds to the scheduler's.

## 5. Robert's decision: rename what's stored and sent, or name beside it

Robert's phase 1 is "refactor builtins to align to the new naming schema". Read literally, that's (a): the built-ins renamed to the names. Read lightly, it's (b): the built-ins declared and documented under the names, which is what aligns them to the schema, and today's ids kept as storage keys underneath. Either way, sections 1 to 4 are built, and a client using today's ids keeps working. The question is whether today's ids go.

### (a) One name for each thing, everywhere

Today's artifact ids are renamed to the names wherever they're stored or sent.

- **Migrations.** One per account, renaming in place:
  - `artifact_lineage.artifact` (a row for each thing made for each clip);
  - `producer_pauses.target` (pauses, and stopped redos);
  - `producer_crashes.artifact`;
  - `system_metadata`'s `producer.<artifact>` settings and `producer.<artifact>.version_seen`;
  - `uploads.artifact` and its CHECK (`video_preview`, `analysis_proxy`);
  - `system_metadata`'s `artifacts.missing` (`asset_id:kind` entries: an unknown kind is skipped silently, so pending repairs of missing files would be lost);
  - the triggers on `producer_pauses` and on `producer.%` keys, and the partial index `ix_artifact_lineage_failing`, held to the new names;
  - an installed plug-in's work (`plugin_geonames` in lineage and pauses): left as it is in phase 1, its naming being phase 2's.

  The scheduler's learned paces (`scheduler.status`) are only cosmetic: relearned. Older migrations whose downgrades assume today's names would need checking too. A downgrade renames them back. Missing `version_seen` alone would stop every producer's remakes as "a new version" (`hold_new_version`). The producer ids in lineage (`ffprobe`, `whisper`, …) stay: they say who made it, which is a different thing, and renaming them would make every clip stale.
- **Code.**
  - Every place an id is written as text: the producers' own SQL (`'capture'`, `'location'`), the faces levels' `producer.faces`, 13 `require_lineage(…, "<artifact>")` calls, ingest's lineage form (`proxy`, `capture`, `probe`, `vision`, `clip`), the artifact and upload types, the reader-work and check-in lists, the device's order (`probe`, `capture`, `analysis_proxy`), and the web app's matches (`"location"`, `"faces"`, `"video_faces"`).
  - Ids are put into SQL as bind and column names (`src_<artifact>`, `:p_<artifact>`, `:remake_<artifact>`). A dotted name isn't one, so a second, safe spelling is needed inside the code anyway.
- **Clients.** What a client sends can be taken under either spelling. What the server sends can't be both:
  - A device on another machine running an older Lumiverb reads its work as `probe`, `capture` and `analysis_proxy`. Sent new names, it would do nothing, with nothing said, until it's upgraded.
  - The Mac and iPhone apps fetch `artifacts/proxy`, `/thumbnail` and `/video_preview`, and match a playback source of `analysis_proxy`.
  - A reader someone wrote for themselves (ADR-020 decision 9) sends lineage keyed by today's ids.
  - An older device's failures report would be refused whole (422) once `artifact` is checked against the new names.
  - An older CLI scanning from another machine sends ingest's lineage keyed `proxy`, `capture`, `probe`, `vision`, `clip`.
  - What people and scripts type: `lumiverb pause vision`, `lumiverb enrich faces`, `lumiverb producers settings transcript`, `lumiverb-server pause <producer>`.
  - The file kinds in URLs (`proxy`, `thumbnail`, `video_preview`, `analysis_proxy`, `scene_rep`) are tied to registry keys (`require_lineage`, `READER_ARTIFACTS`) and fetched by the apps, Resolve, the web, the CLI's downloads and the scan's caches.
  - A browser tab left open across the upgrade.

  So the server takes today's ids for good: we can't know when the last old client has gone. It also keeps sending today's ids where an older client reads them, or it needs clients to say which they speak and old ones to be upgraded. For devices and scanners that lever exists already: the check-in's protocol (`check_ins.PROTOCOL`, `OLDEST_PROTOCOL`, 426 `reader_too_old`), so it's a bump, not new work. The apps, Resolve, scripts and people typing have none.
- **Breaks, and for how long.** With aliases on everything sent to the server and today's ids kept in what it sends, nothing breaks. But then two spellings exist at the edge for as long as old clients do, and one inside the code for SQL. Without them, older devices stall silently until upgraded, the apps lose their previews until rebuilt, and other readers fail until they're changed.
- **Rolling back.** Older code can't migrate down: v0.1.102's `lumiverb-server migrate` on part 4's revisions says "Can't locate revision identified by 'd7f3b9e1c5a2'" and changes nothing (the tester tried). So a rollback past (a) means downgrading with the newer code before installing the older one, or restoring the backup taken before the upgrade, which loses what was made since.
- **Cost.** About three parts: storage and aliases; the clients (web, CLI, device, Swift); then taking out the literals. Most files under `src/server`, `src/processing` and `src/producers` are touched. It is a migration of every account's lineage. A missed rename shows as a mass remake or a silent hold.
- **Tests.**
  - The migration up and down on an account seeded with every table and key, `version_seen` included.
  - Every route under both spellings.
  - Today's device code (a copy pinned in the tests) against the new server.
  - The apps' artifact URLs.
  - The upload CHECK.
  - The suite with every literal gone (a test that no artifact id is written as text outside the registry).
  - A rollback: upgrade, work done, downgrade with the newer code, the older code installed. Nothing remade, pauses and settings kept, open uploads finished.
- **Left alone either way**, a different namespace: AI job names (`vision`, `transcripts`, `clip`, `faces` in `ai_job_models`, `/v1/ai/jobs`, `/v1/lumiverb/clip`), the CLIP `model_id` `clip` the apps send, search's `hit_type` `transcript`, storage keys and the env file's names.

### (b) Names beside today's ids

Today's ids stay what's stored and sent. They're documented as storage keys, kept for every client. The names are the public, documented layer: the page, `GET /v1/producers`' `makes` and `reads`, and everything new (phase 3's manifests, phase 4's `has:`). One table in the registry maps each to the other. It's held by a test: each producer has exactly one name and each name at most one id.

- **Migrations.** None: the PR adds no alembic revision. Rolling back to v0.1.104 is installing it.
- **Code.** `ProducerSpec.makes` and `reads`, `src/producers/names.py`, the protocol 1 table, the page and its writer, two fields on `GET /v1/producers`, and the registry check. About ten files besides tests and docs.
- **Clients.** Nothing they send or read changes. Nothing breaks.
- **Cost.** One part. Two identifiers for each built-in: the id, which people never see (Processing shows titles), and the name, which they read. The page puts them side by side. New API takes names; the existing API keeps its ids, as cursor-api says.
- **Tests.**
  - The rules (grammar, `lumiverb.*`, media level, one maker, every read made, no circle).
  - `needs` worked out from `reads` equal to today's for every producer.
  - The page current.
  - The protocol 1 table complete (every `INPUTS` key has a name, and each name is one a producer or Lumiverb makes).
  - A plug-in still handed exactly what it was.
  - `GET /v1/producers`' `makes` and `reads`.

### Recommendation: (b)

What Robert asked for is readable names and a contract to build on, not new storage. (a) touches every account's lineage and most of the server for no change anyone sees. Because old clients can't all be upgraded together, it still keeps today's ids at the edge, and needs a second spelling inside the code for SQL. So (a) ends with two names too, at far more cost and risk.

(b) gives every reader one name per thing now, in the places names are read: every producer declares its data by name, the names are the documented contract, and everything new speaks them. That is what aligning the built-ins to the schema has to deliver for phases 2 to 5.

If keeping two ids ever costs more than it saves, a later phase can retire the old ones on its own terms: the check-in's protocol bumped first, then the rename. Retiring them then costs about what (a) costs now: the migrations, the code and the aliases. It doesn't get cheaper by waiting, except that older clients will have gone by then.

## 6. What a person sees

Nothing changes on screen. The page `docs/data-names.md` is new, linked from the docs a plug-in author or client writer reads. `GET /v1/producers` has two more fields.

## 7. Tests

Every test calls the real registry, producers and routes (none reimplements a rule), and none asserts speed.

- The rules in section 2, each refusing a made-up producer that breaks it (the registry check, as `tests/test_producer_contract.py` does for producers' other rules).
- `reads` held to what runs: every table and clip column each producer's `applies`, `made` and `recheck` read is stored by a name it reads or makes (or is on the not-data list); every input its work is handed (`Work.inputs`) is a name it reads. Each fails on a made-up producer reading a column it doesn't declare.
- What a reader would be handed is what the clip shows: for `lumiverb.image.description`, `lumiverb.image.text` and `lumiverb.video.transcript`, a clip a person corrected states the person's value (a test on `stored` and `corrections.FIELDS`, so a field a person can write is on the page as theirs).
- Nothing else moves: the whole registry as the code works it out, every value written into the test from main before the change. Each producer's `needs` (now from `reads`), `part_of`, `redo_also`, tier, kind, flag and pool; the scheduler's `KINDS`, its `redo_` kinds too; the pause targets; `MISSING_FLAGS`; lineage's tables (`APPLIES`, `MADE`, `RECHECK`).
- The page: written by the command, equal to the file; and a change to a producer's name fails it.
- The protocol 1 table: each input's name, and `tests/test_plugin_work.py` unchanged (what a plug-in is handed).
- `GET /v1/producers`: each producer's `makes`, `reads` and `reads_others`; an installed plug-in's `reads` (its inputs' names) and its `makes` empty.
- No alembic revision in the PR (b).
- The tester's, in their acceptance file, at READY: every `GET /v1/producers` row equal to today's but for the new fields, on a seeded account with a plug-in (expected values written from main now); a protocol 1 plug-in's work request byte for byte as today's; end to end on throwaway installs, v0.1.104's CLI, `lumiverb device run` and web app against the phase 1 server, GeoNames' outputs not due after the upgrade, and a rollback to v0.1.104 on the same databases with nothing remade or lost; LumiverbKit's and Resolve's tests (on main: 338 Swift tests, 0 failures; Resolve 42 passed).
- Option (a), if Robert chooses it: section 5's list.

## 8. Parts

Under (b), one PR, with no alembic revision (rolling back to v0.1.104 is installing it): the names, the declarations, the page, the API's fields, their tests and docs (ADR-024's progress, CLAUDE.md's producers and plug-ins rows, cursor-api.md). Under (a), three, each released before the next, with section 5's rollback procedure in the release notes.

## 9. Risks

- **Names are hard to change once plug-ins read them** (phase 3). Phase 1's list is the one to get right: the reviewer, the tester and Robert read section 1 for words they'd look for and not find.
- **Two identifiers** (b): someone writes code against the wrong one. The page shows both side by side, cursor-api says which an API takes, and new API takes names only.
- **A name that hides two things** (`lumiverb.capture_time` also brings the GPS's accuracy; `lumiverb.preview` is two files). The page says what each holds.

## 10. Question for Robert (through the coordinator)

1. **Rename what's stored and sent, or name beside it?** His "refactor builtins to align to the new naming schema" read literally is (a); (b) is the lighter reading, and meets it by declaring and documenting every built-in under the names. (a) renames today's ids to the names everywhere. That's a migration of every account, most of the server, aliases kept for older devices, the apps and other readers, and three parts. (b) adds the names beside the ids. That's no migration, nothing broken, one part. Recommended: (b).

The names themselves (section 1) are for the reviewer and the tester to agree. Robert can change any word; the two that took the most thought are `lumiverb.video.format` (probe) and `lumiverb.image.likeness` (CLIP).

## Agreement

| Who | Says |
|-----|------|
| geonames-builder | Proposed |
| geonames-reviewer | |
| geonames-tester | |

# ADR-024 phase 1: Lumiverb's own data named (the plan)

## Status

Revised (2026-10-19) after Robert's decision (section 5): rename everything. Agreed again by the builder, the reviewer and the tester; then the coordinator's go for part 1. Part 1 done (v0.1.105, 2026-10-19); part 2 done (v0.1.106, 2026-10-19); part 3 done (v0.1.107, 2026-10-19); part 4 done (v0.1.108, 2026-10-19); part 5 done (v0.1.109, 2026-10-20): phase 1 is done. Robert's phase 1: "refactor builtins to align to the new naming schema". ADR-024's decisions 1, 2, 8 and 12 are his; this plan only says how.

## What phase 1 does, and doesn't

It names Lumiverb's own data in plain words, `lumiverb.*`, and makes every built-in producer declare by name what it reads and what it makes. It writes those names into a page generated from the producers, and maps protocol 1's inputs to them. Then it renames everything to those names: every identifier family Lumiverb stores or sends for its producers and their files (section 6), in five parts, each released and checked on Robert's Mac before the next (section 9).

It doesn't change what a person sees on screen, how any clip is made, or what any plug-in is handed. It doesn't rename our plug-ins' outputs (`geonames.town`, `example.*`: phase 2). It doesn't let plug-ins read each other's data, or check installs against the graph (phase 3), or add `has:` (phase 4).

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

| Name | What it is | Fields (version 1) | Made by | Renamed from | Protocol 1 input |
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

### Files

Each file a name has is named too: the name itself for its main file, and a part under it for another. These are the file kinds in URLs (`/v1/assets/{id}/artifacts/{kind}`), uploads, ingest's files and playback's `source` (section 6):

| File | Renamed from |
|------|--------------|
| `lumiverb.preview` | `proxy` |
| `lumiverb.preview.thumbnail` | `thumbnail` |
| `lumiverb.video.preview` | `video_preview` (playback's `preview`) |
| `lumiverb.video.analysis_proxy` | `analysis_proxy` |
| `lumiverb.video.scenes.picture` | `scene_rep` |

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

`part_of` and `redo_also` stay declared, not worked out from `reads`: they're choices, not data. Scenes remade remake their descriptions but not the faces in the video, though both read scenes. Worked out from `reads`, every scene remake would remake video faces too. Its `artifact` becomes its name, and `producer`, `kind` and `flag` go: a producer is named by what it makes, and its name stands for its job's kind and its work list (section 6). Its tier and pool stay as declared.

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

`GET /v1/producers` gains each producer's `reads` and `reads_others` (names) in part 3, when its `artifact`, `needs`, `part_of` and waits become names. `GET /v1/plugins`' `reads` stays the words a person reads on its card.

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
- what it was called before (renamed from), kept as the page's history.

A fast test fails when the page isn't what the code would write, and says the command. Another test fails when a producer is added without a name, or its name has no `about`.

The page is linked from cursor-api.md (the Plug-ins API, and "Writing a client"), from ADR-024, and from CLAUDE.md's producers row. Two lists kept by hand have drifted from the producers: cursor-api's job kinds (`GET /v1/producers/queue`) and the web's job labels (`ProcessingSection.tsx` `KIND_LABELS`) leave out `capture`, `location` and `video_faces`. Phase 1 adds them, and tests hold both to the scheduler's kinds: the doc's as the client contract's test reads it, the web's labels as `tests/test_no_silent_waits.py` holds `lib/waits.ts`.

## 5. Robert's decision: rename everything

Asked whether to rename what's stored and sent to the names (a), or keep today's ids as storage keys with the names beside them (b, recommended), Robert chose (a), 2026-10-19: "Rename everything. I don't want half-baked. We can split this up into multiple PRs to do the migration first, etc if that is easier."

So every identifier family is renamed to the names, or is a different thing and says why (section 6). The server ends up storing and sending only the names. What clients send under the old spellings is taken for a stated window that ends with part 5 (section 8).

The road not taken: (b) added the names beside the ids, with no migration and one part. It would have left two identifiers for each built-in for as long as the ids stayed.

## 6. Every identifier family

### Renamed

| Family | Where it's stored or sent | Renamed to | Part |
|--------|---------------------------|------------|------|
| **Artifact ids** (`probe`, `capture`, `proxy`, …) | The registry's keys; `artifact_lineage.artifact`; `producer_crashes.artifact`; `GET /v1/producers`' `artifact`, `needs`, `part_of` and waits; failures and retry; Run; the settings and redo paths; 409 details; the queue's `switches`, `eta` and `not_counted`; the CLI | The names (section 1) | 3 |
| **Lineage's producer** (`ffprobe`, `exiftool`, `preview`, `analysis-proxy`, `scene-detect`, `scene-vision`, `insightface`, `insightface-video`, `whisper`, `vision`, `ocr`, `clip`, `proxy`, `location`) | `artifact_lineage.producer`; the JSON lineage kept with scenes (`video_scenes.lineage`), uploads and the other tables that keep one; what every machine write sends (`lineage.producer`); `require_lineage` | The producer's name: a producer is named by what it makes. The migration renames every row and every JSON lineage in the same transaction as the artifacts, and the code's producer is the name at the same release, so no clip goes stale | 3 |
| **Job kinds** (`render`, `probe`, …, and `redo_<kind>`) | The scheduler's `KINDS`, the dispatcher, `scheduler.status`, `GET /v1/producers/queue` (`running`, `waiting`, `jobs`, `held`, `unlisted`, `pace`), the web's labels, cursor-api | A producer's kind is its name; its remakes' kind is `redo:<name>`. The scan's job stays `scan`: it isn't a producer's | 3 |
| **Flags** (`missing_*`) | `ProducerSpec.flag`, `MISSING_FLAGS`, `MISSING_CONDITIONS`, `GET /v1/assets/repair-summary`'s fields | Gone: the scheduler's work lists and the repair summary are keyed by name (`"missing": {name: count}`), the summary's own counts (`missing_proxy`, `missing_exif`, `missing_face_embeddings`) under the names they count. No client reads the summary | 3 |
| **Pause targets** | `producer_pauses.target` (work, and stopped redos: `keep_made`'s and `hold_new_version`'s); `/v1/producers/{target}/pause` and `/resume`; both CLIs | The names | 3 |
| **Settings keys** | `system_metadata`'s `producer.<artifact>` and `producer.<artifact>.version_seen`; the faces levels' SQL (`producer.faces`) | `producer.<name>` and `producer.<name>.version_seen`, both renamed in the migration, so no new-version hold is tripped. The reader-mark triggers match `producer.%` and still do | 3 |
| **Reader-work and check-in lists** (`previews`, `probe`, `capture`, `analysis_proxy`; `made_with`; `paused`) | `routers/scans.py`, `routers/readers.py`, `src/server/check_ins.py`; the device's `ORDER`; ingest's lineage keys | The names, at check-in protocol 2 | 3 |
| **Plug-in work ids** (`plugin_<id>`) | Lineage, pauses, settings keys, kinds, pools and flags of an installed plug-in's work | The plug-in's id (`geonames`): a plug-in's work is named by its plug-in, as a built-in's by what it makes. Ids that would share a namespace are refused at install from part 1, and the list stays after part 5 (section 8's reserved ids) | 3 |
| **File kinds** (`proxy`, `thumbnail`, `video_preview`, `analysis_proxy`, `scene_rep`) | `/v1/assets/{id}/artifacts/{kind}`; the batch upload's fields; `POST /v1/assets/{id}/uploads` and `uploads.artifact` with its CHECK; ingest's files; `artifacts.missing`'s entries; playback's `source`; the CLI's `--size`; the apps, Resolve, the web and the caches | The files' names (section 1) | 4 |
| **CLI and `lumiverb-server` words** | `lumiverb pause` and `resume`, `enrich`, `producers settings`, `redo` and `failures`, `scan --size`; `lumiverb-server pause` and `resume` | The names, typed with or without `lumiverb.` (`lumiverb pause image.description`): a name typed is the same name either way | 2 (both taken), 5 (names only) |
| **The web's literals** | `ProcessingSection.tsx` (`"location"`, `"faces"`, `"video_faces"`), `WelcomePage.tsx` (`faces`, `video_faces`), `KIND_LABELS`, the image and playback kinds | The names, with each part's server (it serves the web) | 3, 4 |
| **The apps' and Resolve's literals** | `artifacts/proxy`, `/thumbnail`, `/video_preview`; playback's `analysis_proxy`; Resolve's `thumbnail` | The files' names, reading either spelling the server sends | 2 |

The column `producer` in `GET /v1/producers` equals `artifact` once every producer is named by what it makes. It goes in part 5, with the aliases. Lineage's `producer` column stays: it says who made each clip's, and a person can be the maker.

### Kept, because each is a different thing

| Kept | Why |
|------|-----|
| AI job names (`vision`, `transcripts`, `clip`, `faces`) | A job is what an AI machine does, chosen per account with a model (Settings › AI), and several producers share one: descriptions, the text read and scene descriptions all use the vision job. Job names are the AI machines' protocol (`/v1/ai/jobs`, the AI worker's `/v1/lumiverb/clip`), not data |
| Models (`ViT-B-32-openai`, Whisper's `large-v3`, `buffalo_l`), and the apps' `model_id` `clip` | Their makers' names for them |
| Pools (`probe`, `exif`, `render`, `scenes`, `vision`, `clip`, `faces`, `transcripts`, `cpu`) | A pool is a resource several producers share (one vision pool for descriptions, text read and scene descriptions), so no one name fits it |
| The switches `all`, `upkeep`, `scans`, and the scan's job `scan` | They pause or are the machine's own work, not a producer's |
| `person` and `unknown` in lineage | Makers that aren't producers: a person, or a save that didn't say |
| Search's hit types (`asset`, `scene`, `transcript`) | Which part of the search index matched (the clip as a whole, a scene, a moment of its transcript): where a hit is, not what was made |
| Protocol 1's input words (`location`, `capture_time`, `metadata`, `preview`) | A published protocol. Every installed plug-in's manifest, the hash its outputs are kept with (`hash_sql`) and what its install accepted (`installed_plugins.permissions`) are in those words. Renaming them would redo every plug-in's work and ask every install again. Protocol 2 (phase 3) takes the names (section 3) |
| The API's field names (`artifact`, `producer`, `kind`, `target`) | A field names its value's role (which data; who made it; which job); the value becomes the name. Renaming the fields would change every client's shape for no new meaning |

## 7. The SQL spelling

A dotted name can't be a bind parameter, a column alias or an identifier in SQL. Where the code builds one from a name (`src_`, `wait_`, `:p_`, `:v_`, `:h_`, `:remake_`, `w_`, `r_`), it uses one mechanical spelling: the name with each dot and dash an underscore (`lumiverb.video.transcript` → `lumiverb_video_transcript`, a plug-in `my-tool` → `my_tool`). One function makes it (`src/producers/names.py` `sql_spelling`), and refuses anything that isn't a name or a plug-in's id: a remake's kind (`redo:<name>`) never reaches it. It's documented as a spelling, never a second name: it's never stored, never sent, and never typed.

Held by tests:
- every name on the install spells differently (`a.b_c` and `a_b.c` would collide): the registry check, and the install of a plug-in;
- every stored and sent name has its dots: after the migration, every value in lineage, pauses, crashes and settings keys, and every name in the API's answers, is in the name grammar, never a spelling;
- the spelling appears in `src` only from `sql_spelling`.

## 8. Old clients, and when their window ends

From part 1 the server takes the names wherever it takes an id. From part 3 it stores and sends only the names, and still takes the old spellings on what clients send, mapped to the names, until part 5. Each client's window:

| Client | How it changes | Its old spellings end |
|--------|----------------|----------------------|
| Devices (`lumiverb device run`) and the scheduler's own check-in | Part 2's speak check-in protocol 2 (names) when the server offers it, else 1. Part 3's server is protocol 2 only (`check_ins.PROTOCOL` and `OLDEST_PROTOCOL` 2) | At part 3: a device older than part 2 is told to upgrade, 426 `reader_too_old`, never left silent |
| The Mac and iPhone apps | Part 2's fetch files by their names and read playback's source in either spelling; Robert rebuilds both at part 2 | An app from before part 2 plays previews instead of analysis proxies from part 4, and its fetches stop at part 5 (404, naming the file's name) |
| Resolve's plug-in | Part 2's fetches thumbnails as `lumiverb.preview.thumbnail` | At part 5 |
| The CLI, on this machine or another | Part 2's takes and sends names | An older CLI's words and lineage are taken until part 5, then refused (422, naming the new name). One exception, found by the tester at part 3: a CLI from before part 2 matches `producers settings <word>` against `GET /v1/producers` itself, so from part 3, which lists names, it finds none ("No producer analysis_proxy"). It needs part 2's CLI or later; its pause, resume, failures, enrich and redo, which the server maps, still work |
| Scripts and people typing (`lumiverb pause vision`) | From part 2 the CLIs take names (`lumiverb.` optional) and old words with a warning naming the new one | At part 5: an old word is refused, saying the name to type |
| The web app | Served by the server, so it changes with each part. A tab left open across an upgrade sends what it loaded: taken until part 5 | At part 5 |
| Readers someone wrote (ADR-020 decision 9) | cursor-api says the names from part 1, and the old spellings' end at part 5 | At part 5 |

Every alias is in one table (`src/producers/names.py` `RENAMED`), which part 5 deletes. Nothing is taken "for good".

`RENAMED` is kept by family, and each family's mapping applies only where that family is taken: artifacts, lineage's producers (`ffprobe`, `exiftool`, …), kinds (`render`, `redo_*`), files, and plug-in work (`plugin_<id>`). One old word can mean several things:
- `clip` is an artifact (→ `lumiverb.image.likeness`), an AI job, and the model id the apps send;
- `location` is an artifact (→ `lumiverb.location.guess`), protocol 1's input, and lineage's producer;
- `preview` is video previews' lineage producer, playback's source, and protocol 1's input.

A flat table applied everywhere would break `/v1/ai/jobs/clip`, embeddings' `model_id` or a plug-in's inputs. A test holds that job names, model ids, search's hit types and protocol 1's inputs are never mapped.

**Producer ids, both ways.** `lineage.record` keeps a producer that isn't the spec's as `unknown`, and a clip made by `unknown` is stale, so it would be remade. So what's taken is mapped per artifact, in the direction storage needs:
- parts 1 and 2 map a producer's name to today's producer id (`lumiverb.capture_time` → `exiftool`);
- parts 3 and 4 map today's producer ids to the name, from older clients, from a job the previous scheduler handed out before the upgrade, and from the lineage an open upload kept.

A test saves a lineage under each spelling at each part and finds nothing due.

**Reserved ids.** A plug-in's id now shares a namespace with Lumiverb's: its work's kind, pool, pause target and settings key are its id, and a name may be typed without `lumiverb.`. So these are refused at install from part 1, and stay refused after part 5:
- the machine's words: `all`, `upkeep`, `scans`, `scan`, `person`, `unknown`, and any id starting `lumiverb`;
- the pools' names (`probe`, `exif`, `render`, `scenes`, `vision`, `clip`, `faces`, `transcripts`, `cpu`): a plug-in `faces` would share the faces pool;
- every old word `RENAMED` takes, of every family (`vision`, `faces`, `thumbnail`, `render`, `ffprobe`, …), so a plug-in `probe` can't be taken for `lumiverb.video.format`, nor `thumbnail` collide with an alias in pause targets, failures or the CLI's words;
- the first word of each name, typed without its prefix (`original`, `metadata`, `capture_time`, `preview`, `location`, `people`, `image`, `video`).

A plug-in installed before part 1 could have a reserved id (none on Robert's Mac: `example`, `geonames`). Part 3 never stops the upgrade for it:
- the migration leaves its work's rows as they are, under `plugin_<id>`;
- from part 3 Lumiverb leaves the plug-in out of what's installed, as it does a manifest it no longer reads (`plugin_catalog.installed`), so it isn't run and its work isn't a producer: nothing on Processing waits for it;
- while it's left out, its facts stop showing in the Lightbox and its places in Set location, as for any plug-in not installed; the message and part 3's release notes say so;
- it's said with what to do: "<title>'s id `<id>` is one Lumiverb uses now: uninstall it, then install it under another id". That shows on the Admin page's plug-ins row (red), in the log at every start, and in `lumiverb-server plugins`;
- installed again under another id, its work starts afresh under that id. What it made under the old one is removed with it when it's uninstalled.

The schema-wide net (part 3's tests) allows `plugin_<id>` only for such a plug-in.

**Caches.** The Mac app's proxy and thumbnail caches (up to 1 GB and 500 MB) and the iPhone's are kept by URL. Once part 2's rebuilt apps ask for files by their names, each file is fetched again once, under its name, and the old entries age out.

## 9. Parts

Five PRs, each released and checked on Robert's Mac (the gate) before the next starts. The order keeps each release working on its own:
- the server takes the names before any client sends them;
- every client reads both spellings before the server sends only names;
- storage and what's sent change together, in one release, per family;
- the aliases go last, once every client speaks names.

Robert's "migration first" is part 3. Before it come the two parts that make the migration safe: without part 1 nothing could send a name, and without part 2 the first release that sends names would break the apps and devices.

**The gate, for every part** (run by the coordinator on Robert's Mac). Counts move on a live install (a scan every 15 minutes, producers working), so it's held still: Pause all, save `GET /v1/producers?counts=true` and the queue's switches, upgrade, save them again, compare, resume. Pause all stops scans and every producer, and its own survival through the upgrade is part of what's compared.
- The comparison is part 1's `lumiverb producers --compare BEFORE AFTER`, over two saved answers. It compares every producer's counts (current, stale, missing, failing), settings, paused, redo stopped and `new_version`, by name, through `RENAMED`. Part 1's own "before" is v0.1.104's answer, saved from the API, since the CLI upgrades with the server;
- the snapshot after equals the one before;
- nothing remade (no current count down, no remake held that wasn't), and `new_version` nowhere it wasn't;
- pauses, stopped redos and settings kept (GeoNames' radius among them);
- the Admin page as before, and the log without errors.

### Part 1: the names, and the server taking them

- **What.** Sections 1 to 4: `Data`, `makes` and `reads`, `src/producers/names.py`, the page, the protocol 1 table, the registry checks and the SQL spelling.
  - Every route, form and lineage that takes an id today also takes its name, mapped to today's id, family by family; storage is untouched. That includes the files' names (`/v1/assets/{id}/artifacts/{kind}`, the batch upload's fields, `POST /v1/assets/{id}/uploads`' `artifact`, ingest's files, the CLI's `--size`) and lineage's `producer`. Part 2's apps, Resolve, device and scan send all of them, to a part 1 or part 2 server, and to part 1's again after part 2's rollback.
  - `lumiverb producers --compare BEFORE AFTER`, the gate's comparison of two saved `GET /v1/producers?counts=true` answers, through `RENAMED`.
  - `lumiverb-server migrate --to <revision>`: every account (and the control database, given its revision), down or up. It's the rollback tool for parts 3 and 4, shipped and tested before they need it. It refuses while the install is running, checking before it touches anything. As built, that's `restore`'s check (`backups.connected_elsewhere`): anything connected to its databases, the API and scheduler by name (`run` holds no lock of its own, and the check covers a Linux install's services too). With the service stopped, it starts a Mac's own PostgreSQL for the while (`postgres.up_while`), as `backup` and `restore` do; today's `migrate` doesn't.
  - The plug-in ids the machine uses refused at install.
  - No alembic revision.
- **Gate.** The snapshot unchanged.
- **Rollback.** Install v0.1.104.
- **Tests.**
  - The rules of section 2, each refusing a made-up producer that breaks it.
  - `reads` held to the SQL and inputs that run.
  - The whole registry as the code works it out, equal to main's values written into the test: needs, `part_of`, `redo_also`, tier, kind, flag, pool, `KINDS` with the `redo_` kinds, the pause targets, `MISSING_FLAGS`, `APPLIES`, `MADE`, `RECHECK`.
  - The page current.
  - The protocol 1 table, a plug-in handed exactly what it was.
  - Every route under each id, each name and each file's name, the same answer; a lineage under each producer spelling leaving nothing due.
  - Job names, model ids, hit types and protocol 1's inputs never mapped.
  - `migrate --to` down and up on a seeded install, and refused while the install runs; and run against a stopped install's own PostgreSQL, which it starts and stops again.
  - Reserved plug-in ids refused.
  - The spelling tests.
  - No alembic revision.

### Part 2: the clients speak the names

- **What.**
  - The CLI and `lumiverb-server` take names (`lumiverb.` optional) and old words with a warning. Like the device and the apps, the CLI reads either spelling in what the server answers: a CLI on another machine meets part 1 and 2 servers sending ids and part 3's sending names (`lumiverb producers`' eta and not-counted, `producers settings` matched against `GET /v1/producers`, `pause`'s switch targets, failures).
  - The device and the scan send names in lineage, failures, uploads and ingest (taken since part 1). They read the server's lists in either spelling, and speak check-in protocol 2 when it's offered.
  - The Mac and iPhone apps, and Resolve, fetch files by their names and read playback's source in either spelling. Robert rebuilds both apps.
  - No alembic revision.
- **Gate.** The snapshot unchanged. The rebuilt apps show previews, thumbnails and video previews, and play videos. A scan finds and sends a new file.
- **Rollback.** Install part 1's release, which takes what part 2's clients send.
- **Tests.**
  - The CLI's words: names, with and without `lumiverb.`, and old words with their warning. Part 2's CLI against a part 1 server and a part 3 server.
  - The device against a server at protocol 1 and at 2: both lists done.
  - The scan's names taken by part 1's server.
  - LumiverbKit: both playback spellings, and the files' names in URLs.
  - Resolve's thumbnail.

### Part 3: the rename, stored and sent

- **What.**
  - One tenant migration, one transaction per account. Its mapping is written into the migration file, never imported from `RENAMED`: part 5 deletes `RENAMED`, and an install that skips releases, a restored backup and a downgrade still run this migration. It renames:
    - artifact ids;
    - producer ids, per artifact, and only the spec's current producer: (`transcript`, `whisper`) becomes the name, (`video_preview`, `preview`) too, and so on. `''`, `person`, `unknown` and a producer that's no longer the spec's stay as they are. Renamed, a stale row would turn current, and `keep_made`'s kept rows would never be remade. The same rule renames every JSON lineage, an open upload's included;
    - pause targets (work and redo);
    - crash rows;
    - settings keys and `version_seen`;
    - plug-in work ids (`plugin_geonames` → `geonames`).
  - The registry keyed by names: `artifact` the name; `producer`, `kind` and `flag` gone.
  - Every literal in `src`, the web and the tests renamed.
  - What's sent is names only: `GET /v1/producers` (with `reads` and `reads_others`), the queue, failures, reader work, check-ins, 409 details, the repair summary keyed by name.
  - Check-in protocol 2 only.
  - The old spellings still taken on input, through `RENAMED`.
  - cursor-api, CLAUDE.md and ADR-024.
  - The file kinds wait for part 4, so this part is one family's.
- **Gate.** The snapshot before (part 2's, in names) equals the one after. Nothing remade, no new-version hold, pauses and settings kept. A device, if there is one, checks in at protocol 2.
- **Rollback.** On Robert's Mac:
  1. `brew services stop lumiverb`;
  2. `/opt/homebrew/opt/lumiverb/bin/lumiverb-server migrate --to f8c4a2e6d0b5 --env /opt/homebrew/etc/lumiverb/env`: part 3's code takes the accounts back to v0.1.104's head (parts 1 and 2 add no revision; parts 3 and 4 don't touch the control database). `migrate --to` refuses while the service runs. Otherwise part 3's API and scheduler would go on writing names into a database taken back to ids, and `run` would migrate it up again at its next start;
  3. point Homebrew's opt link at part 2's kept keg (the service runs `/opt/homebrew/opt/lumiverb/bin/lumiverb-server run`, and every upgrade keeps the old keg), then `brew pin lumiverb`, so the next `brew upgrade` doesn't move it back. The coordinator writes this step's exact commands from Homebrew's own linking code before the part ships, and the release notes mark them not rehearsed: it's never tried on Robert's install, and no throwaway uses Homebrew;
  4. `brew services start lumiverb`, then the gate: counts identical, nothing remade.

  Step 3 needs the previous version's keg still in the Cellar. By default Homebrew doesn't keep it: `brew upgrade` uninstalls the upgraded formula's old versions, and a fuller cleanup runs every 30 days. The coordinator upgrades Robert's Mac with `HOMEBREW_NO_INSTALL_CLEANUP=1`, which is why 14 kegs are kept. So:
  - parts 3 and 4 are upgraded with `HOMEBREW_NO_INSTALL_CLEANUP=1`;
  - the gate checks that `brew list --versions lumiverb` shows the previous version, before the upgrade and after it;
  - a rollback checks again first, because Homebrew's periodic cleanup can remove an old keg when Robert runs `brew` himself. If the keg is gone, the rollback is the before-upgrade backup.

  Or restore the backup taken before the upgrade (`backups.py`'s before an upgrade), which loses what was made since.

  Before the part ships, the tester rehearses every step but Homebrew's for real, on throwaway installs (which run from checkouts):
  - stop;
  - `migrate --to` refused while the install runs;
  - `migrate --to f8c4a2e6d0b5` with the newer code;
  - the previous release's code started on that database, with nothing remade or lost;
  - then up again.

  They do this for part 3, and for part 4 with an open upload and a pending repair. Step 3 is the coordinator's, as above.
- **Tests.**
  - The migration's own mapping equal to `RENAMED`, while both exist.
  - The migration up and down on an account seeded with every table and key:
    - lineage made, empty and failed, with JSON lineage;
    - a stale row by another producer, a row of an older version, and a `keep_made` pause, with the stale and `new_version` counts equal before and after;
    - pauses of work and redo, `hold_new_version`'s among them;
    - crashes; settings and `version_seen`; a plug-in's work;
    - an upload open across it, finished after.
  - An account from before part 3 upgraded straight to the head: a test that stays, so at part 5 it shows the migration needs nothing `RENAMED` held.
  - A schema-wide net, after the migration, on an account seeded through the real code paths (scans, jobs, a person's edits, a plug-in's work). It reads every text and jsonb column and `system_metadata`, and fails on an old id where an artifact, producer or kind belongs, allowing only the kept families (section 6) and the older producers the migration keeps.
  - The gate in a test: `GET /v1/producers` before and after, compared by name.
  - The scheduler's due lists equal before and after (nothing remade).
  - `hold_new_version` not tripped.
  - Every route takes both spellings and sends names.
  - A protocol 1 device gets 426.
  - The no-old-id test for this family. In `src`, the web's and the apps' code and their tests, it matches:
    - an old id as a whole string literal (`"capture"`, `'scene-detect'`);
    - `producer.<old>`, `missing_<old>`, `redo_<old>` and `plugin_<id>`;
    - an old id in a path (`/producers/vision/`).

    An allow-list holds what may still match, each entry with its family and file: the AI jobs `vision`, `clip`, `faces`, `transcripts`; a pool; search's hit type `transcript`; the apps' model id `clip`; protocol 1's words; `RENAMED` itself; and the migration's frozen mapping. A match outside it fails, naming the file and line.
  - The tester's rollback on a throwaway: upgrade, work done, `migrate --to` with part 3's code, part 2's release installed; nothing remade or lost.

### Part 4: the files' names

- **What.**
  - A tenant migration: `uploads.artifact` and its CHECK, and `artifacts.missing`'s entries, with its own mapping written into its file (as part 3's), and a test holding it equal to `RENAMED` while both exist.
  - The files' names in URLs, uploads, the batch fields, ingest, playback's source, the CLI's `--size` and the caches.
  - Old kinds still taken until part 5.
- **Gate.** The snapshot unchanged. Previews, thumbnails, video previews and playback on the web and both apps. An upload begun before the upgrade finishes after it. A missing-file repair pending before is still pending.
- **Rollback.** As part 3's, with part 4's code: stop the service, `migrate --to <part 3's tenant revision>`, the opt link to part 3's kept keg and pinned (the coordinator's step), start, the gate; or the backup. The release notes name the revision.
- **Tests.**
  - The migration up and down with an open upload and pending repairs.
  - The tester's rollback on a throwaway: an open upload and a pending missing-file repair through the upgrade, `migrate --to` down, and up again on part 3's code.
  - An upload begun before the migration finished after it.
  - Old and new URLs both answered.
  - The no-old-id test for file kinds.

### Part 5: the old spellings end

- **What.**
  - `RENAMED` deleted: an old spelling on input is refused, saying its name (422, or 404 for a URL), and the CLIs say the name to type.
  - `GET /v1/producers`' `producer` field goes.
  - `lumiverb producers --compare` keeps its own copy of the artifacts' old ids (it reads `RENAMED`'s now), so an answer saved before part 3 still compares.
  - cursor-api's note on the window says it closed, and in which release.
  - No alembic revision.
- **When.** Once part 4 is checked on Robert's Mac, his apps are part 2's or later, and no device is on protocol 1 (part 3 already refuses those).
- **Gate.** The snapshot unchanged. The web, both apps, Resolve, a scan and the CLI work.
- **Rollback.** Install part 4's release.
- **Tests.**
  - Each old spelling refused, naming the new.
  - The straight upgrade from before part 3 still passing with `RENAMED` gone.
  - The no-old-id test over `src`, the web, the apps, Resolve and the docs, with only the page's history and cursor-api's note allowed.
  - The web's, the apps' and Resolve's suites.

## 10. What a person sees

On screen, nothing (Processing shows titles). The page `docs/data-names.md` is new. Commands take the names: `lumiverb pause image.description`. Old words are warned of from part 2, then refused from part 5, saying the name to type. Robert rebuilds the Mac and iPhone apps at part 2.

## 11. Tests

Every test calls the real registry, producers, migrations and routes (none reimplements a rule), and none asserts speed. Each part's are in section 9. The tester adds acceptance tests of their own at each READY, from main's values written now:
- every `GET /v1/producers` row equal to today's through the names;
- a protocol 1 plug-in's work request byte for byte;
- end to end on throwaway installs:
  - the previous release's CLI, `lumiverb device run` and web app against the new server;
  - GeoNames' outputs not due after the upgrade;
  - each part's rollback with nothing remade or lost;
- LumiverbKit's and Resolve's tests (on main: 338 Swift tests, 0 failures; Resolve 42 passed).

## 12. Risks

- **Names are hard to change once plug-ins read them** (phase 3), and now they're in every account's data. The list in section 1 is the one to get right: the reviewer, the tester and Robert read it for words they'd look for and not find.
- **A missed rename** shows as a mass remake, a silent hold or a refused save. The gate checks each part on Robert's Mac. The no-old-id tests fail on an old id left as text, and the migration's tests seed every table and key that holds one.
- **Part 3 is big.** Every literal in the server changes at once. It's mechanical (`RENAMED` drives the migration and the aliases alike), and the whole-registry test holds every value the code works out to main's, through the names.
- **A name that hides two things** (`lumiverb.capture_time` also brings the GPS's accuracy; `lumiverb.preview` is two files). The page says what each holds.

## 13. Decided here, for Robert to overrule if he'd rather

- The API's field names stay; their values become names (section 6).
- A name may be typed without `lumiverb.`.
- A producer's remakes run as the kind `redo:<name>`.
- A plug-in's work is named by the plug-in's id.
- Tables, columns and storage keys keep their names: they're where a name's data is kept (`Data.stored`), never sent as a name. Columns could be renamed too (`proxy_key`, `has_transcript`, `video_indexed`: each a migration, with `active_assets` dropped and made again), at the cost of every query that reads them and no reader the better for it. Storage keys would move every file on disk. Recommended: keep both.

## Agreement

| Who | Says |
|-----|------|
| geonames-builder | Proposed, for (a) |
| geonames-reviewer | AGREE on 2f4c896c (the plan for (b)); AGREE on 4d581986 (for (a)), two nits folded in after it (text only): part 4's migration's own mapping, the caches fetched again at part 2; AGREE on 7ccc1d21, its line on the kept keg folded in after it (text only, worded by the coordinator: `HOMEBREW_NO_INSTALL_CLEANUP=1`, the previous keg checked by the gate and again before a rollback) |
| geonames-tester | AGREE on 2f4c896c (the plan for (b)); AGREE on 4d581986 (for (a)), its line folded in after it (text only): the rollback rehearsed on throwaways but for Homebrew's step, which the coordinator writes; AGREE on 7ccc1d21, its caution folded in after it (text only): the previous keg kept through the upgrade, checked by the gate |

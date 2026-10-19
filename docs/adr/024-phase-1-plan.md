# ADR-024 phase 1: Lumiverb's own data named (the plan)

## Status

Revised (2026-10-19) after Robert's decision (section 5): rename everything. For the builder, the reviewer and the tester to agree again before any code; then the coordinator's go for part 1. Robert's phase 1: "refactor builtins to align to the new naming schema". ADR-024's decisions 1, 2, 8 and 12 are his; this plan only says how.

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
| **Plug-in work ids** (`plugin_<id>`) | Lineage, pauses, settings keys, kinds, pools and flags of an installed plug-in's work | The plug-in's id (`geonames`): a plug-in's work is named by its plug-in, as a built-in's by what it makes. Ids the machine uses (`all`, `upkeep`, `scans`, `scan`, `person`, `unknown`, and any starting `lumiverb`) are refused at install from part 1 | 3 |
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
| Tables, columns and storage keys (`assets.proxy_key`, `video_facets`, `has_transcript`, a file's key) | Where a name's data is kept (`Data.stored`), never sent as a name. Renaming them would rewrite the schema and move every file on disk with no reader the better for it |
| The API's field names (`artifact`, `producer`, `kind`, `target`) | A field names its value's role (which data; who made it; which job); the value becomes the name. Renaming the fields would change every client's shape for no new meaning |

## 7. The SQL spelling

A dotted name can't be a bind parameter, a column alias or an identifier in SQL. Where the code builds one from a name (`src_`, `wait_`, `:p_`, `:v_`, `:h_`, `:remake_`, `w_`, `r_`), it uses one mechanical spelling: the name with each dot and dash an underscore (`lumiverb.video.transcript` → `lumiverb_video_transcript`, a plug-in `my-tool` → `my_tool`). One function makes it (`src/producers/names.py` `sql_spelling`). It's documented as a spelling, never a second name: it's never stored, never sent, and never typed.

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
| The CLI, on this machine or another | Part 2's takes and sends names | An older CLI's words and lineage are taken until part 5, then refused (422, naming the new name) |
| Scripts and people typing (`lumiverb pause vision`) | From part 2 the CLIs take names (`lumiverb.` optional) and old words with a warning naming the new one | At part 5: an old word is refused, saying the name to type |
| The web app | Served by the server, so it changes with each part. A tab left open across an upgrade sends what it loaded: taken until part 5 | At part 5 |
| Readers someone wrote (ADR-020 decision 9) | cursor-api says the names from part 1, and the old spellings' end at part 5 | At part 5 |

Every alias is in one table (`src/producers/names.py` `RENAMED`, old to new), which part 5 deletes. Nothing is taken "for good".

## 9. Parts

Five PRs, each released and checked on Robert's Mac (the gate) before the next starts. The order keeps each release working on its own:
- the server takes the names before any client sends them;
- every client reads both spellings before the server sends only names;
- storage and what's sent change together, in one release, per family;
- the aliases go last, once every client speaks names.

Robert's "migration first" is part 3. Before it come the two parts that make the migration safe: without part 1 nothing could send a name, and without part 2 the first release that sends names would break the apps and devices.

**The gate, for every part** (run by the coordinator on Robert's Mac, before and after the upgrade):
- a snapshot from `lumiverb producers --json` (part 1 adds it): every producer's counts (current, stale, missing, failing), settings, paused, redo stopped and `new_version`, keyed by name, the old ids written in names through `RENAMED`;
- the snapshot after equals the one before;
- nothing remade (no current count down, no remake held that wasn't), and `new_version` nowhere it wasn't;
- pauses, stopped redos and settings kept (GeoNames' radius among them);
- the Admin page as before, and the log without errors.

### Part 1: the names, and the server taking them

- **What.** Sections 1 to 4: `Data`, `makes` and `reads`, `src/producers/names.py`, the page, the protocol 1 table, the registry checks and the SQL spelling.
  - Every route, form and lineage that takes an id today also takes its name, mapped to today's id; storage is untouched.
  - `lumiverb producers --json`, the gate's snapshot.
  - `lumiverb-server migrate --to <revision>`: the control database and every account, down or up, the rollback tool for parts 3 and 4, shipped and tested before they need it.
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
  - Every route under each id and each name, the same answer.
  - `migrate --to` down and up on a seeded install.
  - Reserved plug-in ids refused.
  - The spelling tests.
  - No alembic revision.

### Part 2: the clients speak the names

- **What.**
  - The CLI and `lumiverb-server` take names (`lumiverb.` optional) and old words with a warning.
  - The device and the scan send names in lineage, failures, uploads and ingest (taken since part 1). They read the server's lists in either spelling, and speak check-in protocol 2 when it's offered.
  - The Mac and iPhone apps, and Resolve, fetch files by their names and read playback's source in either spelling. Robert rebuilds both apps.
  - No alembic revision.
- **Gate.** The snapshot unchanged. The rebuilt apps show previews, thumbnails and video previews, and play videos. A scan finds and sends a new file.
- **Rollback.** Install part 1's release, which takes what part 2's clients send.
- **Tests.**
  - The CLI's words: names, with and without `lumiverb.`, and old words with their warning.
  - The device against a server at protocol 1 and at 2: both lists done.
  - The scan's names taken by part 1's server.
  - LumiverbKit: both playback spellings, and the files' names in URLs.
  - Resolve's thumbnail.

### Part 3: the rename, stored and sent

- **What.**
  - One tenant migration, one transaction per account, renaming:
    - artifact ids, and producer ids in lineage's column and every JSON lineage;
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
- **Rollback.** `lumiverb-server migrate --to <part 2's revision>` with part 3's code, then install part 2's release. Or restore the backup taken before the upgrade (`backups.py`'s before an upgrade), which loses what was made since.
- **Tests.**
  - The migration up and down on an account seeded with every table and key: lineage made, empty and failed with JSON lineage; pauses of work and redo, `keep_made`'s and `hold_new_version`'s among them; crashes; settings and `version_seen`; a plug-in's work.
  - The gate in a test: `GET /v1/producers` before and after, compared by name.
  - The scheduler's due lists equal before and after (nothing remade).
  - `hold_new_version` not tripped.
  - Every route takes both spellings and sends names.
  - A protocol 1 device gets 426.
  - The no-old-id test for this family: no old artifact, producer, kind or flag id is written as text in `src`, the web or the apps, outside `RENAMED`.
  - The tester's rollback on a throwaway: upgrade, work done, `migrate --to` with part 3's code, part 2's release installed; nothing remade or lost.

### Part 4: the files' names

- **What.**
  - A tenant migration: `uploads.artifact` and its CHECK, and `artifacts.missing`'s entries.
  - The files' names in URLs, uploads, the batch fields, ingest, playback's source, the CLI's `--size` and the caches.
  - Old kinds still taken until part 5.
- **Gate.** The snapshot unchanged. Previews, thumbnails, video previews and playback on the web and both apps. An upload begun before the upgrade finishes after it. A missing-file repair pending before is still pending.
- **Rollback.** As part 3's: `migrate --to <part 3's revision>` with part 4's code, then install part 3's release; or the backup.
- **Tests.**
  - The migration up and down with an open upload and pending repairs.
  - An upload begun before the migration finished after it.
  - Old and new URLs both answered.
  - The no-old-id test for file kinds.

### Part 5: the old spellings end

- **What.**
  - `RENAMED` deleted: an old spelling on input is refused, saying its name (422, or 404 for a URL), and the CLIs say the name to type.
  - `GET /v1/producers`' `producer` field goes.
  - cursor-api's note on the window says it closed, and in which release.
  - No alembic revision.
- **When.** Once part 4 is checked on Robert's Mac, his apps are part 2's or later, and no device is on protocol 1 (part 3 already refuses those).
- **Gate.** The snapshot unchanged. The web, both apps, Resolve, a scan and the CLI work.
- **Rollback.** Install part 4's release.
- **Tests.**
  - Each old spelling refused, naming the new.
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

## Agreement

| Who | Says |
|-----|------|
| geonames-builder | Proposed, for (a) |
| geonames-reviewer | AGREE on 2f4c896c (the plan for (b)); on this revision: |
| geonames-tester | AGREE on 2f4c896c (the plan for (b)); on this revision: |

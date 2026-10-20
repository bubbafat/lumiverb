# Lumiverb's data, by name

Written from the producers (`src/producers/<artifact>/__init__.py`, each one's `makes` and `reads`) and
`src/producers/names.py` by `uv run python -m src.producers --write`. Don't edit it by hand: a test fails when it isn't what
the code says (ADR-024 phase 1, `docs/adr/024-phase-1-plan.md`).

Each `lumiverb.*` name is Lumiverb's own data, version 1, its fields a draft until a plug-in is first
handed one (ADR-024 phase 3). A name that only one kind of clip has says which: `lumiverb.image.*`
(photos: `image`, as a clip's `media_type` says) or `lumiverb.video.*`. What a reader would be handed is
what the clip shows now: where a person wrote it, theirs.

Until ADR-024 phase 1's part 5 each was also taken by what it was called before ("Was" below, its
history): since then the API refuses an old spelling, saying the name.

## `lumiverb.capture_time`

When it was taken, in UTC, and its time zone's offset when known, as the file says.

- **Fields:** `utc` time; `offset_min` number.
- **Made by:** Capture time and GPS accuracy (photos and videos).
- **Made from:** `lumiverb.original`.
- **Read by:** Location guesses (its own clip's and other clips').
- **Protocol 1's input:** `capture_time` (a plug-in's manifest says it that way).
- **Was:** `capture` (lineage's producer `exiftool`).

## `lumiverb.image.description`

A photo described, with tags.

- **Fields:** `description` text; `tags` list (`tag` text).
- **A person can write:** `description`, `tags`: then theirs is shown.
- **Made by:** Descriptions and tags (photos).
- **Made from:** `lumiverb.preview`.
- **Was:** `vision`.

## `lumiverb.image.faces`

The faces found in a photo: each one's box, vector and crop. Who each is: lumiverb.people.

- **A list, each with:** `box` record (`x` number, `y` number, `w` number, `h` number); `vector` vector; `crop` image.
- **Made by:** Faces (photos).
- **Made from:** `lumiverb.preview`.
- **Read by:** `lumiverb.people`.
- **Was:** `faces` (lineage's producer `insightface`).

## `lumiverb.image.likeness`

What visual search and Find similar compare: the photo's CLIP vector, and the model it's from.

- **Fields:** `vector` vector; `model` text.
- **Made by:** Visual search (CLIP) (photos).
- **Made from:** `lumiverb.preview`.
- **Was:** `clip`.

## `lumiverb.image.text`

The text read in a photo.

- **Fields:** `text` text.
- **A person can write:** `text`: then theirs is shown.
- **Made by:** Text in images (OCR) (photos).
- **Made from:** `lumiverb.preview`.
- **Was:** `ocr`.

## `lumiverb.location`

Where it was: a person's location, else the file's GPS, else an applied guess (never a suggestion), with how sure, in meters.

- **Fields:** `lat` number; `lon` number; `sure_m` number; `source` text.
- **Made by:** Lumiverb.
- **Made from:** `lumiverb.location.person`, `lumiverb.location.gps`, `lumiverb.location.guess`.
- **Protocol 1's input:** `location` (a plug-in's manifest says it that way).

## `lumiverb.location.gps`

The file's own GPS, with its accuracy when the file says.

- **Fields:** `lat` number; `lon` number; `accuracy_m` number.
- **Made by:** the scan (the capture producer reads the accuracy for older clips).
- **Read by:** Location guesses (its own clip's and other clips'), `lumiverb.location`.

## `lumiverb.location.guess`

Where it probably was, from the clips taken around the same time: applied, or a suggestion to take.

- **Fields:** `lat` number; `lon` number; `radius_m` number; `applied` yes or no.
- **Made by:** Location guesses (photos and videos).
- **Made from:** `lumiverb.capture_time`, `lumiverb.location.gps`, `lumiverb.location.person`; of other clips, `lumiverb.capture_time`, `lumiverb.location.gps`, `lumiverb.location.person`.
- **Read by:** `lumiverb.location`.
- **Was:** `location`.

## `lumiverb.location.person`

A location a person set: a point, a place, or the same as another clip's.

- **Fields:** `lat` number; `lon` number; `place` record (`id` text, `label` text, `credit` text).
- **A person can write:** `lat`, `lon`, `place`: then theirs is shown.
- **Made by:** a person.
- **Read by:** Location guesses (its own clip's and other clips'), `lumiverb.location`.

## `lumiverb.metadata`

Its media type, size, length, camera and lens.

- **Fields:** `media_type` text; `width` number; `height` number; `duration_sec` number; `camera_make` text; `camera_model` text; `lens` text.
- **Made by:** the scan (a video's length, its probe).
- **Read by:** Scenes, Faces in videos, Transcripts.
- **Protocol 1's input:** `metadata` (a plug-in's manifest says it that way).

## `lumiverb.original`

The file itself, in its library. Never handed to a plug-in: the API serves no originals.

- **Fields:** `file` file.
- **Made by:** the library.
- **Read by:** Video facts (probe), Capture time and GPS accuracy, Proxies and thumbnails, Video previews, Analysis proxies.

## `lumiverb.people`

Who's in it, photo or video: the people its faces are named as.

- **A list, each with:** `person` record (`id` text, `name` text); `confirmed` yes or no.
- **A person can write:** `person`, `confirmed`: then theirs is shown.
- **Made by:** a person, or Lumiverb (a face matched to someone named).
- **Made from:** `lumiverb.image.faces`, `lumiverb.video.faces`.

## `lumiverb.preview`

Its preview image and thumbnail: a still, for a video too.

- **Fields:** `preview` image; `thumbnail` image.
- **Made by:** Proxies and thumbnails (photos and videos).
- **Made from:** `lumiverb.original`.
- **Read by:** Descriptions and tags, Text in images (OCR), Visual search (CLIP), Faces.
- **Protocol 1's input:** `preview` (a plug-in's manifest says it that way).
- **Was:** `proxy`.
- **Files:** `lumiverb.preview` (was `proxy`), `lumiverb.preview.thumbnail` (was `thumbnail`).

## `lumiverb.video.analysis_proxy`

The copy of a video Lumiverb analyses: smaller, and the browser plays it.

- **Fields:** `video` video.
- **Made by:** Analysis proxies (videos).
- **Made from:** `lumiverb.original`.
- **Read by:** Scenes, Scene descriptions, Faces in videos, Transcripts.
- **Was:** `analysis_proxy` (lineage's producer `analysis-proxy`, its job's kind `render`).
- **Files:** `lumiverb.video.analysis_proxy` (was `analysis_proxy`).

## `lumiverb.video.faces`

The faces seen in a video: one for each person seen, and when. Who each is: lumiverb.people.

- **A list, each with:** `box` record (`x` number, `y` number, `w` number, `h` number); `frame_ms` number; `seen_ms` list (`ms` number); `vector` vector; `crop` image.
- **Made by:** Faces in videos (videos).
- **Made from:** `lumiverb.video.analysis_proxy`, `lumiverb.video.scenes`, `lumiverb.metadata`.
- **Read by:** `lumiverb.people`.
- **Was:** `video_faces` (lineage's producer `insightface-video`).

## `lumiverb.video.format`

A video's container, codecs, size, rotation, frame rate, timecode and audio, as ffprobe reads them.

- **Fields:** `duration_sec` number; `container` text; `video_codec` text; `width` number; `height` number; `rotation` number; `frame_rate` number; `start_timecode` text; `drop_frame` yes or no; `audio_codec` text; `audio_channels` number; `audio_sample_rate` number.
- **Made by:** Video facts (probe) (videos).
- **Made from:** `lumiverb.original`.
- **Was:** `probe` (lineage's producer `ffprobe`).

## `lumiverb.video.preview`

Its short preview video, the first seconds: moving, where lumiverb.preview is a still.

- **Fields:** `video` video.
- **Made by:** Video previews (videos).
- **Made from:** `lumiverb.original`.
- **Was:** `video_preview` (lineage's producer `preview`).
- **Files:** `lumiverb.video.preview` (was `video_preview`).

## `lumiverb.video.scene_descriptions`

Each scene described, with tags.

- **A list, each with:** `start_ms` number; `end_ms` number; `description` text; `tags` list (`tag` text).
- **Made by:** Scene descriptions (videos).
- **Made from:** `lumiverb.video.analysis_proxy`, `lumiverb.video.scenes`.
- **Was:** `scene_vision` (lineage's producer `scene-vision`).

## `lumiverb.video.scenes`

Where its scenes start and end, each with its picture.

- **A list, each with:** `start_ms` number; `end_ms` number; `picture` image.
- **Made by:** Scenes (videos).
- **Made from:** `lumiverb.video.analysis_proxy`, `lumiverb.metadata`.
- **Read by:** Scene descriptions, Faces in videos.
- **Was:** `scenes` (lineage's producer `scene-detect`).
- **Files:** `lumiverb.video.scenes.picture` (was `scene_rep`).

## `lumiverb.video.transcript`

What's said in it, with times.

- **Fields:** `language` text; `segments` list (`start_ms` number, `end_ms` number, `text` text).
- **A person can write:** `language`, `segments`: then theirs is shown.
- **Made by:** Transcripts (videos).
- **Made from:** `lumiverb.video.analysis_proxy`, `lumiverb.metadata`.
- **Was:** `transcript` (lineage's producer `whisper`).

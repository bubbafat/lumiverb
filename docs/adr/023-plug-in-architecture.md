# ADR-023: Plug-ins that stand on their own

## Status

Proposed (2026-10-18). Decided by Robert: plug-ins run outside Lumiverb ("They run outside for safety"). Everything else here is a proposal for his review before any code: see "Open questions".

Replaces ADR-021's decision 1 ("Built in only: code from outside Lumiverb is a later ADR of its own"). What ADR-021 built for an account (the switch, the acknowledgment, the re-accept rule, a plug-in's server-wide data and its progress) carries over.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | The manifest, and Settings › Plug-ins built from it: settings, what it reads, what it shows, what it downloads | Not started |
| 2 | The plug-in host and protocol: plug-ins as processes of their own, started, checked, restarted, logged | Not started |
| 3 | A plug-in's work on each clip through the protocol: its inputs, its outputs, kept and shown by Lumiverb | Not started |
| 4 | GeoNames moved out: a plug-in like any other, its Lumiverb code and tables gone | Not started |
| 5 | Confinement enforced on each platform | Not started |
| 6 | Installing plug-ins from elsewhere: the server admin's acceptance, updates, removal; "Writing a plug-in" | Not started |

## Overview

Robert, 2026-10-18, after asking how Place names' settings work: "I think this is missing the Mark. I believe that our plug-in should be a totally independent feature that can be loaded in Lumiverb, but which could also come from a third-party. In this case I would expect that the plug-in exposes some sort of configuration settings, declaration, which defines the variable to set the type range of valid values or some sort of validator and the help text description for the UI and the UI dynamically builds this. It should also declare any dependency information such as making it clear that it depends on location data or that it depends on proxies already existing without this. We haven't really built a plug-in architecture. We've just built kind of a half to bake a different way of turning on features."

He's right about what ADR-021 built. A plug-in today is a `PluginSpec` in Lumiverb's own Python: a title, a license and a switch. Everything Place names does is Lumiverb's code, named for it:

- its tables in the control database (`geonames_*`), and the account's `asset_place`;
- its SQL (`repository/places.py`, `asset_places.py`), its routes (`routers/places.py`), and its producer (`src/producers/place/`), whose setting is the producer's rather than the plug-in's;
- the web app asking for `plugin_id === "geonames"` (`SetLocationModal.tsx`) and drawing "At …" and "Near …" (`LocationRow.tsx`).

Nobody else could write one. Nothing says what it needs (a location). Its settings form is the producer's.

This ADR makes a plug-in a separate program. It ships a **manifest**: what it is, its settings with their types, bounds, validation and help text, what it reads, what it shows, and what it downloads. Lumiverb runs it **in a process of its own**, confined, and talks to it over a **protocol**. Lumiverb hands it only what it declared. It hands back only kinds of output Lumiverb knows how to keep and show. It never holds a key to Lumiverb's API or its database. The UI is built from the manifest, so no screen names a plug-in. Lumiverb's own GeoNames is rebuilt as the first one, which proves the design.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Where does a plug-in's code run? | **Decided (Robert):** outside Lumiverb, in a process of its own; never imported into the API or the scheduler. Any language: Lumiverb starts the command its manifest names. |
| 2 | What does a plug-in declare? | **Proposed:** a manifest file, read before any of its code runs: identity and version, its acknowledgment (as now), its settings schema, its inputs (what it needs and reads), its outputs (what it shows), its downloads and whether it needs the internet, and what it adds to Lumiverb's screens (lookups). |
| 3 | How are its settings shown and checked? | **Proposed:** Settings › Plug-ins builds the form from the schema. Each setting has a key, type, default, label and help text. A number also has bounds, a unit and whether it's whole. Text has a pattern and a length. A choice has its values with labels. A secret is never shown again. Each setting is the account's or the server's. Lumiverb checks every value against the schema before saving it or handing it on. A plug-in can also check what only it can know (does this API key work?), and its messages show beside the field. |
| 4 | What can it read? | **Proposed:** only the inputs it declares, from a fixed list Lumiverb defines and versions: a clip's location, capture time, a few metadata fields, its preview image; later its video, its words (description, text read in it, transcript). Never an original, a path, a library's folders, other clips, people, or the account. Each input it declares is a dependency (a clip without it waits, or isn't one it applies to) and a permission shown to the admin. |
| 5 | What can it give back? | **Proposed:** only kinds of output Lumiverb defines. In the first version these are **facts** shown with a clip ("Near Port of Spain"), optionally searchable, with the plug-in's credit, and **lookups** that a screen asks (Set location's Place search). Tags, scores and suggestions a person accepts come later, as a plug-in needs them. Lumiverb keeps what it gives back and checks every value (lengths, plain text, http(s) links only). |
| 6 | What can it reach? | **Proposed:** only what it declares; nothing else is handed to it. It runs with a clean environment (none of Lumiverb's settings, database addresses or keys), its own data folder, and no network. Lumiverb fetches the downloads it declares (the admin sees the hosts and sizes) and hands them over in a folder. A plug-in that must call out (an online service) declares `internet`, and the admin sees that before turning it on. The operating system enforces all of this (phase 5). |
| 7 | Who installs, who turns on? | **Proposed:** the server admin installs a plug-in for the install (code on the server is the install's to allow), accepting what it reads and reaches. Each account's admin then turns it on for their account, accepting its acknowledgment if it has one (ADR-021, unchanged). Lumiverb's own plug-ins come installed. |
| 8 | One process for every account? | **Proposed:** yes. A plug-in runs once for the server. Each request carries the account's settings and that account's clips' inputs, never which account it is. What it keeps (GeoNames' place list) is the server's. |

## Design

### The manifest

A plug-in is a folder with `lumiverb-plugin.json` and its program. Place names', for example:

```json
{
  "id": "geonames",
  "version": "4",
  "protocol": 1,
  "title": "Place names (GeoNames)",
  "description": "Find a place by name when setting a location, and see the nearest town and the landmark each photo or video was taken at.",
  "author": "Lumiverb",
  "homepage": "https://www.geonames.org/",
  "run": ["bin/geonames-plugin"],

  "acknowledgment": {"version": "3", "reaccept_from": "1", "title": "GeoNames: Creative Commons Attribution 4.0",
                     "text": "…", "links": [{"label": "The license (CC BY 4.0)", "url": "https://creativecommons.org/licenses/by/4.0/"}]},
  "credit": {"text": "GeoNames", "url": "https://www.geonames.org/"},

  "downloads": [
    {"url": "https://download.geonames.org/export/dump/cities1000.zip", "about_mb": 11, "title": "Towns of 1,000 people or more"},
    {"url": "https://download.geonames.org/export/dump/allCountries.zip", "about_mb": 403, "title": "Every place, for its landmarks"},
    {"url": "https://download.geonames.org/export/dump/alternateNamesV2.zip", "about_mb": 195, "title": "Names in other languages"}
  ],
  "data_version": 3,
  "internet": false,

  "settings": [
    {"key": "radius_km", "type": "int", "default": 25, "minimum": 1, "maximum": 200, "unit": "km",
     "label": "Nearest town within",
     "help": "How far away a town can be and still name where a clip was. Farther than this, a clip is \"Not near a town\".",
     "remakes": true},
    {"key": "mirror", "type": "text", "scope": "server", "default": "", "pattern": "^(https://.+)?$", "max_length": 500,
     "label": "Download from", "help": "A mirror of download.geonames.org, if this server can't reach it. Empty: GeoNames itself.",
     "advanced": true}
  ],

  "work": {
    "title": "Place names",
    "version": "2",
    "media": ["image", "video"],
    "inputs": ["location"],
    "batch": 200,
    "outputs": [
      {"key": "at", "kind": "fact", "label": "At"},
      {"key": "near", "kind": "fact", "label": "Near", "searchable": true}
    ]
  },

  "lookups": ["places"]
}
```

- `version`: the plug-in's release, compared as numbers with dots (ADR-021's rule). `protocol`: the protocol version it speaks; Lumiverb refuses one it doesn't speak. `work.version` goes up when its output would differ, as a producer's version does. Lumiverb then holds the remakes for an admin to start, as for a new producer version (ADR-018).
- The manifest is a file, not something the plug-in answers. What a plug-in reads and reaches is shown and accepted before any of its code has run.
- Lumiverb checks the whole manifest when it's installed: known types, inputs, outputs and lookups; bounds that make sense; a default within them; https URLs. A plug-in whose manifest fails doesn't start, and the server admin sees why.

### Settings: types, bounds, help, validation

The fields are the ones Settings › Processing already draws for a producer (`fields`: key, label, kind, default, minimum, maximum, unit, advanced, remakes, choices). The manifest adds what a stranger's plug-in needs:

| Type | Control | Checked by Lumiverb |
|------|---------|---------------------|
| `int`, `float` | number, with its unit | a finite number within `minimum`/`maximum`; whole for `int` |
| `bool` | switch | true or false |
| `text` | text field | `max_length` (at most 4,000), `pattern` (a regular expression, matched whole), not empty unless the default is |
| `choice` | menu | one of `choices` (`[{"value": "km", "label": "Kilometres"}]`) |
| `secret` | password field | as `text`; never returned by the API again (shown as "Set" or "Not set"), never logged |

Every setting has `label` and `help` (a sentence under the field). It can also have `advanced` (folded away), `remakes` (its outputs are remade when it changes, after the same 409 `redo_on_change` question as a producer's settings) and `scope`. `account` (the default) is set by each account's admin; `server` is set by the server admin, for things like a mirror or a key for a service the whole install uses.

A value is saved only once Lumiverb has checked it against the schema. Then, if the manifest says `"check": true`, the plug-in is asked (`POST /v1/settings/check`). It answers per field (`{"errors": {"api_key": "This key was refused by the service"}}`), and the error is shown beside that field. The plug-in's code never runs in the browser. Saved settings are kept per account (`plugin_settings`, in the account's database) or for the server (the control database), and are handed to the plug-in with each request.

### Inputs: what it needs, and what it may read

Lumiverb defines the inputs, and their shapes are versioned with the protocol. A clip without an input either **waits** (Lumiverb makes it for every clip, and it's coming) or **isn't one the plug-in applies to** (some clips never have it). Processing counts and says which.

| Input | What's handed over | A clip without it |
|-------|--------------------|-------------------|
| `location` | The location the clip shows (a person's, else the file's GPS, else an applied guess; never a suggestion): latitude, longitude, how sure in metres, its source | Not applicable: "4,392 have no location" |
| `capture_time` | When it was taken, with its time zone if known | Not applicable |
| `metadata` | Media type, width and height, duration, camera make and model, lens | — (every clip) |
| `preview` | The preview image (JPEG, up to 2048 px) | Waits for its preview |
| `video` (later) | The analysis proxy (H.264 MP4), streamed | Waits for it |
| `words` (later) | The description, the text read in it, the transcript | Waits while those are made; not applicable where they're off |

Each input a plug-in declares is the one thing it reads. Settings › Plug-ins says so in words: "Reads each clip's location". A plug-in can't ask for more than it's handed, because it can't call Lumiverb at all.

A clip's inputs are hashed when its work is handed out, and what the plug-in gives back is kept with that hash. A clip whose inputs have changed since (a person moved its location) is due again, and what it showed stays until the new output is saved. This is how Place names already treats a moved location (`asset_place.lat/lon`), made general.

### Outputs: what it gives back, and where it shows

| Kind | What it is | Where it shows |
|------|------------|----------------|
| `fact` | A short text for a clip (up to 200 characters), with an optional http(s) link | The Lightbox's details, under the plug-in's title, with its credit; signed-in only, never on a public page. `searchable`: its text is in the clip's search document |
| none (`null`) | Nothing to say for this clip ("Not near a town") | Counted as made, shown as the output's empty line if it declares one |

Later kinds, each when a plug-in needs it: `tags` (words a clip is found and filtered by), `score` (a number in declared bounds, for sorting and filtering) and `suggestion` (a proposed location, capture time or name, shown as "Maybe: … Use this / No"; a person decides, and derived data never overwrites theirs, as ADR-016 requires).

Outputs are kept by Lumiverb in the account's database (`plugin_outputs`: clip, plug-in, key, value, the inputs' hash, lineage). A plug-in has no storage in an account. Turning it off keeps its outputs and hides them. Removing it from the server removes them, after the server admin is asked.

### Lookups: what it adds to a screen

Some plug-ins answer a person rather than a clip. Lumiverb defines each lookup and the screen that asks it. The first is `places`, for Set location's Place search: `GET /v1/lookups/places?q=` returns places (`id`, `name`, `region`, `country`, `kind`, `lat`, `lon`, `matched`), and `GET /v1/lookups/places/{id}` returns one. Clients still call `GET /v1/places?q=`. The API asks every plug-in that is on in the account and offers `places`, and labels each place with the plug-in that found it. A location set from a place records which plug-in and which place (`basis`), so the place's name and credit show with it.

### The protocol

HTTP/1.1 with JSON (multipart where images go), over a Unix socket in the plug-in's run folder that only Lumiverb can reach. Lumiverb asks; the plug-in answers:

| Request | What for |
|---------|----------|
| `GET /v1/health` | `ready`, `preparing` (with `done`, `total`, `detail`: shown as "N of M") or `failed` (with why) |
| `POST /v1/prepare` | Its downloads are in `downloads/`: build its data in its own folder. Lumiverb deletes the downloads once it's ready. Called again when `data_version` goes up; the old data is used meanwhile |
| `POST /v1/work` | A batch of clips (ids that mean nothing outside the request), each with its declared inputs, and the account's settings. It answers per clip: its outputs, or an error that is the clip's (charged, as a failure) or the plug-in's (nothing charged, the clips wait, as for an AI machine that's down) |
| `POST /v1/settings/check` | Its own check of new settings, when it declares one |
| `GET /v1/lookups/<name>` | A lookup it offers |

Every request has a time limit. Lumiverb checks every answer against the manifest (keys it declared, kinds and lengths) and refuses the rest, which is counted as the plug-in's error.

### The host

`lumiverb-server plugins` starts and watches every installed plug-in that some account has on. On a Mac it's a child of `run`, beside the API and the scheduler (`supervise.py`'s restart rules). On Linux it's a unit of its own, written by `update.sh` as the scheduler's is. For each plug-in it:

- prepares the run folder and socket, and starts the manifest's `run` command with a clean environment: its folders, the socket, nothing of Lumiverb's;
- waits for `ready`, restarts it when it exits (waiting longer each time, up to 10 minutes) and stops it when no account has it on;
- sends its output to the service log, named (`plugin geonames | …`), so Admin › Logs shows it.

The API and the scheduler talk to it through the socket. While it's down, its clips wait (`plugin_down`, a new wait code: no silent waits) and its lookups say it's unavailable.

### Confinement (phase 5)

A plug-in, made by anyone, gets the inputs it's handed and nothing else. It must not be able to:

- read the originals, the previews, the database, the env file or other plug-ins' folders;
- write anywhere but its own data folder;
- reach the network unless it declared `internet`.

Lumiverb starts it inside the operating system's confinement:

- **Mac:** a sandbox profile that Lumiverb writes for each plug-in and starts it under (`sandbox-exec`). It reads only the system's folders, its runtime and its own folder; it writes only its data folder; it uses no network but its socket. Tried on macOS 26.5 (2026-10-18): a process under such a profile was refused the network, refused reading Lumiverb's data folder and refused writing outside its own folder, and could write its own. Apple calls `sandbox-exec` deprecated, so a test proves it on every release.
- **Linux:** a systemd unit for each plug-in, as a user of its own (`DynamicUser`), with `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, its data as its `StateDirectory`, Lumiverb's folders and the libraries' roots `InaccessiblePaths`, a memory limit, and `IPAddressDeny=any` with only Unix sockets allowed unless it declared `internet`. Not tried yet.

Tests start a plug-in that tries each forbidden thing and must be refused. The Admin page shows each plug-in's confinement. Where confinement can't be set up, a plug-in from elsewhere doesn't start, and the page says why.

### Installing (phase 6)

`lumiverb-server plugins install PATH-OR-URL --sha256 HASH` runs on the machine, as `server-admins` does. It shows the manifest in words (what it reads, what it shows, what it downloads and from where, whether it uses the internet) and asks the server admin to accept. Lumiverb records who accepted which version and what it was allowed. Later, the server admin could do this on the Admin page.

- An update that reads or reaches more asks again; one that asks the same or less doesn't.
- `uninstall` stops the plug-in. Its outputs are removed after the server admin is asked how many there are (the API requires their say).
- Lumiverb's own plug-ins ship in the install (`plugins/<id>/`), installed already.

"Writing a plug-in" (docs) is the protocol's whole contract, as cursor-api's "Writing a client" is a reader's. A test holds it to the code.

## Place names, rebuilt (phase 4)

GeoNames becomes a program in `plugins/geonames/` that imports nothing of Lumiverb's (a test holds that), with the manifest above:

- **Its data:** the same downloads, which Lumiverb fetches into its folder. It builds its own database there (towns, landmarks, names in other languages), not in Lumiverb's control database.
- **Its work:** given each clip's location and the radius, it answers `near` and `at`, as the producer and `repository/places.py` do now: the same rules for which landmarks count and how far each kind reaches, moved into the plug-in.
- **Its lookup:** `places`, as place search is now (the same tiers: exact names first, then other languages, then the start of a name).
- **What goes from Lumiverb:** `src/producers/place/`, `repository/places.py` and `asset_places.py`, `routers/places.py`'s GeoNames code, the `geonames_*` tables, `asset_place`, `src/plugins/geonames/`, and the web app's `geonames` checks. Each goes only once its replacement holds its data (keep until replaced). The 14,237 named clips on Robert's Mac are named again by the plug-in, which took about 45 seconds as a producer, and their old names show until then. A person's locations set from a place keep their place, recorded as the plug-in's.

## Edge cases

| Scenario | Behavior |
|----------|----------|
| The plug-in crashes or hangs | Its request times out; it's restarted, waiting longer each time; its clips wait (`plugin_down`), uncharged; Settings and Processing say so |
| It answers something it didn't declare, or too long, or HTML | Refused and counted as its error; never shown |
| It asks for an input Lumiverb doesn't define, or a protocol Lumiverb doesn't speak | Its manifest fails: not installed, and the server admin is told why |
| Two accounts with different settings | Each request carries its account's settings; nothing of one account reaches another's requests |
| A setting changes that remakes | The 409 `redo_on_change` question, as for a producer; remade after everything else |
| An account turns it off | Its outputs stay and are hidden; nothing more is made; turned on again, what's missing is made |
| Its data needs a newer download (`data_version`) | Fetched and prepared again in the background; the old data is used until the new is ready |
| A download fails or the disk fills | `failed` with why, tried again an hour later (as `plugin_data` today) |
| An update reads more than before | Not started until the server admin accepts it |
| The Mac's sandbox goes away in a macOS release | Plug-ins from elsewhere don't start; the Admin page says why; Lumiverb's own run unconfined, as its own code always has |

## Code references

| Area | File | Notes |
|------|------|-------|
| Today's plug-in | `src/plugins/contract.py`, `src/plugins/__init__.py`, `src/plugins/geonames/` | The spec becomes the manifest |
| Account switch, acknowledgment | `src/server/repository/plugins.py`, `routers/plugins.py`, `pages/settings/PluginsSection.tsx` | Kept; reads manifests |
| Server-wide data | `src/server/plugin_data.py`, `repository/plugin_data.py` | Becomes the host's downloads and `prepare` |
| A plug-in's producer | `src/producers/contract.py` (`ProducerSpec.plugin`), `lineage.APPLIES`, `src/producers/place/` | Becomes one generic producer for every plug-in's `work` |
| Settings fields | `src/producers/contract.py` `Setting`, `ProcessingSection.tsx` | The schema's shape and the form's controls |
| GeoNames in Lumiverb | `repository/places.py`, `asset_places.py`, `routers/places.py`, `routers/locations.py`, `SetLocationModal.tsx`, `LocationRow.tsx` | Moves into the plug-in (phase 4) |
| A process Lumiverb runs and asks | `src/server/ai_worker/` (ADR-022), `lifecycle/supervise.py` | The host's model |

## Doc references

- `docs/cursor-api.md`: plug-in settings, lookups, `plugin_down`.
- `docs/architecture.md`: the plug-in host.
- New: `docs/writing-a-plugin.md` (phase 6).
- `CLAUDE.md`: the Plug-ins row.

## Alternatives considered

- **In-process Python packages** (entry points, as many apps load plug-ins). Simpler, but a plug-in would run as the server, with its database, its keys and every library's originals. Robert: "They run outside for safety."
- **Plug-ins as services anywhere, at a URL with a key** (as AI machines are). This is the same protocol at another address, so it stays possible. But Lumiverb couldn't confine what it doesn't start, and the server admin would be installing and running each one by hand. Lumiverb starting them comes first.
- **WebAssembly.** Strong confinement in any language, but GeoNames' work (zip files, a database, half a gigabyte of downloads) is a poor fit for it today, and no plug-in needs it yet.
- **Plug-ins calling Lumiverb's API with a key of their own.** They could read anything the key allows, and confinement would mean scoping a key per plug-in. Being handed declared inputs is smaller and easier to check.

## What this does NOT include

- A plug-in catalogue or store, or plug-in updates found automatically.
- Plug-ins that add pages or their own UI. Every screen is Lumiverb's, built from what's declared.
- Moving faces, transcripts, descriptions or location guesses into plug-ins (ADR-021 phase 4). Each might come later, once this exists.
- Video and words inputs, tags, scores and suggestions: each in a later phase, when a plug-in needs it.

## Open questions (for Robert)

1. **Who installs:** the server admin installs for the install, and each account's admin turns it on (decision 7). Or should any account admin be able to install?
2. **Network:** none by default; Lumiverb fetches the downloads a plug-in declares; `internet` is a permission the admin sees (decision 6). Right?
3. **First outputs:** facts and the place lookup only, which is what GeoNames needs (decision 5). Tags, scores and suggestions come when a plug-in needs them.
4. **The Mac's confinement** rests on `sandbox-exec`: deprecated by Apple, but working on macOS 26.5. If a release removes it, plug-ins from elsewhere stop on Macs until something replaces it. Acceptable?
5. **Order:** phase 1 first (the manifest and the form built from it, GeoNames still inside Lumiverb), so you can try the form early. Or go straight to the host (phase 2) and show both together?

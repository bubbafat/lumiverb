# ADR-023: Plug-ins that stand on their own

## Status

Accepted (2026-10-18, Robert). Plug-ins run outside Lumiverb ("They run outside for safety"). His answers to the proposal's questions are under "Robert's answers"; the decisions below are his. Built runner first, with Settings › Plug-ins as "a ux stick in the ground that we can iterate on".

Replaces ADR-021's decision 1 ("Built in only: code from outside Lumiverb is a later ADR of its own"). What ADR-021 built for an account (the switch, the acknowledgment, the re-accept rule, a plug-in's server-wide data and its progress) carries over.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1a | The manifest, and installing a plug-in from a folder on the machine (the server admin's, on or off by default); every account's switch follows the default until its admin chooses | Built: `src/plugins/manifest.py`, `lumiverb-server plugins install|default|uninstall` (`src/server/lifecycle/plugins_cli.py`, `src/server/plugin_install.py`), `installed_plugins` and `plugin_install_log` (control migration `f7c3a1e9b5d2`), `src/server/plugin_catalog.py`, `GET /v1/plugins`' installed plug-ins, `tests/plugins/hello/`; `tests/test_plugin_manifest.py`, `tests/test_plugin_install.py` |
| 1b | The runner: each plug-in a process of its own, started, checked, restarted, kept from taking the server down (memory, time, priority, its folder's size), logged; on a Mac and on Linux | Built: `src/server/plugin_runner.py` and `plugin_launch.py`, a Mac's `run` child `plugins`, Linux's `lumiverb-plugins.service`, `GET /v1/plugins`' `running`, Admin › Logs' Plug-ins; `tests/test_plugin_runner.py` (crash, hang, nonsense, memory, a big file, a full folder, a clean environment, a lower priority, reinstall, uninstall), on macOS and on Linux (Debian, in a container) |
| 1c | Settings › Plug-ins built from the manifest (the stick in the ground): its state, what it reads, what it downloads, its settings form, checked by the server | Built: `pages/settings/InstalledPlugin.tsx`, `src/server/plugin_settings.py` (`plugin_settings` per account, `plugin_server_settings` for the server), `PUT /v1/plugins/{id}/settings` and `/server-settings` (422 `invalid_settings` by key; the plug-in's own check), `src/server/plugin_client.py`, Lumiverb's example `src/plugin_examples/example/`; tried on a throwaway install in a browser |
| 2 | A plug-in's work on each clip through the protocol: its inputs, its outputs, kept and shown by Lumiverb; lookups | Built (v0.1.96 to v0.1.99). 2a built: the producers can change while the API and the scheduler run (`src/producers/__init__.py` `use_added`, `src/producers/live.py`, the scheduler's `_take_up_kinds`; `tests/test_added_producers.py`). 2b built: each installed plug-in's work is a producer (`src/server/plugin_producers.py`, from its manifest; read again by the API and the scheduler every half minute), its clips' inputs and their hash (`src/server/repository/clip_inputs.py`, `POST /v1/plugins/{id}/inputs`), one `POST /v1/work` a job (`src/producers/plugin_work.py`: settings, secrets too, read from the databases by the scheduler), what it gives back checked and kept (`manifest.checked_outputs`, `plugin_outputs`, `POST /v1/plugins/{id}/outputs`), `plugin_down` (the scheduler holds its work back while it isn't running), a setting that remakes asks (409 `redo_on_change`), the example's work; `tests/test_plugin_work.py` with a test plug-in (`tests/plugins/facts/`). 2c built: its facts in the clip's detail and the Lightbox (`repository/plugin_outputs.py` `facts_of`, `GET /v1/assets/{id}`'s `facts`, `components/PluginFactsSection.tsx`) and found by the account's search (`plugin_outputs.search_text` and `words`, `text_search.facts`), while it's on. 2d built: lookups (`src/server/plugin_lookups.py`): `GET /v1/places` asks each plug-in on here that finds places, each place labelled with its source and credited, a plug-in that can't answer said in `unavailable`; a location set from a plug-in's place asks it for the point and credits it; Set location offers Place search with one on; the example finds a few places. Phase 2 is done |
| 3 | GeoNames moved out: a plug-in like any other, its Lumiverb code and tables gone | Built, in four parts (`docs/adr/023-phase-3-plan.md`, agreed 2026-10-19). Part 1, what a real plug-in needs that no plug-in had (all of it general, protocol 1): a plug-in is run only while it's on somewhere (`plugin_catalog.OnSomewhere`, the runner's `off`; an account that can't be read keeps what it said last; part 1b: read on a thread of its own, a connection given up on after a few seconds, so an account's database host away holds nothing up); its server settings at start (`LUMIVERB_PLUGIN_SETTINGS`, started again when they change); `ready` with work in the background; an output's `empty` line; a credit's `license`; this machine's proxy handed on; facts shown and searched only while the clip has their inputs (`plugin_outputs.shown_sql`); rows of a producer this server no longer has left out of failures and retry. Part 2, the program: `src/own_plugins/geonames/` (Lumiverb's own plug-ins, one folder each: a manifest and its program; shipped, not installed yet), written against the protocol alone in Python 3.9's standard library, run with the machine's `python3 -I`; its own list, a SQLite file in its data folder (never WAL), fetched from GeoNames or its `mirror` and built on a thread of its own while it answers, its downloads kept across restarts, a build stopped three times in a row not tried a fourth until an hour on; answering as today's Place names does, held to it by side-by-side tests on the same files and a golden file written from today's code (`tests/plugins/geonames_golden.json`), which outlives it. Part 3, the switch-over: GeoNames is installed by Lumiverb itself as migrating's last step (`src/server/own_plugins.py`, and the runner at its start; off by default, every account's switch and acceptance standing, a failure said and shown on the Admin page), its built-in registry entry and producer gone (`/v1/places/nearest`, `/names` 410), the producer's radius and pauses carried over to the plug-in's work by a tenant migration, and what the built-in left standing in for the plug-in until part 4 where GeoNames is on by the account's own switch: each clip's old name until the plug-in's are saved, its list answering search and set-by-place while the plug-in can't; a place's credit kept with every location set from it. A search's `matched`, between two other names of a place equally good, follows code-point order as a Mac's database (locale C) did. Part 4, the removal, with plain migrations (Robert, 2026-10-19: an install that jumps to it from before part 3 has no place names or Place search for the minutes the plug-in takes to fetch and name, nothing a person made lost): the built-in's list and ADR-021's `plugin_data` dropped from the control database (migration `d7f3b9e1c5a2`), each account's old names (`asset_place`, migration `f8c4a2e6d0b5`), the fallbacks and the detail's `place` with them, its code (`src/plugins/geonames/`, `repository/places.py`, `asset_places.py`, `src/server/plugin_data.py`) and ADR-021's registry (`PluginSpec` stays, a manifest as an account's switch sees it); nothing in `src/` names GeoNames but its program (a test), what its install takes from an install's settings said in its folder (`lumiverb-own.json`). Its version 2 searches `at` as well as `near` (Robert, 2026-10-19: people search by landmarks more than towns), its work's version unchanged, so nothing is named again: the clips it named before are found by their landmark once the search sweep has written their text again from their facts (`plugin_outputs.searched_again`, for any plug-in whose newer version searches other outputs). Set location always offers Place, saying why when no plug-in that finds places is on (`GET /v1/places`' 409 `plugin_off`, naming those installed that would) or one can't answer now. Phase 3 is done |
| 4 | Downloads fetched by Lumiverb, and network and file confinement | Maybe in the future (Robert, 2026-10-19: see "Confinement: maybe later"). Without confinement, a plug-in fetches its own downloads |
| 5 | Installing plug-ins from elsewhere (a URL and its checksum), updates that ask again, removal; "Writing a plug-in" | Not started |

## Overview

Robert, 2026-10-18, after asking how Place names' settings work: "I think this is missing the Mark. I believe that our plug-in should be a totally independent feature that can be loaded in Lumiverb, but which could also come from a third-party. In this case I would expect that the plug-in exposes some sort of configuration settings, declaration, which defines the variable to set the type range of valid values or some sort of validator and the help text description for the UI and the UI dynamically builds this. It should also declare any dependency information such as making it clear that it depends on location data or that it depends on proxies already existing without this. We haven't really built a plug-in architecture. We've just built kind of a half to bake a different way of turning on features."

He's right about what ADR-021 built. A plug-in today is a `PluginSpec` in Lumiverb's own Python: a title, a license and a switch. Everything Place names does is Lumiverb's code, named for it:

- its tables in the control database (`geonames_*`), and the account's `asset_place`;
- its SQL (`repository/places.py`, `asset_places.py`), its routes (`routers/places.py`), and its producer (`src/producers/place/`), whose setting is the producer's rather than the plug-in's;
- the web app asking for `plugin_id === "geonames"` (`SetLocationModal.tsx`) and drawing "At …" and "Near …" (`LocationRow.tsx`).

Nobody else could write one. Nothing says what it needs (a location). Its settings form is the producer's.

This ADR makes a plug-in a separate program. It ships a **manifest**: what it is, its settings with their types, bounds, validation and help text, what it reads, what it shows, and what it downloads. Lumiverb runs it **in a process of its own**, kept from taking the server down, and talks to it over a **protocol**. Lumiverb hands it only what it declared. It hands back only kinds of output Lumiverb knows how to keep and show. It never holds a key to Lumiverb's API or its database. The UI is built from the manifest, so no screen names a plug-in. Lumiverb's own GeoNames is rebuilt as the first one, which proves the design.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Where does a plug-in's code run? | Outside Lumiverb, in a process of its own; never imported into the API or the scheduler. Any language: Lumiverb starts the command its manifest names. |
| 2 | What does a plug-in declare? | A manifest file, read before any of its code runs: identity and version, its acknowledgment (as now), its settings schema, its inputs (what it needs and reads), its outputs (what it shows), its downloads and whether it needs the internet, and what it adds to Lumiverb's screens (lookups). |
| 3 | How are its settings shown and checked? | Settings › Plug-ins builds the form from the schema. Each setting has a key, type, default, label and help text. A number also has bounds, a unit and whether it's whole. Text has a pattern and a length. A choice has its values with labels. A secret is never shown again. Each setting is the account's or the server's. Lumiverb checks every value against the schema before saving it or handing it on. A plug-in can also check what only it can know (does this API key work?), and its messages show beside the field. |
| 4 | What can it read? | Only the inputs it declares, from a fixed list Lumiverb defines and versions: a clip's location, capture time, a few metadata fields, its preview image; later its video, its words (description, text read in it, transcript). Never an original, a path, a library's folders, other clips, people, or the account. Each input it declares is a dependency (a clip without it waits, or isn't one it applies to) and a permission shown to the admin. |
| 5 | What can it give back? | For now (Robert), only kinds of output Lumiverb defines. In the first version these are **facts** shown with a clip ("Near Port of Spain"), optionally searchable, with the plug-in's credit, and **lookups** that a screen asks (Set location's Place search). Tags, scores and suggestions a person accepts come later, as a plug-in needs them. Lumiverb keeps what it gives back and checks every value (lengths, plain text, http(s) links only). |
| 6 | What can it reach? | Only what it declares: "Safety and isolation first" (Robert). It runs with a clean environment (none of Lumiverb's settings, database addresses or keys) and its own data folder, and is handed nothing else. It declares what it downloads (its data: each file's host and size) and whether it calls out otherwise (`internet`: an online service), and the admin sees both before it's installed. It fetches its downloads itself, into its own folder, saying how far as it goes (its health's `preparing`, N of M). What it declares is shown and accepted, not enforced (decision 9). |
| 7 | Who installs, who turns on? | The server admin installs a plug-in for the install (code on the server is the install's to allow), accepting what it reads and reaches, and installs it **on or off by default** "to make one's config easier" (Robert). An installed plug-in is there for every account and, where it's on, every library. An account follows the default until its admin turns the plug-in on or off there; after that its admin's choice stands. A plug-in with an acknowledgment that's on by default is accepted by the server admin at install, for the accounts that follow the default; an account admin turning it on themselves accepts it as now (ADR-021). Lumiverb's own plug-ins come installed. |
| 8 | One process for every account? | Yes. A plug-in runs once for the server. Each request carries the account's settings and that account's clips' inputs, never which account it is. What it keeps (GeoNames' place list) is the server's. |
| 9 | How isolated? | Good enough, on a Mac and on Linux: "We don't need enterprise security. We're just doing a good enough job. I don't want them taking down the server" (Robert). A process of its own, at a lower priority than Lumiverb's, ended by the runner when it uses more memory than it may, its requests given a time limit, its folder's size watched, restarted when it stops (waiting longer each time) and left stopped, with why, when it keeps stopping. Nothing Lumiverb runs waits on it longer than a request's limit. Confining its files and network is maybe in the future: Robert, 2026-10-19, "Let's hold off on this - mark it as maybe in the future. Right now being admin-only installs addresses my core concern." The server admin installs only what they trust. |

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
  "memory_mb": 1024,
  "disk_mb": 2000,

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
      {"key": "at", "kind": "fact", "label": "At", "searchable": true},
      {"key": "near", "kind": "fact", "label": "Near", "searchable": true}
    ]
  },

  "lookups": ["places"]
}
```

- `version`: the plug-in's release, compared as numbers with dots (ADR-021's rule). `protocol`: the protocol version it speaks; Lumiverb refuses one it doesn't speak. `work.version` goes up when its output would differ, as a producer's version does. Lumiverb then holds the remakes for an admin to start, as for a new producer version (ADR-018).
- `memory_mb` and `disk_mb`: what it may use, at most what the server allows (the runner holds it there: "Isolation").
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
| `preview` | The preview image as producers see it: at most 1,280 px on its long edge, JPEG or WebP (base64, with its type) | Waits for its preview |
| `video` (later) | The analysis proxy (H.264 MP4), streamed | Waits for it |
| `words` (later) | The description, the text read in it, the transcript | Waits while those are made; not applicable where they're off |

Each input a plug-in declares is the one thing it reads. Settings › Plug-ins says so in words: "Reads each clip's location". A plug-in can't ask for more than it's handed, because it can't call Lumiverb at all.

A clip's inputs are hashed when its work is handed out, and what the plug-in gives back is kept with that hash. A clip whose inputs have changed since (a person moved its location) is due again, and what it showed stays until the new output is saved. This is how Place names already treats a moved location (`asset_place.lat/lon`), made general.

### Outputs: what it gives back, and where it shows

| Kind | What it is | Where it shows |
|------|------------|----------------|
| `fact` | A short text for a clip (up to 200 characters), with an optional http(s) link | The Lightbox's details, under the plug-in's title, with its credit (and its license, `credit.license`); signed-in only, never on a public page. `searchable`: its text is in the clip's search document |
| none (`null`) | Nothing to say for this clip ("Not near a town") | Counted as made, shown as the output's empty line if it declares one (`empty`: plain text, up to 80 characters; never searched) |

A clip's outputs are shown and searched only while it's one of the work's media and has every input the work reads: a clip whose location a person cleared isn't one it applies to any more, so what it made (never made again) is kept and hidden, and shows again if the location comes back.

Later kinds, each when a plug-in needs it: `tags` (words a clip is found and filtered by), `score` (a number in declared bounds, for sorting and filtering) and `suggestion` (a proposed location, capture time or name, shown as "Maybe: … Use this / No"; a person decides, and derived data never overwrites theirs, as ADR-016 requires).

An output renamed in a newer version says the key it was saved under (`was`, ADR-024 phase 2): the search sweep renames what's saved under that, nothing made again.

Outputs are kept by Lumiverb in the account's database (`plugin_outputs`: clip, plug-in, key, value, the inputs' hash, lineage). A plug-in has no storage in an account. Turning it off keeps its outputs and hides them. Removing it from the server removes them, after the server admin is asked.

### Lookups: what it adds to a screen

Some plug-ins answer a person rather than a clip. Lumiverb defines each lookup and the screen that asks it. The first is `places`, for Set location's Place search: `POST /v1/lookups/places` with what was typed returns places (`id`, `name`, `region`, `country`, `kind`, `lat`, `lon`, `matched`), and with an `id` that one place. Clients still call `GET /v1/places?q=`. The API asks every plug-in that is on in the account and offers `places`, and labels each place with the plug-in that found it. A location set from a place records which plug-in and which place (`basis`), so the place's name and credit show with it.

### The protocol

HTTP/1.1 with JSON (multipart where images go), over a Unix socket in the plug-in's run folder that only Lumiverb can reach. Lumiverb asks; the plug-in answers:

| Request | What for |
|---------|----------|
| `GET /v1/health` | `ready` (asked as ready; it may say what it does in the background, with `done`, `total` and `detail`: a newer list fetched while the old one answers), `preparing` (with `done`, `total`, `detail`: shown as "N of M": fetching and building its data, which it does itself; nothing is asked of it meanwhile) or `failed` (with why: nothing is asked) |
| `POST /v1/work` | A batch of clips (ids that mean nothing outside the request), each with its declared inputs, and the account's settings: `{"settings": {...every setting, the server's too}, "clips": [{"id": "1", "location": {...}, ...}]}`. It answers per clip, `{"clips": {"1": {"outputs": {"near": {"text": "...", "url": "..."}, "at": null}}}}`, or an error that is the clip's (`{"error": {"whose": "clip", "message"}}`: charged, as a failure) or the plug-in's (`"whose": "plugin"`: nothing charged, the clips wait, as for an AI machine that's down). A clip it says nothing of, or outputs it didn't declare, are its fault: counted as a crash, the clip charged once it's happened three times in a row. Two minutes for the whole request |
| `POST /v1/settings/check` | Its own check of new settings, when it declares one |
| `POST /v1/lookups/<name>` | A lookup it offers, with the account's settings (a POST: a secret never goes in a URL). `places`: `{"q", "limit", "settings"}`, places whose name starts with q, or `{"id", "settings"}`, that one place; it answers `{"places": [{"id", "name", "region", "country", "kind", "matched", "lat", "lon", "population"}]}`. Three seconds: a person is waiting |

Every request has a time limit. Lumiverb checks every answer against the manifest (keys it declared, kinds and lengths) and refuses the rest, which is counted as the plug-in's error:

- **A fact:** `{"text", "url"?}` or null, for each output it declares and no other; its text plain (no control character, nothing like an HTML tag: `<` and a letter), 1 to 200 characters once its whitespace is collapsed; its link http(s), up to 2,000 characters. One wrong fact makes that clip's answer broken.
- **A place:** an `id` (text or a whole number) of up to 100 characters with no space, a `name` up to 200, `region` and `country` up to 100, `kind` up to 60, `matched` up to 200, each plain text (no control character, no `<` or `>`), whitespace collapsed; `lat` and `lon` a real point (within range, not 0, 0); `population` a whole number, 0 or more; at most 100 places. One wrong place makes the whole answer refused (the plug-in is "unavailable" for that search), so a plug-in checks what it keeps.

What it's handed, besides each request: its environment, clean of Lumiverb's settings, keys and database addresses:

| Variable | What |
|----------|------|
| `LUMIVERB_PLUGIN_SOCKET` | Where it listens |
| `LUMIVERB_PLUGIN_DATA` | Its own folder, the only one it should write (its `HOME` too); watched against its `disk_mb` |
| `LUMIVERB_PLUGIN_SETTINGS` | A JSON file, `{key: value}`, of every setting of its with the server's scope, secrets too (its default when the server admin hasn't chosen), for what it does before anything asks it anything (a mirror to download from); on disk in its folder, readable by Lumiverb's user alone (0600). It's started again when they change. Requests carry every setting as before |
| `LUMIVERB_PLUGIN_ID`, `LUMIVERB_PLUGIN_PROTOCOL` | Who it is, and the protocol (1) |
| `TMPDIR` | A folder of its own, emptied at each start |
| `PATH`, `LANG`, `LC_ALL`, `TZ` | As Lumiverb has them |
| `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` (and lowercase) | This machine's proxy to the internet, when the install has one |

It's run only while it's on somewhere: on by default, or turned on in some account. One on in no account isn't started (`off`), and is stopped when the last account turns it off, so a plug-in that downloads fetches nothing until an admin has turned it on and accepted what it asks (its license). The runner reads every account's switch on a thread of its own, giving up on a database that doesn't answer within a few seconds: an account whose database is away keeps what it said last, and holds up neither the other accounts' switches nor the runner's watching of the plug-ins and what it says they're doing. Protocol 1 gained these (the settings file, `ready`'s background work, `empty`, `credit.license`) in phase 3: a plug-in that uses none of them is unaffected, and a Lumiverb from before refuses a manifest declaring `empty` or `credit.license`.

### The runner (phase 1b)

`lumiverb-server plugins run` starts and watches every installed plug-in. On a Mac it's a child of `run`, beside the API and the scheduler (`supervise.py`'s restart rules). On Linux it's a unit of its own, `lumiverb-plugins.service`, written by `update.sh` as the scheduler's is. For each plug-in it:

- prepares its folders (`DATA_DIR/plugins/<id>/data/`, its own; its socket in `DATA_DIR/plugins/run/<id>.sock`, a folder only Lumiverb's user can reach; a data folder too deep for a socket's path, 104 bytes on a Mac, is reached through a short link in /tmp, this user's) and starts the manifest's `run` command from the installed copy of its folder, with a clean environment: its folders, its socket and its server settings (`LUMIVERB_PLUGIN_SOCKET`, `LUMIVERB_PLUGIN_DATA`, `LUMIVERB_PLUGIN_SETTINGS`), nothing of Lumiverb's; only while it's on somewhere (`off` otherwise), and again when its server settings change;
- waits for `ready` (`GET /v1/health`), restarts it when it exits (waiting longer each time, up to 10 minutes) and leaves it stopped, with why, when it keeps stopping soon after each start;
- sends its output to the service log a line at a time, named (`plugin geonames | …`), so Admin › Logs shows it;
- writes what each plug-in is doing (starting, running, preparing with how far, stopped and why) where the API reads it, for Settings › Plug-ins.

The API and the scheduler talk to it through its socket. While it's down, its clips wait (`plugin_down`, a new wait code: no silent waits) and its lookups say it's unavailable.

### Settings › Plug-ins (phase 1c): the stick in the ground

Built from the manifests, for Robert to try and change: each plug-in a card with its title, version, author and description; what it's doing now (starting, running, preparing N of M, stopped and why); what it reads ("Reads each clip's location"), what it downloads and from where, whether it uses the internet; its switch in this account (and, for an account following the default, that it does: "On for every account, unless turned off here"); its settings form, built from its schema, each field with its help text and the server's or the plug-in's error beside it. The server admin also sees its default and the server's own settings.

### Isolation: good enough (phase 1b)

Robert: "We don't need enterprise security. We're just doing a good enough job. I don't want them taking down the server." So the runner keeps a plug-in, however badly written, from hurting the server, on a Mac and on Linux alike:

| What could go wrong | What the runner does |
|---------------------|----------------------|
| It crashes, or exits | Only it stops; restarted, waiting longer each time; left stopped, with why, when it keeps stopping |
| It hangs, or answers slowly | Every request has a time limit; its clips wait, uncharged; a plug-in that stops answering its health check is restarted |
| It uses too much memory | Ended above its limit (the manifest's `memory_mb`, at most what the server allows; 1 GB unless it says): the runner watches its processes' memory on both platforms, and on Linux the unit's `MemoryMax` holds all of them |
| It uses the CPU | It runs at a lower priority than Lumiverb's API and scheduler (`nice`) |
| It fills the disk | A file it writes can't pass a size limit (`RLIMIT_FSIZE`), and its data folder is watched: stopped, with why, above its `disk_mb` |
| It reads Lumiverb's secrets | It's given none: a clean environment, no key to Lumiverb, nothing but its inputs |
| It answers nonsense | Checked against its manifest; refused and counted as its error |

What isn't held here is a plug-in written to do harm: it runs as Lumiverb's user and could read what that user can, and reach the network whatever its manifest says. The server admin installs only what they trust (decision 7).

### Confinement: maybe later

Lumiverb fetching a plug-in's downloads was there so a plug-in could run with no network at all; without confinement it buys little (Robert agreed, 2026-10-19), so it waits with confinement. A plug-in fetches its own data, and `downloads` and `internet` say what it does, as declarations.

Holding a plug-in to what its manifest says it reaches (no network unless it declares `internet`, no files outside its own folders) was to be phase 4. Robert, 2026-10-19: "Let's hold off on this - mark it as maybe in the future. Right now being admin-only installs addresses my core concern." So `internet` and the folders a plug-in may use are declarations, shown to the server admin before installing and accepted, and not enforced. If it's taken up, what was found:

- **On a Mac:** the runner would start each plug-in under a sandbox profile written from its manifest (`sandbox-exec`): no network unless declared (its Unix socket still works), reading its program, its data folder and the system's libraries but not Lumiverb's data folder, writing its own folder only. Tried on macOS 26.5 on 2026-10-18: it refused the network, reading Lumiverb's data folder and writing outside the plug-in's folder. Apple calls `sandbox-exec` deprecated, so a test would prove it each release.
- **On Linux, not the unit's own settings:** `lumiverb-plugins.service`'s (`IPAddressDeny`, `ProtectSystem` and the rest) hold every plug-in alike, so one couldn't have the network while another doesn't, and the runner in it reaches the database over TCP. It has to be per plug-in, from the launcher (`plugin_launch.py`), before the program starts and without root: a seccomp filter refusing internet sockets (any kernel; its Unix socket unaffected) and Landlock for its files (kernel 5.13 and later: reading its program and the system's libraries, writing its own folder).
- **Either way:** tests run a plug-in that tries to connect out, read Lumiverb's data folder and write outside its folder, refused each time on a Mac and in a Debian container; where the system can't do it (no Landlock, a macOS without the sandbox), plug-ins run as now and Settings › Plug-ins and the Admin page say the confinement is missing; one that declares `internet` keeps its file limits.

### Installing (phase 1a, then 5)

`lumiverb-server plugins install FOLDER --on|--off` runs on the machine, as `server-admins` does. It checks the manifest, shows it in words (what it reads, what it shows, what it downloads and from where, whether it uses the internet, its acknowledgment if it's to be on by default) and asks the server admin to accept. Then it copies the folder into the install (`DATA_DIR/plugins/<id>/program/`, read-only) and records it in the control database: which version, on or off by default, who accepted what and when. `--yes` is for no one to ask, having been shown it.

- `lumiverb-server plugins list` shows what's installed and its state; `default ID on|off` changes its default for the accounts that follow it; `uninstall ID` stops it and removes its program (its outputs, from phase 2, after the server admin is asked how many there are).
- Installing a newer version of a plug-in replaces it. One that reads or reaches more asks again; one that asks the same or less doesn't.
- Phase 5 adds installing from a URL with its checksum (`--sha256`), and the Admin page.
- Lumiverb's own plug-ins ship in the install (`plugins/<id>/`), installed already.

"Writing a plug-in" (docs, phase 5) is the protocol's whole contract, as cursor-api's "Writing a client" is a reader's. A test holds it to the code.

## Place names, rebuilt (phase 3)

GeoNames becomes a program in `plugins/geonames/` that imports nothing of Lumiverb's (a test holds that), with the manifest above:

- **Its data:** the same downloads, which it fetches itself into its folder (Python's standard library), saying how far through its health, from GeoNames or a server setting's mirror (tests point it at their own list: they never reach geonames.org). It builds its own database there (towns, landmarks, names in other languages), not in Lumiverb's control database, and fetches it again when it needs newer data, the old used meanwhile.
- **Its work:** given each clip's location and the radius, it answers `near` and `at`, as the producer and `repository/places.py` did: the same rules for which landmarks count and how far each kind reaches, moved into the plug-in. Both are searched (Robert, 2026-10-19: "people search by landmarks more than towns"; the plan had `near` alone, as ADR-017 decision 9 had left searching by place for later).
- **Its lookup:** `places`, as place search is now (the same tiers: exact names first, then other languages, then the start of a name).
- **What goes from Lumiverb:** `src/producers/place/`, `repository/places.py` and `asset_places.py`, `routers/places.py`'s GeoNames code, the `geonames_*` tables, `asset_place`, `src/plugins/geonames/`, and the web app's `geonames` checks. Each goes only once its replacement holds its data (keep until replaced). The 14,237 named clips on Robert's Mac are named again by the plug-in, which took about 45 seconds as a producer, and their old names show until then. A person's locations set from a place keep their place, recorded as the plug-in's.

## Build phases

Each part is its own PR, with its tests and docs, the whole suite passing, and a release.

### Phase 1a: the manifest and installing

- `src/plugins/manifest.py`: reads and checks `lumiverb-plugin.json` (every field above; settings by the same rules as producers', `Setting.check`), refusing a manifest with anything it doesn't know.
- The control database's installed plug-ins (version, manifest, on or off by default, who accepted what and when); `lumiverb-server plugins install|list|default|uninstall`.
- Every account's switch: an installed plug-in follows the default until an account's admin turns it on or off; `GET /v1/plugins` lists installed plug-ins beside the built-in one (GeoNames, unchanged until phase 3), from their manifests.
- A test plug-in (`tests/plugins/`), written against the protocol alone.

### Phase 1b: the runner

- `lumiverb-server plugins run`: each installed plug-in started, checked, restarted, held to its limits, logged, its state written for the API; a child of `run` on a Mac, `lumiverb-plugins.service` on Linux (`deploy-api.sh`, `update-api.sh`).
- Tests start real plug-in processes: one that crashes, one that hangs, one that takes too much memory, one that writes too much, one that answers what it didn't declare; the server goes on, and each says why.

### Phase 1c: Settings › Plug-ins

- The cards above; the settings form built from the schema; `PUT /v1/plugins/{id}/settings` checked by Lumiverb and, when it asks, by the plug-in; the server's settings for the server admin.
- An example plug-in to try it with (`plugins/example/`, every kind of setting), installable with one command.

## Edge cases

| Scenario | Behavior |
|----------|----------|
| The plug-in crashes or hangs | Its request times out; it's restarted, waiting longer each time; its clips wait (`plugin_down`), uncharged; Settings and Processing say so |
| It answers something it didn't declare, or too long, or HTML | Refused and counted as its error; never shown |
| It asks for an input Lumiverb doesn't define, or a protocol Lumiverb doesn't speak | Its manifest fails: not installed, and the server admin is told why |
| Two accounts with different settings | Each request carries its account's settings; nothing of one account reaches another's requests |
| A setting changes that remakes | The 409 `redo_on_change` question, as for a producer; remade after everything else |
| An account turns it off | Its outputs stay and are hidden; nothing more is made; turned on again, what's missing is made |
| Its data needs a newer download | The plug-in fetches it again itself, in the background; the old data is used until the new is ready |
| A download fails or the disk fills | The plug-in says `failed`, with why, and tries again itself; Settings › Plug-ins shows it (a full folder: the runner stops it, as phase 1b does) |
| An update reads more than before | Not started until the server admin accepts it |
| It reaches what it didn't declare (the internet, files outside its folder) | Not stopped: what it declares is shown and accepted, not enforced (confinement is maybe later). The server admin installs only what they trust |
| An account never chose, and the server admin changes the default | It follows the new default; an account whose admin chose keeps its choice |

## Code references

| Area | File | Notes |
|------|------|-------|
| ADR-021's plug-in | `src/plugins/contract.py`, `src/plugins/__init__.py`; `src/plugins/geonames/` gone | The spec is a manifest as an account's switch sees it (`Manifest.spec`); the registry gone (phase 3) |
| Account switch, acknowledgment | `src/server/repository/plugins.py`, `routers/plugins.py`, `pages/settings/PluginsSection.tsx` | Kept; reads manifests |
| Server-wide data | `src/server/plugin_data.py`, `repository/plugin_data.py` | Gone with GeoNames' Lumiverb code in phase 3: a plug-in fetches its own data |
| A plug-in's producer | `src/producers/contract.py` (`ProducerSpec.plugin`), `src/server/plugin_producers.py` | One generic producer for every plug-in's `work` (`src/producers/place/` gone) |
| Settings fields | `src/producers/contract.py` `Setting`, `ProcessingSection.tsx` | The schema's shape and the form's controls |
| GeoNames in Lumiverb | `src/own_plugins/geonames/` | Moved into the plug-in (phase 3): `repository/places.py`, `asset_places.py` and the routes' and pages' GeoNames code gone |
| A process Lumiverb runs and asks | `src/server/ai_worker/` (ADR-022), `lifecycle/supervise.py` | The host's model |

## Doc references

- `docs/cursor-api.md`: plug-in settings, lookups, `plugin_down`.
- `docs/architecture.md`: the plug-in host.
- New: `docs/writing-a-plugin.md` (phase 5).
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
- Confining a plug-in's network and files to what it declares, and Lumiverb fetching its downloads for it: maybe in the future ("Confinement: maybe later").

## Robert's answers (2026-10-18)

To the proposal's five questions:

1. Who installs: "Yes. And make them available to all libraries and allow the system admin to install them on or off by default to make one's config easier." (Decision 7.)
2. No network by default, Lumiverb fetching what a plug-in declares: "Yes. Safety and isolation first." (Decision 6.)
3. Facts and the place lookup only: "For now." (Decision 5.)
4. The Mac's sandbox: "Needs to work on Linux too. Process isolation? We don't need enterprise security. We're just doing a good enough job. I don't want them taking down the server." (Decision 9: process isolation and limits on both, the system's confinement later where it's cheap.)
5. Order: "Runner first with a ux stick in the ground that we can iterate on." (Phases 1a to 1c.)

Later, 2026-10-19, after asking how `internet` would be enforced: "Let's hold off on this - mark it as maybe in the future. Right now being admin-only installs addresses my core concern." Then, asked whether Lumiverb fetching downloads still matters without it: "agreed on moving to maybe later as well." (Phase 4 is deferred, both halves: "Confinement: maybe later".)

On phase 3's plan (`docs/adr/023-phase-3-plan.md` section 11), 2026-10-19: jumping straight to the removal, the gap is accepted (plain migrations); and `at` is searchable as well as `near`, "agreed that people search by landmarks more than towns".

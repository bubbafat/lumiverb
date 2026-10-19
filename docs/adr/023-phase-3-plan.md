# ADR-023 phase 3: GeoNames moved out (the plan)

## Status

Draft (2026-10-19), for the builder, the reviewer and the tester to agree before any code; then the coordinator's go. Robert approved phase 3. His words: "Getting plugins 'right' is critical if we're going to split out more." GeoNames is the proof that a real plug-in can be built on the protocol alone.

What's settled here is how. What only Robert can say is under "Questions for Robert", sent through the coordinator; the plan says what it does until he answers.

## Where it starts

Today GeoNames is Lumiverb's own code: ADR-021's registry entry (`src/plugins/geonames/`), its list in the control database (`geonames_places`, `geonames_landmarks`, `geonames_admin1`, `geonames_countries`, `geonames_names`), fetched by `src/server/plugin_data.py` into `plugin_data`; its queries (`repository/places.py`: `nearest`, `landmark_at`, `search`, `get`), its producer (`src/producers/place/`, artifact `place`, setting `radius_km`) saving into each account's `asset_place` through `POST /v1/places/nearest` and `/names`; `GET /v1/places`; `place_id` `geonames:<id>` on `PUT /v1/assets/locations` (basis `place.source` `geonames`, credited from `repository/locations.py` `CREDITS`); the detail's `place`, drawn by `LocationRow.tsx` ("At …", "Near …", "Not near a town"); `SetLocationModal.tsx`'s `geonames` checks.

On Robert's Mac it's on, with acknowledgment version 3 accepted, 14,237 clips named, and the data version 3 list (171,329 towns, about 72,000 landmarks, about 637,000 other names).

## 1. The plug-in program

### Where it lives and how it ships

- `src/own_plugins/geonames/`: Lumiverb's own plug-ins, one folder each, each a manifest and its program. Under `src/` because that's what both installs carry: the Homebrew formula installs `src` (its `RUNTIME`), and a Linux install is the checkout. A top-level `plugins/` would need the tap's formula changed and isn't on a Linux install's list of what it reads. `src/plugin_examples/example/` stays where it is (it's installed only when someone asks).
- No `__init__.py`: it isn't a Python package of Lumiverb's. Its files: `lumiverb-plugin.json`; `geonames.py` (the program: the socket, the protocol, the fetch thread); `fetch.py` (downloading and reading GeoNames' files: today's `parse_*` functions, moved); `places.py` (its database: building it and every query); `landmarks.py` (which kinds are landmarks and each kind's reach: today's `KINDS`, moved).
- It imports nothing of Lumiverb's. A test walks every module's imports (`ast`): each is in `sys.stdlib_module_names` or one of its own files. Nothing outside the standard library either.

### Language and runtime

- Python, the standard library only, run as `["python3", "geonames.py"]`: the machine's `python3`, as the example plug-in is. Decided deliberately:
  - Every install has one that's new enough. A Mac with Homebrew has the Command Line Tools (Homebrew needs them), whose `python3` is 3.9; with a Homebrew Python first on the service's PATH, that one. Debian 11 and later, Ubuntu 22.04 and later: 3.9 or newer.
  - So it's written for **Python 3.9 or later**: `from __future__ import annotations` (as the example), no `match`, no `X | Y` outside annotations, no `zip(strict=)`. A test parses every module with `ast.parse(..., feature_version=(3, 9))`; the end-to-end tests run it with the `python3` the runner would find. ruff gets a per-folder target of py39 if its rules would rewrite it for 3.12.
  - Not Lumiverb's own venv Python: a plug-in is a program of its own (decision 1), and putting Lumiverb's interpreter (with its packages, `src` among them) first on every plug-in's PATH would be one more thing to undo when confinement comes.
  - What it needs of the standard library is everywhere: `sqlite3` (SQLite 3.25 or later, for window functions: macOS's and Debian 11's are newer), `zipfile`, `urllib.request`, `http.server`, `socketserver`, `unicodedata`, `json`, `threading`.
  - One difference to know: Python 3.9's `unicodedata` is Unicode 13, 3.12's is 15. Folding (`fold`: NFKD, accents dropped, casefolded) is done by the plug-in's own Python for both the list and what's typed, so it agrees with itself; it can differ from today's only for characters added in Unicode 14 or 15.

### Its own data store

SQLite, in its data folder (`LUMIVERB_PLUGIN_DATA`): `places.sqlite`, the list in use, read-only once built. The same rows as today's tables, the same columns that are used:

| Table | Columns | Indexes |
|-------|---------|---------|
| `countries` | iso, name, folded | iso (key) |
| `regions` | code, name, folded | code (key) |
| `towns` | geonameid, name, ascii_lower, folded, lat, lon, feature_code, country_code, admin1_code, population | key; (lat, lon) for the towns that name a place (today's `TOWNS`: not PPLX, PPLH, PPLQ, PPLW, PPLCH: a partial index, as `ix_geonames_places_lat`); folded; ascii_lower |
| `landmarks` | geonameid, name, ascii_lower, folded, lat, lon, feature_code, country_code, admin1_code, known | key; (lat, lon); folded; ascii_lower |
| `names` | geonameid, name, folded, language | (geonameid, folded) key; folded |
| `meta` | key, value | data version, when it was built, from where, its counts |

How each answer is found fast, the same way Postgres finds it now:

- **Nearest town:** today's band (`_band`: a band of latitude around the point from the (lat, lon) index, then of longitude as wide in meters, both sides of the antimeridian), the same arithmetic, so the same candidates; each candidate's distance by the same haversine (a Python function registered with SQLite, `create_function(..., deterministic=True)`: double-precision `math`, which is what Postgres's `sin`, `cos`, `asin`, `radians` and `power` are), within the radius, nearest first, then the most populous, then the lowest id (Postgres leaves a full tie unordered; this makes it one).
- **The landmark it's at:** the same band for the farthest reach (1.5 km), each kind's reach, `distance <= reach AND reach >= sure_m`, ordered by distance over reach, then distance, then id (today's).
- **Search by name:** today's query, written for SQLite: prefixes as index ranges (`folded >= :p AND folded < :p || char(0x10FFFF)`, the same for `ascii_lower` and the other names: always an index range, never LIKE's escaping), the same tiers (exactly its own or English name 0, exactly another 1, its own or English name starts so 2, another 3), each place once (`ROW_NUMBER() OVER (PARTITION BY geonameid ORDER BY matched IS NOT NULL, tier, language = 'en' DESC, length(matched), matched)`, today's `DISTINCT ON`, with the minimum tier over the place: today's `best_tier`), "Name, where" narrowing to a region or country starting so or a country's code, ranked by best tier, population, towns before landmarks, the best known first, id. Where SQLite and Postgres order differently (NULLs in a descending sort; text compared by bytes rather than the database's collation, for two other names of a place the same length in the same tier), the parity tests below say which wins and the query is written to agree.
- **One place by id:** a town, else a landmark.
- **Labels and kinds:** today's `label` ("Port of Spain, Trinidad and Tobago": the name, its region unless the same, its country) and each landmark kind's noun.

### Fetching its own data

Robert deferred Lumiverb fetching downloads, so it fetches its own, as ADR-023 says. In detail:

- **When:** only while it's wanted. The runner starts a plug-in only while it's on somewhere (on by default, or turned on in an account: section 4), so GeoNames fetches nothing on an install where no account has turned it on: the acknowledgment (GeoNames' license) is accepted before anything is fetched, as ADR-021 promised ("never at install"). Started with no list, it fetches at once.
- **From where:** its server setting `mirror` (empty: `https://download.geonames.org/export/dump/`), the same five files as today: `cities1000.zip`, `admin1CodesASCII.txt`, `countryInfo.txt`, `alternateNamesV2.zip`, `allCountries.zip`. It learns its server settings at start (section 4: the runner hands them over), since nothing has asked it anything yet.
- **How:** `urllib.request`, each file's size asked first (HEAD) for the total, written into `data/building/`, refused past today's caps (`_MOST`: downloaded and unzipped sizes); the zips read as they're unzipped, never whole into memory; rows written into `data/building/places.sqlite` as they're read (not lists in memory: only the set of features with a Wikipedia article and each kept place's own folded names, as today). Then `ANALYZE`, the downloads deleted, and the new file put in place of the old in one rename. A start that finds `building/` left over deletes it and starts again.
- **How far:** its health says `preparing` with `done` and `total` (megabytes downloaded, then what it's reading: "Reading the landmarks", "Saving 171,329 towns and 72,272 landmarks, with their names in other languages"), so Settings › Plug-ins shows it as N of M.
- **Failing:** health `failed`, with why (the file and what went wrong); it tries again itself an hour later, or at once when it's started again (a changed mirror restarts it: section 4).
- **Newer data, the old used meanwhile:** its own data version (a constant in its code, in the database's `meta`). A release whose list differs raises it. Started with an older list, it answers place searches from the old one while it builds the new one beside it (health `ready`, with the background fetch's `detail`, `done` and `total`: section 4), and answers `/v1/work` with `{"error": {"whose": "plugin"}}` for every clip until the new list is in place (its clips wait, uncharged; named from this release's list, as the producer's wait for the current list does today). Such a release raises `work.version` too, so what was named is remade, after the hold an admin resumes (ADR-018).
- **Disk and memory, set from a real run:** at its peak the data folder holds the downloads (about 610 MB), the list being built and the list in use (each estimated at 150 to 250 MB). Part 2 builds it once from GeoNames' real files (a one-off run by hand on the builder's machine, never a test: see "Questions") and sets `disk_mb` and `memory_mb` from what it measured, with room for GeoNames' files growing; until then the manifest says 2,000 MB and 1,024 MB (the ADR's). The biggest single file (`allCountries.zip`, 403 MB) is well under `disk_mb`, which is also its file-size limit (`RLIMIT_FSIZE`). On Linux, `lumiverb-plugins.service`'s `MemoryMax=4G` holds every plug-in together; GeoNames' 1 GB fits beside the example's 128 MB.

### Its answers

| Request | What it does |
|---------|--------------|
| `GET /v1/health` | `ready`; `preparing` (done, total, detail); `failed` (error); `ready` with detail, done and total while it fetches newer data |
| `POST /v1/work` | Each clip's `location` (`lat`, `lon`, `sure_m`): the nearest town within `settings.radius_km` and the landmark it's at, as `near` and `at` (below). A clip whose answer can't be worked out (a bug) is `{"error": {"whose": "plugin"}}`: it waits rather than being charged |
| `POST /v1/lookups/places` | `{"q", "limit"}`: search; `{"id"}`: that place. Each place `{"id": "<geonameid>", "name", "region", "country", "kind", "matched", "lat", "lon", "population"}`: Lumiverb makes it `geonames:<geonameid>` with today's label, so a place's id and label are the same as today's |

What `at` and `near` say, as `LocationRow.tsx` says them today:

- `at`: the landmark's name when a town is near ("Magic Kingdom"), else its label, which says where ("Magic Kingdom, Florida, United States"); null at no landmark.
- `near`: the town's label ("Horizon West, Florida, United States"); null with no town within the radius. Its declared empty line says so ("No town nearby": section 4), in place of today's "Not near a town".

It answers within the protocol's limits: a lookup in 3 seconds (part 2 measures the slowest, a one-letter search on the real list), a batch of 200 clips in two minutes.

## 2. Parity: the same answers as today

Proven by tests that run today's code and the plug-in on the same lists:

1. **Side by side (parts 2 and 3).** A test serves its own small GeoNames files (zips it builds, as `tests/test_geonames.py` does), has today's fetch put them in Postgres and the plug-in build its SQLite from the same files, then asks both the same questions and compares every field:
   - `nearest` at points chosen for the edges: in towns, between two, at exactly the radius and just past it, sections of towns (PPLX) and historical places that must be skipped, both sides of the antimeridian, near the poles (the band's own quirk kept), equal distances (the population decides);
   - `landmark_at` at each reach (250 m, 500 m, 1 km, 1.5 km) and just past it, with `sure_m` under, at and over the reach, two landmarks whose distance over reach ties;
   - `search` for exact names, prefixes, accents and none ("zurich", "Zürich", "Zuerich"), other languages ("Eiffel Tower" finds Tour Eiffel, `matched`), English first where two languages share a name, a name in several languages kept once, "Name, region" and "Name, country code", LIKE's own characters (`%`, `_`, `\`), an empty name, a limit;
   - `get` for a town, a landmark and an id that's neither; labels, kinds, `known`, populations, counts.
2. **The facts against the Lightbox's lines.** For the same clips, the plug-in's `at` and `near` against what `LocationRow.tsx` draws from today's `place` (a table of cases: town and landmark, landmark alone, town alone, neither).
3. **A golden file that outlives the old code.** Part 2 checks in what today's code answers for those questions on those lists (`tests/plugins/geonames_golden.json`, written by a script run against today's code). The plug-in is held to it in every part, so parity is still proven once part 4 has removed the old code.
4. **One source of the kinds.** Until part 4, a test holds the plug-in's `landmarks.py` `KINDS` to Lumiverb's (each code's noun, reach and `every_one`).
5. **On the real list, once (not a test).** Part 2's real run compares the two on GeoNames' real files: 20,000 points (random, and Robert-like: cities, parks, the sea) and 5,000 searches (prefixes of real names in many languages), every difference listed in the PR. Expected: none, or only the documented ties.

## 3. Its manifest

```json
{
  "id": "geonames",
  "version": "1",
  "protocol": 1,
  "title": "Place names (GeoNames)",
  "description": "Find a place by name when setting a location, and see the nearest town and the landmark each photo or video was taken at. Lumiverb keeps a list of the world's towns of 1,000 people or more and its landmarks on this server, from GeoNames.",
  "author": "Lumiverb",
  "homepage": "https://www.geonames.org/",
  "run": ["python3", "geonames.py"],
  "acknowledgment": {"version": "3", "reaccept_from": "1", "title": "GeoNames: Creative Commons Attribution 4.0",
                     "text": "(today's version 3, word for word)", "links": ["(today's three)"]},
  "credit": {"text": "GeoNames", "url": "https://www.geonames.org/",
             "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}},
  "downloads": [
    {"url": "https://download.geonames.org/export/dump/cities1000.zip", "about_mb": 11, "title": "Towns of 1,000 people or more"},
    {"url": "https://download.geonames.org/export/dump/admin1CodesASCII.txt", "about_mb": 1, "title": "Regions"},
    {"url": "https://download.geonames.org/export/dump/countryInfo.txt", "about_mb": 1, "title": "Countries"},
    {"url": "https://download.geonames.org/export/dump/alternateNamesV2.zip", "about_mb": 195, "title": "Names in other languages, and which places have a Wikipedia article"},
    {"url": "https://download.geonames.org/export/dump/allCountries.zip", "about_mb": 403, "title": "Every place, for its landmarks"}
  ],
  "internet": false,
  "memory_mb": 1024,
  "disk_mb": 2000,
  "settings": [
    {"key": "radius_km", "type": "int", "default": 25, "minimum": 1, "maximum": 200, "unit": "km", "remakes": true,
     "label": "Nearest town within", "help": "How far away a town can be and still name where a clip was."},
    {"key": "mirror", "type": "text", "scope": "server", "default": "", "pattern": "^(https?://\\S+)?$", "max_length": 500,
     "advanced": true, "label": "Download from",
     "help": "A copy of download.geonames.org/export/dump/ to fetch the lists from, if this server can't reach it. Empty: GeoNames itself."}
  ],
  "work": {
    "title": "Place names", "version": "1", "media": ["image", "video"], "inputs": ["location"], "batch": 200,
    "outputs": [
      {"key": "at", "kind": "fact", "label": "At"},
      {"key": "near", "kind": "fact", "label": "Near", "searchable": true, "empty": "No town nearby"}
    ]
  },
  "lookups": ["places"]
}
```

- `version` 1: its first release as a program. `work.version` 1: a new producer (`plugin_geonames`), so every located clip is due once, not held.
- The acknowledgment is today's version 3, unchanged: every acceptance stands (`reaccept_from` 1) and nobody is asked again. What it says is still true: nothing is fetched until it's turned on, about 600 MB, kept on this server, nothing about anyone's photos sent.
- `credit.license` and `outputs[].empty` are new to the manifest, for any plug-in (section 4).
- The mirror takes `http://` as well as `https://`, as `LUMIVERB_GEONAMES_URL` does today (a mirror on the local network), and is how tests serve their own lists.
- `radius_km` keeps today's default and bounds; its help loses "Not near a town" (that's the empty line now).

## 4. What Lumiverb doesn't have yet (part 1, for every plug-in)

GeoNames needs a few things no plug-in has needed. Each is general: nothing in it names GeoNames.

| What | Why GeoNames needs it | The change |
|------|----------------------|------------|
| A plug-in runs only while it's on somewhere | It comes installed on every install, but must fetch nothing until an admin has accepted its license (ADR-021's "never at install") | The runner starts an installed plug-in only while it's on by default or on in some account (each account's `plugins` row, `state_of` with its default, read with what's installed); one on nowhere isn't started, and is stopped when the last account turns it off. Settings › Plug-ins: "Not running: it's off in every account". Its outputs stay (as ADR-023 says of turning off) |
| Its server settings at start | It fetches before anything has asked it anything, from the mirror the server admin chose | The runner writes its server-scope settings (each value, its default when not chosen) to a file in its own folder (`DATA_DIR/plugins/<id>/server-settings.json`, mode 0600, outside its data folder) and names it in `LUMIVERB_PLUGIN_SETTINGS`; when the server admin changes them, it's started again with the new ones. Requests carry every setting as now |
| `ready` with work in the background | It answers searches from the old list while it fetches a newer one | Health `ready` may carry `detail`, `done` and `total`; the runner keeps them; Settings shows "Running · Fetching a newer place list (120 of 610)" |
| An output's empty line | Today's "Not near a town" | An output may declare `empty` (plain text, up to 80 characters): shown under its label when its value is null, never searched, never stored. ADR-023's outputs table already describes it ("shown as the output's empty line if it declares one") |
| A credit's license | CC BY 4.0 asks for a link to the license beside the credit, and Set location's only license link today is `SetLocationModal.tsx`'s `geonames` check | `credit.license` (`text`, `url`), shown beside the credit in the Lightbox's facts and Set location's places (`credits[].license`) |
| Proxy settings handed on | Today's fetch runs in the API, whose HTTP client honours the env file's `HTTPS_PROXY`; a plug-in's clean environment drops it | The runner hands each plug-in the standard proxy variables when set (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, and lowercase), and nothing else more |

The example plug-in and the test plug-ins use each where it fits (the example declares an empty line; `tests/plugins/facts/` a credit with a license), so each is tested without GeoNames. Docs: ADR-023's protocol section, cursor-api's Plug-ins API, CLAUDE.md's row.

## 5. Install and switch-over, keeping what's there until it's replaced

Robert's rule: existing data stays until its replacement is saved. Part 3 does the switch:

- **It comes installed.** The plug-in runner, at its start (on a Mac a child of `run`, on Linux `lumiverb-plugins.service`: both start again after every upgrade), installs Lumiverb's own plug-ins from `src/own_plugins/` when one isn't installed or the shipped version is newer: the same checks and copy as `lumiverb-server plugins install`, recorded as by `lumiverb`, its permissions accepted (the server admin accepted Lumiverb by installing it). A new one is installed **off by default**; an update keeps the default the server admin chose. One the server admin uninstalled stays uninstalled (its last log entry says so); `lumiverb-server plugins install <the shipped folder>` puts it back. `plugin_install.plan`'s refusal of ids in ADR-021's registry stays until the registry goes (part 4).
- **Its id stays `geonames`**, and its ADR-021 registry entry goes in the same release, so `plugin_catalog` finds the installed one. Each account's `plugins` row (enabled, `acknowledged_version` 1, 2 or 3) and every `plugin_acknowledgments` row carry over untouched: with today's acknowledgment (version 3, `reaccept_from` 1), an account that had it on has it on, and one that had it off, or never chose, has it off (off by default).
- **Its settings carry over.** An account's chosen `radius_km` for the `place` producer (`lineage.overrides`) becomes its `radius_km` for the plug-in (`plugin_settings`); a pause of `place` (`producer_pauses`) becomes a pause of `plugin_geonames`; an install's `LUMIVERB_GEONAMES_URL`, when it isn't GeoNames', becomes the plug-in's `mirror` (unless the server admin set one). Done once, by the step that installs it.
- **The old producer stops.** `src/producers/place/` and `POST /v1/places/nearest` and `/names` go (410 `gone`, naming the plug-in's work), so nothing names clips twice. `located` and `SHOWS_LOCATION` move into `repository/clip_inputs.py`, their only remaining users.
- **Clips keep today's names until the plug-in's are saved.** The detail's `place` (from `asset_place`, nothing written to it any more) is shown while GeoNames is on in the account and the plug-in hasn't saved its outputs for the clip; once it has, the facts show and `place` is null. Nothing shows both, and a clip shows today's name until its new one is there. On Robert's Mac, his 14,237 clips are named again once the list is fetched (the producer took under a minute for them).
- **Place search keeps working.** `GET /v1/places` asks the plug-in through the lookup (place ids `geonames:<geonameid>` as today, `source` `geonames`, `matched`, `kind`, `credits` with the license). While the plug-in can't answer yet (fetching its first list), and today's list is still in the control database, the search answers from today's list, as it does now; the same for a `geonames:` id on `PUT /v1/assets/locations`. So search has no gap while the first list is fetched. That fallback goes in part 4.
- **Locations set from a place keep resolving.** A person's location whose basis is `{"place": {"id": "geonames:<id>", "label", "source": "geonames"}}` is untouched; its credit comes from the installed plug-in's manifest (`repository/locations.py` `credit`, which already does this for installed plug-ins), the same text and link as `CREDITS` gives now.
- **GET /v1/places stays compatible.** The same query and place fields. Changes clients see: places are ordered by plug-in title (GeoNames' after the example's, where the example is on); `credit` (GeoNames' alone) is null, `credits` has the license instead; with GeoNames the only plug-in on and preparing, the answer is its `unavailable` entry rather than 503 `places_not_ready` (as with any plug-in). The web app is the only client using it (the Mac and iOS apps don't).

## 6. What a person sees

- **The Lightbox.** "At …" and "Near …" move from under Location into the plug-in's own section, "Place names (GeoNames)", as every plug-in's facts show (Robert accepted it): "At · Magic Kingdom", "Near · Horizon West, Florida, United States", "From GeoNames · CC BY 4.0". With no town near: "Near · No town nearby". Under Location, a place picked by name still says its name with its credit; the section may say the same place again (today's row hid that repetition): we think it reads fine as two sections, so no hint is proposed; the reviewer may disagree.
- Two small differences: a clip whose location was just moved shows its old name until it's named again (seconds to minutes), where today it shows none meanwhile (ADR-023's rule for every plug-in); and a landmark with no town near now has "Near · No town nearby" under it, where today's row left it out.
- **Search** finds clips by the town they're near (`near` is searchable, as ADR-023's manifest has it). Not today: ADR-017 decision 9 left searching by place name for later (a heads-up below).
- **Set location.** Place search as now, from one generic path: "Places from GeoNames, CC BY 4.0" (today: "Place names from GeoNames, CC BY 4.0"). While its first list is fetched, today's list answers.
- **Settings › Plug-ins.** GeoNames' card becomes an installed one's, like the example's: version, "Reads each clip's location", what it downloads and from where, "Doesn't use the internet", its state (preparing N of M while it fetches), its `radius_km` (moved from Settings › Processing) and, for the server admin, the mirror. Its switch and its acknowledgment work as now.
- **Processing.** The old "Place names" row goes; the plug-in's "Place names" row comes, "Part of the Place names (GeoNames) plug-in", waiting (`plugin_down`: preparing its data) until its list is there, then naming every located clip once.
- **Admin › Logs.** Its lines, named `plugin geonames | …`.

## 7. Removal (part 4)

Once part 3 has run (the plug-in's list fetched, its names saved), these go, each with its tests and docs:

- `src/plugins/geonames/` (the fetch, the old `KINDS`), `src/server/plugin_data.py`, `repository/plugin_data.py`, upkeep's `plugin_data`, `PluginSpec`'s `data*` fields, GET /v1/plugins' `data` (and the web's code for it);
- `repository/places.py`, `repository/asset_places.py`, `routers/places.py`'s fallback (GET /v1/places becomes the plug-ins' lookups alone), `routers/locations.py`'s `geonames:` branch, `repository/locations.py` `CREDITS`;
- the detail's `place`, `AssetPlace` and `LocationRow.tsx`'s place lines; `SetLocationModal.tsx`'s last `geonames` mention, if any; config's `lumiverb_geonames_url`;
- ADR-021's registry (`src/plugins/__init__.py` `registry()`, `plugin()`, `kind: "built_in"`), being empty; `PluginSpec` stays, as the account switch's view of a manifest;
- what's left of the old producer in each account: its lineage rows, settings and pauses for `place`;
- migrations: the control database's `geonames_*` and `plugin_data` dropped; each account's `asset_place` dropped. The 410s for `/v1/places/nearest` and `/names` stay a while, as the API's other `gone` routes do.

`tests/test_geonames.py` and `tests/test_place_producer.py` go with the code they test; what they proved of the answers is the golden file's and the plug-in's own tests by then.

On an install that steps through the releases, nothing is removed before its replacement holds it. One that jumps from before part 3 to part 4 or later (a Linux install updating after a while) has no fallback: no place names on clips and no Place search until the plug-in has fetched its list and named them (minutes), and nothing a person made is lost (their locations and places stay). See "Questions for Robert".

## 8. Parts

Each is its own PR from a freshly fetched main (no stacked PRs), each with its tests and docs, the whole suite passing, and a release of its own. The next starts once the last is merged.

| Part | What | Robert's Mac after it | A Linux install after it |
|------|------|-----------------------|--------------------------|
| 1 | Section 4: what a plug-in needs (runs only while on somewhere, its server settings at start, `ready` with background work, outputs' empty line, a credit's license, proxy settings handed on), with the example and the test plug-ins using them | GeoNames unchanged. The example plug-in, if installed and on in no account, shows "Not running: it's off in every account" | The same |
| 2 | The plug-in program in `src/own_plugins/geonames/`, its tests, the parity tests and the golden file; the real-data run's numbers in the PR | Nothing changes: it ships but isn't installed (the built-in GeoNames is still the one) | The same |
| 3 | Switch-over (section 5) | On upgrade: GeoNames is the installed plug-in, still on; it downloads about 600 MB from GeoNames and builds its list (minutes; about 1 GB used meanwhile in `DATA_DIR/plugins/geonames/data`), today's names and search used meanwhile; then its 14,237 clips named again in about a minute and the names show in the Lightbox's Place names section | The same, from the mirror its `LUMIVERB_GEONAMES_URL` named, if any |
| 4 | Removal (section 7) | Nothing changes that he sees; the control database shrinks by the old list | The same; if it skipped part 3, the gap in section 7 |

Parts 1 and 2 are independent of each other's code but the program's manifest uses part 1's fields, so part 1 comes first.

## 9. Tests

All call the real code: the real program over its socket, the real runner, the real API with testcontainers Postgres. None asserts how fast anything is (part 2's real run reports timings; it doesn't test them).

- **Never geonames.org.** Every test that runs the plug-in serves its own lists on 127.0.0.1 and sets the mirror. Behind that, two guards: conftest gives every process a test starts a closed HTTPS proxy (`HTTPS_PROXY` to a port nothing listens on, `NO_PROXY` for 127.0.0.1 and localhost), which the runner hands to plug-ins (part 1), so even a plug-in started without a mirror can't reach `download.geonames.org`; and in parts 3 and 4, conftest's `LUMIVERB_GEONAMES_URL` (`*.invalid`) becomes the mirror of every GeoNames the switch-over installs in a test. A test proves the first guard: the plug-in started with no mirror fails with the proxy's error.
- **Part 1:** the runner starts a plug-in only while it's on somewhere and stops it when the last account turns it off (real processes); its server settings file (values, defaults, 0600) and its restart when they change; `ready` with detail shown; the empty line shown and never searched; a credit's license shown and listed; proxy variables handed on, nothing else of the environment.
- **Part 2:** the program end to end with the machine's `python3`: preparing with N of M, then ready; a restart reuses the list (the test's server sees no request); a list of an older data version used for searches while the new one is built, work waiting meanwhile; a failed download (a missing file, one too big, a broken zip) says why and tries again when restarted; `disk_mb` and `memory_mb` honoured on the test lists; its answers through `/v1/work` and `/v1/lookups/places`; the imports test; the Python 3.9 grammar test; the parity tests and the golden file (section 2).
- **Part 3:** installing Lumiverb's own (fresh; newer; the same version again changes nothing; the server admin's default and an uninstall kept); an account on with acknowledgment 1, 2 or 3 stays on, one off stays off; `radius_km`, a pause and `LUMIVERB_GEONAMES_URL` carried over; the old producer and its routes gone (410); a clip shows `place` until the plug-in's outputs are saved, then the facts; GET /v1/places and `PUT /v1/assets/locations` with `geonames:<id>` through the plug-in, and through today's list while it prepares; an old person's location keeps its credit; `tests/test_no_silent_waits.py` and `tests/test_added_producers.py` with it; the web's Set location and facts without any `geonames` check (vitest).
- **Part 4:** the migrations (up and down where the repo's migrations go down); the golden file still passing with the old code gone; nothing in `src/` but `src/own_plugins/geonames/` names GeoNames (a test).
- **The tester's:** independent acceptance and parity tests of their own, run on a real Linux machine, against each part.

## 10. Risks

- **The real list's numbers.** Disk, memory, build time and the slowest search are estimates until part 2's real run. If a one-letter search on the real list is near the 3-second limit, search gets a materialized prefix table rather than a looser limit.
- **A Mac's `python3`.** The Command Line Tools' Python 3.9 verifying GeoNames' certificate: part 2's real run uses it, on a Mac without a Homebrew `python3` first on PATH, to be sure.
- **Unicode versions** (section 1): folding can differ from today's for characters newer than Unicode 13 when the machine's `python3` is 3.9. Rare in place names; the golden file's lists include none.
- **Ties** Postgres leaves unordered (two towns at exactly the same distance with the same population) are ordered by id in the plug-in: the parity tests don't count those as differences, and say so.
- **Running only while on somewhere** changes the runner for every plug-in (part 1): a plug-in installed off with no account on is no longer running, so its own settings check doesn't run until it's on (settings are saved with `unchecked` saying so, as when it's down).
- **A jump straight to part 4** (section 7).

## 11. Questions for Robert (through the coordinator)

The plan does what's written above until he says otherwise.

1. **Jumping straight to the removal.** An install that upgrades from before part 3 straight to part 4 or later would have no place names or Place search for the minutes the plug-in takes to fetch and name, with nothing a person made lost. Plain migrations accept that (recommended: his Mac steps through every release). The alternative, dropping the old tables only once the plug-in says its replacement is there, keeps the old code (search and names from the old tables) in Lumiverb for as long as any install hasn't got there. Is the gap acceptable?
2. **Searching by place.** ADR-023's manifest makes `near` searchable, so search finds clips by the town they're near (ADR-017 decision 9 had left that for later). Should `at` be searchable too (a search for "Magic Kingdom" finding the clips taken there)? The plan makes only `near` searchable, as the ADR does.
3. **A one-off real run.** To set `disk_mb` and `memory_mb` from fact and compare the old and new answers on the real list, the builder would download GeoNames' real files (about 610 MB, once) from geonames.org on the machine this session runs on, outside any test and outside his Lumiverb install, into a throwaway folder, and build both lists there (today's in a throwaway Postgres). OK? Without it, the limits stay estimates and parity is proven on the test lists only.

## Agreement

| Who | Says |
|-----|------|
| geonames-builder | Proposed |
| geonames-reviewer | |
| geonames-tester | |

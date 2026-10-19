# ADR-023 phase 3: GeoNames moved out (the plan)

## Status

Agreed (2026-10-19) by the builder, the reviewer and the tester, and built in four parts (part 4, the removal, with Robert's answers to section 11 folded in). Drafted for the three to agree before any code; then the coordinator's go. Robert approved phase 3. His words: "Getting plugins 'right' is critical if we're going to split out more." GeoNames is the proof that a real plug-in can be built on the protocol alone.

What's settled here is how. What only Robert could say is under "Questions for Robert", with his answers.

## Where it starts

Today GeoNames is Lumiverb's own code: ADR-021's registry entry (`src/plugins/geonames/`), its list in the control database (`geonames_places`, `geonames_landmarks`, `geonames_admin1`, `geonames_countries`, `geonames_names`), fetched by `src/server/plugin_data.py` into `plugin_data`; its queries (`repository/places.py`: `nearest`, `landmark_at`, `search`, `get`), its producer (`src/producers/place/`, artifact `place`, setting `radius_km`) saving into each account's `asset_place` through `POST /v1/places/nearest` and `/names`; `GET /v1/places`; `place_id` `geonames:<id>` on `PUT /v1/assets/locations` (basis `place.source` `geonames`, credited from `repository/locations.py` `CREDITS`); the detail's `place`, drawn by `LocationRow.tsx` ("At …", "Near …", "Not near a town"); `SetLocationModal.tsx`'s `geonames` checks.

On Robert's Mac it's on, with acknowledgment version 3 accepted, 14,237 clips named, and the data version 3 list (171,329 towns, about 72,000 landmarks, about 637,000 other names).

## 1. The plug-in program

### Where it lives and how it ships

- `src/own_plugins/geonames/`: Lumiverb's own plug-ins, one folder each, each a manifest and its program. Under `src/` because that's what both installs carry: the Homebrew formula installs `src` (its `RUNTIME`), and a Linux install is the checkout. A top-level `plugins/` would need the tap's formula changed and isn't on a Linux install's list of what it reads. `src/plugin_examples/example/` stays where it is (it's installed only when someone asks).
- No `__init__.py`: it isn't a Python package of Lumiverb's. Its files: `lumiverb-plugin.json`; `geonames.py` (the program: the socket, the protocol, the fetch thread); `fetch.py` (downloading and reading GeoNames' files: today's `parse_*` functions, moved); `places.py` (its database: building it and every query); `landmarks.py` (which kinds are landmarks and each kind's reach: today's `KINDS`, moved).
- It imports nothing of Lumiverb's. A test walks every module's imports (`ast`): each is in `sys.stdlib_module_names` or one of its own files. Nothing outside the standard library either.

### Language and runtime

- Python, the standard library only, run as `["python3", "-I", "geonames.py"]`: the machine's `python3`, as the example plug-in is, isolated from any `PYTHON*` variable and user site (`-I`; the program puts its own folder on its path itself, as `-I` leaves it off). Decided deliberately:
  - Every install has one that's new enough. A Mac with Homebrew has the Command Line Tools (Homebrew needs them), whose `/usr/bin/python3` is 3.9 (3.9.6 here); with a Homebrew Python first on the service's PATH, that one (3.14 here). A Linux install has the distro's (`deploy-api.sh` installs `python3-dev` and runs `python3` itself): Debian 12's 3.11, Ubuntu 22.04's 3.10.
  - So it's written for **Python 3.9 or later**: `from __future__ import annotations` (as the example), no `match`, no `X | Y` outside annotations, nothing of the standard library newer than 3.9 (`zip(strict=)`, `dataclass(slots=)`, `int.bit_count`, `itertools.pairwise` are 3.10, `datetime.UTC` 3.11, `sqlite3`'s `autocommit` 3.12). A test parses every module with `ast.parse(..., feature_version=(3, 9))`, which catches grammar only, so the end-to-end tests are run under each distinct interpreter found: the `python3` first on PATH, `/usr/bin/python3`, and an installed 3.9 (`uv python find 3.9`: one installed by hand with `uv python install 3.9`; the suite never downloads one). On this Mac that's 3.14.8 (Homebrew's, likely what a Mac's service finds) and 3.9.6 (the Command Line Tools', a real 3.9); in the tester's Debian container, 3.11 and uv's 3.9. ruff gets a per-folder target of py39 if its rules would rewrite it for 3.12.
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
- **How:** `urllib.request`, each file's size and date asked first (HEAD: `Content-Length`, `Last-Modified`) for the total, written into `data/building/` as `<name>.part` and renamed once it's all there (its size what the server said), with what HEAD said kept beside it; refused past today's caps (`_MOST`: downloaded and unzipped sizes); the zips read as they're unzipped, never whole into memory; rows written into `data/building/places.sqlite` as they're read (not lists in memory: only the set of features with a Wikipedia article and each kept place's own folded names, as today). Then `ANALYZE`, the downloads deleted, and the new file put in place of the old in one rename.
- **A restart never downloads a finished file again.** The runner restarts a plug-in for a memory overrun, three missed health checks, an upgrade or a changed mirror, and one up longer than its `STAYED_UP_SEC` isn't counted as failing, so it's restarted every time. A start that finds `building/` keeps each download that finished and whose `Content-Length` and `Last-Modified` the server still gives (a `.part`, or one the server now gives otherwise, is fetched again) and builds the database again; the list in use is never touched until the rename. A build that's stopped three times in a row on the same downloads (killed for its memory, say) isn't tried a fourth: health says `failed` with why, and it tries again an hour later or when it's started with other settings, so it can't churn.
- **While it builds it answers.** The build is a thread of its own; health, lookups and work are answered by the server's other threads (a threading server, each request with its own read-only SQLite connection, opened on the list in use: one opened before a swap reads the old, unlinked file to its end, the next opens the new one; that holds because the list is never in WAL mode, built in the default `journal_mode=DELETE` and read with `?mode=ro&immutable=1`, so no `-wal` or `-shm` found by name can pair an old file with a new one: the tester runs lookups across a swap on the real list), so health answers within the runner's 5 seconds throughout, and the first within its 60 seconds of starting. Stopped mid-build (SIGTERM), the list in use is whole; the build starts again.
- **Everything it keeps passes Lumiverb's checks**, so one odd name can't make a whole search unavailable (`checked_place` refuses the whole answer) or a clip broken (`checked_outputs`). At the build it takes out of every name what Lumiverb refuses (control characters, `<`, `>`; none in today's list, the tester found) and leaves out a place whose name is then empty or whose point isn't a real one (`is_fix`: within range, not 0, 0); what it answers is cut, at a word with "…", to Lumiverb's lengths (a name 200 characters, a region or country 100, a kind 60, a fact 200). Whitespace is kept as GeoNames has it (Lumiverb collapses it). The tester counted today's real list: no name, region, country or other name with a control character, `<` or `>`, the longest label 154 characters. Tests build lists with such names (in the name, region, country and other names) and points (0, 0; out of range), and a search over them still answers through `GET /v1/places`.
- **How far:** its health says `preparing` with `done` and `total` (megabytes downloaded, then what it's reading: "Reading the landmarks", "Saving 171,329 towns and 72,272 landmarks, with their names in other languages"), so Settings › Plug-ins shows it as N of M.
- **Failing:** health `failed`, with why (the file and what went wrong); it tries again itself an hour later, or at once when it's started again (a changed mirror restarts it: section 4).
- **Newer data, the old used meanwhile:** its own data version (a constant in its code, in the database's `meta`). A release whose list differs raises it. Started with an older list, it answers place searches from the old one while it builds the new one beside it (health `ready`, with the background fetch's `detail`, `done` and `total`: section 4), and answers `/v1/work` with `{"error": {"whose": "plugin"}}` for every clip until the new list is in place (its clips wait, uncharged; named from this release's list, as the producer's wait for the current list does today). Such a release raises `work.version` too, so what was named is remade, after the hold an admin resumes (ADR-018).
- **Disk and memory, set from a real run:** at its peak the data folder holds the downloads (638 MB on 2026-10-19), the list being built and the list in use (today's tables are 190 MB with their indexes in Postgres; SQLite's should be about that). So about 1 GB at the peak, and the list alone after. Today's fetch peaks at 378 MB resident (the tester's baseline). Part 2 builds it from GeoNames' real files (the tester's copy, already downloaded, read-only, in the builder's scratch area: never in a test, never near Robert's install) and sets `disk_mb` and `memory_mb` from what it measured, with room for GeoNames' files growing; until then the manifest says 2,000 MB and 1,024 MB (the ADR's). The biggest single file (`allCountries.zip`, 422 MB) is well under `disk_mb`, which is also its file-size limit (`RLIMIT_FSIZE`). On Linux, `lumiverb-plugins.service`'s `MemoryMax=4G` holds every plug-in together; GeoNames' 1 GB fits beside the example's 128 MB.

### Its answers

| Request | What it does |
|---------|--------------|
| `GET /v1/health` | `ready`; `preparing` (done, total, detail); `failed` (error); `ready` with detail, done and total while it fetches newer data |
| `POST /v1/work` | Each clip's `location` (`lat`, `lon`, `sure_m`): the nearest town within `settings.radius_km` and the landmark it's at, as `near` and `at` (below). A clip whose answer can't be worked out (an exception on that clip) is `{"error": {"whose": "clip"}}` with its message: charged, shown in Failures with Retry, never held forever. `whose: plugin` only for "no list yet" and "a newer list being built" |
| `POST /v1/lookups/places` | `{"q", "limit"}`: search; `{"id"}`: that place. Each place `{"id": "<geonameid>", "name", "region", "country", "kind", "matched", "lat", "lon", "population"}`: Lumiverb makes it `geonames:<geonameid>` with today's label, so a place's id and label are the same as today's |

What `at` and `near` say, as `LocationRow.tsx` says them today:

- `at`: the landmark's name when a town is near ("Magic Kingdom"), else its label, which says where ("Magic Kingdom, Florida, United States"); null at no landmark.
- `near`: the town's label ("Horizon West, Florida, United States"); null with no town within the radius. Its declared empty line says so in today's words, "Not near a town" (section 4).
- A fact is at most 200 characters (`manifest.FACT_MAX`; more and the clip's answer is refused as broken): a longer label is cut at a word, with "…". A test has a place with a very long name.

It answers within the protocol's limits: a lookup in 3 seconds (part 2 measures the slowest, a one-letter search on the real list), a batch of 200 clips in two minutes.

## 2. Parity: the same answers as today

Proven by tests that run today's code and the plug-in on the same lists:

1. **Side by side (parts 2 and 3).** A test serves its own small GeoNames files (zips it builds, as `tests/test_geonames.py` does), has today's fetch put them in Postgres and the plug-in build its SQLite from the same files, then asks both the same questions and compares every field:
   - `nearest` at points chosen for the edges: in towns, between two, at exactly the radius and just past it, sections of towns (PPLX) and historical places that must be skipped, both sides of the antimeridian, near the poles (the band's own quirk kept), equal distances (the population decides);
   - `landmark_at` at each reach (250 m, 500 m, 1 km, 1.5 km) and just past it, with `sure_m` under, at and over the reach, two landmarks whose distance over reach ties;
   - `search` for exact names, prefixes, accents and none ("zurich", "Zürich", "Zuerich"), other languages ("Eiffel Tower" finds Tour Eiffel, `matched`), English first where two languages share a name, a name in several languages kept once, "Name, region" and "Name, country code", LIKE's own characters (`%`, `_`, `\`), an empty name, a limit;
   - `get` for a town, a landmark and an id that's neither; labels, kinds, `known`, populations, counts.
   Asked of the store's own functions and again through Lumiverb (`GET /v1/places` and the clip detail's `facts` against today's `GET /v1/places` and `place`), since Lumiverb's checks build the label and collapse whitespace themselves.
   Compared exactly (ids, order, names, `matched`, labels, kinds, populations), except distances, within 1 m (both are double-precision haversine, but a platform's libm can differ in the last bit), and full ties Postgres leaves unordered (two towns at the same point with the same population), where either answer passes and the test says so. The plug-in keeps names as GeoNames has them, so its own answers equal today's to the character; Lumiverb's checks of what a plug-in answers collapse whitespace (`plugin_lookups._text`, `checked_outputs`), which changes the 11 real names with doubled or edge spaces (10 landmarks, 1 town: the tester's count) as a page shows them anyway, so a comparison through the API collapses whitespace on both sides and says why.
2. **The facts against the Lightbox's lines.** For the same clips, the plug-in's `at` and `near` against what `LocationRow.tsx` draws from today's `place` (a table of cases: town and landmark, landmark alone, town alone, neither).
3. **A golden file that outlives the old code.** Part 2 checks in what today's code answers for those questions on those lists (`tests/plugins/geonames_golden.json`, written by the script committed beside it, `tests/plugins/geonames_golden.py`, run against today's code: it can be written again while the old code exists). The plug-in is held to it in every part, so parity is still proven once part 4 has removed the old code.
4. **One source of the kinds.** Until part 4, a test holds the plug-in's `landmarks.py` `KINDS` to Lumiverb's (each code's noun, reach and `every_one`).
5. **On the real list (not a test).** The tester already has today's answers on GeoNames' real files (7,614 points at radii of 1, 5, 25 and 200 km with landmarks at `sure_m` 0 to 2,000, and 1,751 searches, from a Postgres with locale C as a Mac's cluster is), and serves the files on 127.0.0.1 for this phase. The plug-in, built from them with that as its mirror, is compared with those answers on the builder's machine for part 2's PR (under 3.9 and 3.14), and in the tester's Debian container, every difference listed. Expected: none, or only the documented ties.

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
  "run": ["python3", "-I", "geonames.py"],
  "acknowledgment": {"version": "3", "reaccept_from": "1", "title": "GeoNames: Creative Commons Attribution 4.0",
                     "text": "(today's version 3, word for word)", "links": ["(today's three)"]},
  "credit": {"text": "GeoNames", "url": "https://www.geonames.org/",
             "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}},
  "downloads": [
    {"url": "https://download.geonames.org/export/dump/cities1000.zip", "about_mb": 11, "title": "Towns of 1,000 people or more"},
    {"url": "https://download.geonames.org/export/dump/admin1CodesASCII.txt", "about_mb": 1, "title": "Regions"},
    {"url": "https://download.geonames.org/export/dump/countryInfo.txt", "about_mb": 1, "title": "Countries"},
    {"url": "https://download.geonames.org/export/dump/alternateNamesV2.zip", "about_mb": 205, "title": "Names in other languages, and which places have a Wikipedia article"},
    {"url": "https://download.geonames.org/export/dump/allCountries.zip", "about_mb": 422, "title": "Every place, for its landmarks"}
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
      {"key": "at", "kind": "fact", "label": "At", "searchable": true},
      {"key": "near", "kind": "fact", "label": "Near", "searchable": true, "empty": "Not near a town"}
    ]
  },
  "lookups": ["places"]
}
```

- `version` 1: its first release as a program. `work.version` 1: a new producer (`plugin_geonames`), so every located clip is due once, not held. `version` 2 (part 4) searches `at` too (Robert's answer to question 2): `work.version` stays 1, so nothing is named again, and the search sweep writes the text of the clips named before again from their facts (`plugin_outputs.searched_again`, general: any plug-in's newer version that searches other outputs).
- The acknowledgment is today's version 3, unchanged: every acceptance stands (`reaccept_from` 1) and nobody is asked again. What it says is still true: nothing is fetched until it's turned on, about 600 MB, kept on this server, nothing about anyone's photos sent.
- `credit.license` and `outputs[].empty` are new to the manifest, for any plug-in (section 4).
- The mirror takes `http://` as well as `https://`, as `LUMIVERB_GEONAMES_URL` does today (a mirror on the local network), and is how tests serve their own lists.
- `radius_km` keeps today's default and bounds; its help loses "Not near a town" (that's the empty line now).

## 4. What Lumiverb doesn't have yet (part 1, for every plug-in)

GeoNames needs a few things no plug-in has needed. Each is general: nothing in it names GeoNames.

| What | Why GeoNames needs it | The change |
|------|----------------------|------------|
| A plug-in runs only while it's on somewhere | It comes installed on every install, but must fetch nothing until an admin has accepted its license (ADR-021's "never at install") | The runner starts an installed plug-in only while it's on by default or on in some account (each account's `plugins` row, `state_of` with its default, read with what's installed every `LIST_EVERY_SEC`: a query in each account's database every 10 seconds, fine at today's few accounts); one on nowhere isn't started, and is stopped when the last account turns it off. Settings › Plug-ins: "Not running: it's off in every account". Its outputs stay (as ADR-023 says of turning off) |
| Its server settings at start | It fetches before anything has asked it anything, from the mirror the server admin chose | The runner writes its server-scope settings (each value, its default when not chosen) to a file in its own folder (`DATA_DIR/plugins/<id>/server-settings.json`, mode 0600, outside its data folder) and names it in `LUMIVERB_PLUGIN_SETTINGS`; when the server admin changes them, it's started again with the new ones. Requests carry every setting as now |
| `ready` with work in the background | It answers searches from the old list while it fetches a newer one | Health `ready` may carry `detail`, `done` and `total`; the runner keeps them; Settings shows "Running · Fetching a newer place list (120 of 610)" |
| An output's empty line | Today's "Not near a town" | An output may declare `empty` (plain text, up to 80 characters): shown under its label when its value is null, never searched, never stored. ADR-023's outputs table already describes it ("shown as the output's empty line if it declares one") |
| A credit's license | CC BY 4.0 asks for a link to the license beside the credit, and Set location's only license link today is `SetLocationModal.tsx`'s `geonames` check | `credit.license` (`text`, `url`), shown beside the credit in the Lightbox's facts and Set location's places (`credits[].license`) |
| Proxy settings handed on | Today's fetch runs in the API, whose HTTP client honours the env file's `HTTPS_PROXY`; a plug-in's clean environment drops it | The runner hands each plug-in the standard proxy variables when set (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, and lowercase), and nothing else more |
| Facts only while the clip has their inputs | A clip whose location a person clears would keep "Near Port of Spain", shown and searchable, for good: nothing makes it again (the clip no longer applies) and nothing deletes it. Today it's hidden (`asset_places.for_asset`) | A plug-in's facts are shown (`facts_of`) and searched (`text_search.facts`) only while the clip is one of its media and still has every input its work declares (`clip_inputs.present_sql`): kept, as a turned-off plug-in's are, and shown again if the input comes back (then it's made again anyway). A moved location still shows the old fact until the new one is saved, as ADR-023 says. Tested with `tests/plugins/facts/`: the input cleared, the fact gone from the detail and from search |
| Rows of a producer this server no longer has | Part 3 removes the `place` producer; an uninstalled plug-in's producer leaves the same. `GET /v1/producers/failures` looks up `PRODUCERS[artifact]` for every failed row (`routers/producers.py:908`) and `lineage.failures` doesn't filter, so one failed `place` row is a 500; the same at `producers.py:340, 790, 1000, 1016` and in `producer_waits.py` | Every list, retry, run and remake leaves out (or refuses, 404, by name) rows of a producer this server doesn't have now; tested with an uninstalled plug-in's lineage (ok, empty, failed), settings and a pause |

All of it is additive within **protocol 1**, which stays 1: a plug-in that uses none of it is unaffected, and a manifest that declares `empty` or `credit.license` is refused by a Lumiverb from before this part (it refuses fields it doesn't know), which is right. ADR-023's protocol section is brought up to everything a plug-in relies on, so a stranger could write GeoNames from it: the environment (`LUMIVERB_PLUGIN_SOCKET`, `_DATA`, `_ID`, `_PROTOCOL`, `_SETTINGS` and its JSON, the proxy variables, nothing else), health's states with `ready`'s detail, `empty` and `credit.license`, and every check Lumiverb makes of an answer (plain text with no control character, `<` or `>`; a place's id up to 100 characters with no space, its name 200, region and country 100, kind 60, matched 200, a real point (not 0, 0), at most 100 places; a fact's text 1 to 200 characters with no tag, its link http(s) up to 2,000). The example plug-in and the test plug-ins use each where it fits (the example declares an empty line; `tests/plugins/facts/` a credit with a license), so each is tested without GeoNames. Docs: ADR-023's protocol section, cursor-api's Plug-ins API, CLAUDE.md's row.

## 5. Install and switch-over, keeping what's there until it's replaced

Robert's rule: existing data stays until its replacement is saved. Part 3 does the switch:

- **It comes installed, before the API serves.** Lumiverb's own plug-ins are installed from `src/own_plugins/` (`src/server/own_plugins.py` `install_own`) when one isn't installed or the shipped version is newer, as the last step of migrating: a Mac's `run` migrates before it starts the API, the scheduler and the runner (`lumiverb-server migrate` the same), and a Linux update runs `migrate.sh` as the service user with the services stopped (`update-api.sh`; `deploy-api.sh` too), which runs the same step. The runner runs it again at its start (nothing to do, usually). It holds a control-database advisory lock while it copies and records, so a migrate run by hand and `run`'s own can't both install. On Linux, `migrate.sh` runs (`update-api.sh:169`) before the update makes and hands over `DATA_DIR/plugins` (`:341-345`), so `install_own` makes the folder itself, as the service user it runs as; tried in Debian both ways, an upgrade from before plug-ins and a fresh deploy. The same checks and copy as `lumiverb-server plugins install`, recorded as by `lumiverb`. A failed install is loud and doesn't stop the server: an error in the log and in a Linux update's output, recorded in `plugin_install_log` with why, and an Admin page row ("Lumiverb's own plug-ins: GeoNames couldn't be installed: …"); it's tried again at the next start, and meanwhile part 3's fallbacks (below) keep GeoNames' names and search as they were. This doesn't wait for the runner, which a Linux update restarts only once the API answers (`update-api.sh:489-491`). A Linux install whose env file names no `DATA_DIR` has no runner at all (`update-api.sh:386-387` warns): `install_own` skips it and says why (in the log and the Admin page's row), the fallbacks keep GeoNames as today through part 3, and from part 3 the update's warning says Place names stops working at the removal unless `DATA_DIR` is set. What it reads and reaches is accepted with Lumiverb itself (the server admin accepted Lumiverb's own by installing or upgrading Lumiverb), so a newer version that reads more asks nobody and the log says so. A new one is installed **off by default**; an update keeps the default the server admin chose (on by default with an acknowledgment that must be accepted again: the accounts following it are `waiting`, as ADR-021's rule says). One the server admin uninstalled stays uninstalled (its last log entry says so); `lumiverb-server plugins install <the shipped folder>` puts it back. `plugin_install.plan`'s refusal of ids in ADR-021's registry stays until the registry goes (part 4).
- **Its id stays `geonames`**, and its ADR-021 registry entry goes in the same release, so `plugin_catalog` finds the installed one. Each account's `plugins` row (enabled, `acknowledged_version` 1, 2 or 3) and every `plugin_acknowledgments` row carry over untouched: with today's acknowledgment (version 3, `reaccept_from` 1), an account that had it on has it on, and one that had it off, or never chose, has it off (off by default).
- **Its settings carry over.** An account's chosen `radius_km` for the `place` producer (`lineage.overrides`) becomes its `radius_km` for the plug-in (`plugin_settings`), so nothing is named at 25 km that was named at another; a pause of `place` (`producer_pauses`) becomes a pause of `plugin_geonames`. A tenant migration, before anything else of this release runs. An install's `LUMIVERB_GEONAMES_URL`, when it isn't GeoNames', becomes the plug-in's `mirror` (unless the server admin set one), by `install_own` when it first installs it.
- **The old producer stops.** `src/producers/place/` and `POST /v1/places/nearest` and `/names` go (410 `gone`, naming the plug-in's work), so nothing names clips twice. `located` and `SHOWS_LOCATION` move into `repository/clip_inputs.py`, their only remaining users. What it leaves in each account goes in the same migration, once its radius and pause are carried over: its lineage rows, failures, `keep_made`, settings and pauses for `place` (nothing reads them once it's gone; tests hold GET /v1/producers, the failures list and retry, and Processing to working after). `asset_place` itself stays (below).
- **Part 3's fallbacks don't depend on the plug-in being installed.** Whether GeoNames was on in an account is the account's own `plugins` row (enabled, and its acceptance standing under the acknowledgment in the shipped manifest, read from `src/own_plugins/geonames/lumiverb-plugin.json`), not the installed plug-in: so before the install, after a failed one, with the runner never started or the plug-in still preparing, the three below behave as today. Tested with the runner never started and the plug-in not installed.
- **Clips keep today's names until the plug-in's are saved.** The detail's `place` (from `asset_place`, nothing written to it any more) is shown while the account's row has GeoNames on and the plug-in hasn't saved its outputs for the clip (and, as today, while the clip shows the location it was named from); once it has, the facts show and `place` is null. Nothing shows both, and a clip shows today's name until its new one is there. On Robert's Mac, his 14,237 clips are named again once the list is fetched (the producer took under a minute for them).
- **Place search keeps working.** `GET /v1/places` asks the plug-in through the lookup (place ids `geonames:<geonameid>` as today, `source` `geonames`, `matched`, `kind`, `credits` with the license). While the plug-in can't answer (not installed, not running, or fetching its first list) for an account whose row has GeoNames on, and today's list is still in the control database, the search answers from today's list, as it does now; the same for a `geonames:` id on `PUT /v1/assets/locations`. So search has no gap while the first list is fetched. That fallback goes in part 4.
- **Locations set from a place keep resolving, and keep their credit.** A person's location whose basis is `{"place": {"id": "geonames:<id>", "label", "source": "geonames"}}` keeps its place and label. Its credit is kept with it from now on (general): setting a location from any plug-in's place stores that plug-in's credit, its license too, in `basis.place.credit`, and `repository/locations.py` `credit` reads it there first, so a place's name keeps its credit if the plug-in is later uninstalled or not installed yet (the license asks for it wherever the name shows). The same migration adds GeoNames' credit and license to every existing `basis.place` whose source is `geonames` (written beside what's there; nothing a person chose changes). `LocationRow.tsx` shows the license beside the credit too.
- **GET /v1/places stays compatible.** The same query and place fields. Changes clients see: places are ordered by plug-in title (GeoNames' after the example's, where the example is on); `credit` (GeoNames' alone) is null, `credits` has the license instead (`SetLocationModal.tsx`'s fallback to `credit` goes); once the fallback has gone (part 4), GeoNames the only plug-in on and preparing is its `unavailable` entry rather than 503 `places_not_ready` (as with any plug-in). All of it in cursor-api.md. The web app is the only client using it (the Mac and iOS apps don't).

## 6. What a person sees

- **The Lightbox.** "At …" and "Near …" move from under Location into the plug-in's own section, "Place names (GeoNames)", as every plug-in's facts show (Robert accepted it): "At · Magic Kingdom", "Near · Horizon West, Florida, United States", "From GeoNames · CC BY 4.0". With no town near: "Near · Not near a town". Under Location, a place picked by name still says its name with its credit and license; the section may say the same place again (today's row hid that repetition): it reads fine as two sections (the reviewer agrees), so no hint is proposed.
- Two small differences: a clip whose location was just moved shows its old name until it's named again (seconds to minutes), where today it shows none meanwhile (ADR-023's rule for every plug-in; a cleared location shows none, as today: section 4); and a landmark with no town near now has "Near · Not near a town" under it, where today's row left it out.
- **Search** finds clips by the town they're near and the landmark they're at (`near` and `at` are searchable; Robert, question 2: `at` too). Not today: ADR-017 decision 9 left searching by place name for later.
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
- migrations: the control database's `geonames_*` and `plugin_data` dropped; each account's `asset_place` dropped. The 410s for `/v1/places/nearest` and `/names` stay a while, as the API's other `gone` routes do.

`tests/test_geonames.py` and `tests/test_place_producer.py` go with the code they test; what they proved of the answers is the golden file's and the plug-in's own tests by then.

As built: what part 3 left for the switch-over goes too: `lumiverb-server plugins`' count of clips still showing an old name, config's `lumiverb_geonames_url` (an install's `LUMIVERB_GEONAMES_URL` still becomes the plug-in's mirror when Lumiverb first installs it, said in the plug-in's own folder, `lumiverb-own.json`, so Lumiverb's code names no plug-in's variable), and the tests that compared the plug-in with the code removed (the golden file holds it now; `tests/plugins/geonames_golden.py`, which wrote it, goes). `GET /v1/plugins`' `kind` is always `installed`.

**When part 4 may ship.** Not only once part 3 is out: on Robert's Mac and on the tester's throwaway install, the plug-in says `ready` and no clip still shows an old name. Part 3's `lumiverb-server plugins` prints that count for each account (a line removed in part 4), the coordinator reading it on Robert's Mac:

```sql
-- clips whose detail still shows asset_place's name (part 3's rule): GeoNames on by the account's row,
-- the clip showing the location it was named from, and no output of the plug-in's saved for it
SELECT count(*) FROM asset_place ap
JOIN active_assets a ON a.asset_id = ap.asset_id
LEFT JOIN asset_location l ON l.asset_id = ap.asset_id
WHERE <the account's row has geonames on>
  AND ap.lat = <located lat> AND ap.lon = <located lon>
  AND NOT EXISTS (SELECT 1 FROM plugin_outputs po WHERE po.asset_id = ap.asset_id AND po.plugin_id = 'geonames');
```

An install where the plug-in never managed to fetch (its mirror unreachable, say) has the same gap at part 4 as one that skipped part 3.

On an install that steps through the releases, nothing is removed before its replacement holds it. What the drop takes, wherever it runs before the plug-in has named every clip:

- **A clip not named by the plug-in yet** (the runner down, a download failing, a jump from before part 3: below) loses today's name until the plug-in names it. A place name is derived (from the clip's location and GeoNames' list): the plug-in makes it again by itself as soon as it can run, and nothing a person made is lost (their locations, their places and their places' credit stay).
- **An account with GeoNames off** loses its hidden old names (hidden since part 3, as any turned-off plug-in's facts are); turned on again, the plug-in names its clips.
- **Place search** answers from the plug-in alone: while it can't (its first list still being fetched), Set location says so and its other ways (a point, the same place as another clip) work. As built, Set location always offers Place (the reviewer's note on part 3: a failed install hid it), and says why it finds nothing: `GET /v1/places`' 409 `plugin_off` names the plug-ins installed that would (none: the server admin installs one), and `unavailable` one that can't answer now.
- **An install with no plug-in runner** (a Linux env file without `DATA_DIR`) has no GeoNames from part 4: no place names on clips, no Place search, until its env file names `DATA_DIR` and it's updated again (the update has warned since part 3). People's locations and their places' labels and credits stay.

An install that steps through every release meets none of these but the first, and only while the runner is down. One that jumps from before part 3 to part 4 or later (a Linux install updating after a while) has no fallback: no place names on clips and no Place search until the plug-in has fetched its list and named them (minutes). See "Questions for Robert".

## 8. Parts

Each is its own PR from a freshly fetched main (no stacked PRs), each with its tests and docs, the whole suite passing, and a release of its own. The next starts once the last is merged.

| Part | What | Robert's Mac after it | A Linux install after it |
|------|------|-----------------------|--------------------------|
| 1 | Section 4: what a plug-in needs (runs only while on somewhere, its server settings at start, `ready` with background work, outputs' empty line, a credit's license, proxy settings handed on, facts only while the clip has their inputs), with the example and the test plug-ins using them | GeoNames unchanged. The example plug-in, if installed and on in no account, shows "Not running: it's off in every account" | The same |
| 2 | The plug-in program in `src/own_plugins/geonames/`, its tests, the parity tests and the golden file; the real-data run's numbers in the PR | Nothing changes: it ships but isn't installed (the built-in GeoNames is still the one) | The same |
| 3 | Switch-over (section 5) | On upgrade: GeoNames is the installed plug-in, still on; it downloads about 600 MB from GeoNames and builds its list (minutes; about 1 GB used meanwhile in `DATA_DIR/plugins/geonames/data`), today's names and search used meanwhile; then its 14,237 clips named again in about a minute and the names show in the Lightbox's Place names section | The same, from the mirror its `LUMIVERB_GEONAMES_URL` named, if any |
| 4 | Removal (section 7) | Nothing changes that he sees; the control database shrinks by the old list | The same; if it skipped part 3, the gap in section 7; with no `DATA_DIR` (no runner), no GeoNames until it's set |

Parts 1 and 2 are independent of each other's code but the program's manifest uses part 1's fields, so part 1 comes first.

## 9. Tests

All call the real code: the real program over its socket, the real runner, the real API with testcontainers Postgres. None asserts how fast anything is (part 2's real run reports timings; it doesn't test them).

- **Never geonames.org.** Every test that runs the plug-in serves its own lists on 127.0.0.1 and sets the mirror. Behind that, two guards: every plug-in a test starts (through the runner, or the GeoNames tests' fixture) gets closed proxies (`HTTPS_PROXY` and `HTTP_PROXY` to a port nothing listens on, `NO_PROXY` for 127.0.0.1 and localhost: an `http://` mirror on another host is covered too), handed on as the runner hands on proxies (part 1), so even a plug-in started without a mirror can't reach `download.geonames.org`; only the plug-ins' processes get them, never the test process (whose own HTTP clients and the opt-in `ai` tests' model downloads they'd break); and in parts 3 and 4, conftest's `LUMIVERB_GEONAMES_URL` (`*.invalid`) becomes the mirror of every GeoNames the switch-over installs in a test. A test proves the first guard: the plug-in started with no mirror fails with the proxy's error.
- **Part 1:** the runner starts a plug-in only while it's on somewhere and stops it when the last account turns it off (real processes); its server settings file (values, defaults, 0600) and its restart when they change; `ready` with detail shown; the empty line shown and never searched; a credit's license shown and listed; proxy variables handed on, nothing else of the environment; a fact hidden and not found by search once its clip's location is cleared, shown again when it's back; an uninstalled plug-in's lineage (ok, empty, failed), settings and pause leaving Processing, failures, retry, Run and Remake working.
- **Part 2:** the program end to end with the machine's `python3` (and `/usr/bin/python3` where that's another), run as the runner runs it (`-I`, a clean environment): preparing with N of M, then ready; a restart reuses the list (the test's server sees no request); a restart mid-download keeps the finished files (the server sees each asked for once) and never takes a `.part` as whole; stopped mid-build, the list in use whole; health answered throughout a build; a list of an older data version used for searches while the new one is built, work waiting meanwhile; a failed download (a missing file, one too big, a broken zip) says why and tries again when restarted; `disk_mb` and `memory_mb` honoured on the test lists; a lookup answered during a 200-clip batch; its answers through `/v1/work` and `/v1/lookups/places`; a very long name cut to 200 characters; the imports test; the Python 3.9 grammar test; the parity tests and the golden file (section 2).
- **Part 3:** installing Lumiverb's own (fresh; newer; the same version again changes nothing; the server admin's default and an uninstall kept); installed but on in no account, it fetches nothing (the test's mirror counts no request), and the first account turning it on starts the fetch; an account on with acknowledgment 1, 2 or 3 stays on without being asked, one off or that never chose stays off; `radius_km` and a pause carried over by the tenant migration, `LUMIVERB_GEONAMES_URL` by the first install, each once: an admin's later `radius_km` or the server admin's mirror isn't overwritten at the next start, and `LUMIVERB_GEONAMES_URL` never replaces a mirror the server admin chose; an account seeded as Robert's is (GeoNames on with acknowledgment 3, `place` lineage ok, empty and failed, a `radius_km` override, a pause, `asset_place` names, a person's location set from a place) upgraded: Processing, failures, retry, Run and Remake working, the old producer and its routes gone (410); with the runner never started and the plug-in not installed, the old names, search and set-by-place all still working; a failed own install said in the log, `plugin_install_log` and the Admin page, the server up; a person's place keeping its credit with the plug-in uninstalled; a clip shows `place` until the plug-in's outputs are saved, then the facts; GET /v1/places and `PUT /v1/assets/locations` with `geonames:<id>` through the plug-in, and through today's list while it prepares; an old person's location keeps its credit; `tests/test_no_silent_waits.py` and `tests/test_added_producers.py` with it; the web's Set location and facts without any `geonames` check (vitest).
- **Part 4:** the migrations (up and down where the repo's migrations go down); the golden file still passing with the old code gone; nothing in `src/` but `src/own_plugins/geonames/` names GeoNames (a test).
- **The tester's:** independent acceptance tests of their own (`tests/test_geonames_plugin_acceptance.py`, through the protocol and the API), a throwaway install on this Mac (the plug-in fetching from the tester's mirror, killed and restarted by the runner, on nowhere fetching nothing; at part 3, an upgrade from the release before, the old names kept until replaced), the real-list comparison and limits at part 2, and each part's suite in a Debian 12 container.

## 10. Risks

- **The real list's numbers.** Disk, memory, build time, the slowest search and the slowest health answer during the build (it must stay under 5 s) are estimates until part 2's real run, which records each (peak resident memory, peak data folder, wall time). If a one-letter search on the real list is near the 3-second limit, search gets a materialized prefix table rather than a looser limit.
- ~~A Mac's `python3` and GeoNames' certificate~~: the tester checked it by hand (the Command Line Tools' 3.9.6, LibreSSL 2.8.3: HEAD 200 from download.geonames.org).
- **Linux is tested in a Debian 12 container**, not a Linux machine: the runner, the plug-in under Debian's `python3`, the suite, `test_deploy_scripts` and `test_update_script`. The unit's sandboxing (`ProtectSystem=strict`, `PrivateTmp`, `MemoryMax`) is read, not run.
- **Unicode versions** (section 1): folding can differ from today's for characters newer than Unicode 13 when the machine's `python3` is 3.9. Rare in place names; the golden file's lists include none.
- **Ties** Postgres leaves unordered (two towns at exactly the same distance with the same population) are ordered by id in the plug-in: the parity tests don't count those as differences, and say so.
- **Running only while on somewhere** changes the runner for every plug-in (part 1): a plug-in installed off with no account on is no longer running, so its own settings check doesn't run until it's on (settings are saved with `unchecked` saying so, as when it's down).
- **A jump straight to part 4** (section 7).

## 11. Questions for Robert (through the coordinator)

The plan does what's written above until he says otherwise.

1. **Jumping straight to the removal.** *Robert, 2026-10-19: the gap is accepted; plain migrations, as recommended.* An install that upgrades from before part 3 straight to part 4 or later would have no place names or Place search for the minutes the plug-in takes to fetch and name, with nothing a person made lost. Plain migrations accept that (recommended: his Mac steps through every release). The alternative, dropping the old tables only once the plug-in says its replacement is there, keeps the old code (search and names from the old tables) in Lumiverb for as long as any install hasn't got there. Is the gap acceptable?
2. **Searching by place.** *Robert, 2026-10-19: `at` is searchable too, "agreed that people search by landmarks more than towns" (part 4).* ADR-023's manifest makes `near` searchable, so search finds clips by the town they're near (ADR-017 decision 9 had left that for later). Should `at` be searchable too (a search for "Magic Kingdom" finding the clips taken there)? The plan makes only `near` searchable, as the ADR does.
3. ~~A one-off real run~~ Answered (the coordinator, 2026-10-19): yes, from the tester's copy of GeoNames' real files, read-only, in the builder's scratch area; no download again, never in a test, never near Robert's install.

Robert answered questions 1 and 2 on 2026-10-19 (through the coordinator); part 4 builds both.

## Agreement

| Who | Says |
|-----|------|
| geonames-builder | Proposed |
| geonames-reviewer | AGREE (on 8f251ab2), reservations folded in after it (text only): a runnerless Linux install named in §7 and the Admin row; `install_own` makes `DATA_DIR/plugins` itself on Linux (both an upgrade and a fresh deploy tried); `install_own` under an advisory lock |
| geonames-tester | AGREE (on 7c33ed9d), its reservations folded in: every `python3` tested, the golden file's script committed, carry-over done once, a closed `HTTP_PROXY` too, whitespace in names, the list never in WAL mode |

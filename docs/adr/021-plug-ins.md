# ADR-021: Plug-ins, and what turning one on asks

## Status

Accepted (2026-10-18, Robert). Phase 1 first: one plug-in, place names from GeoNames, until Robert has tried the UI.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | The mechanism: the registry, each account's switch, the acknowledgment and its record, the API, the CLI, Settings › Plug-ins | Built: `src/plugins/`, `repository/plugins.py`, `routers/plugins.py`, `lumiverb plugins`, `pages/settings/PluginsSection.tsx`; every turn-on asks (not only the first) |
| 2 | Place names (GeoNames): the place list downloaded once for the server; place search when setting a location (ADR-017 phase 4, its first part) | Built: `src/plugins/geonames/data.py`, `src/server/plugin_data.py`, `GET /v1/places`, `place_id` on `PUT /v1/assets/locations`, Set location's Place, the GeoNames credit beside a place's name |
| 2b | Place names' producer: each clip with a location named with its nearest town (Robert, 2026-10-18: "Shouldn't this be a processor as well? A plugin processor?") | Built: `src/producers/place/` (`plugin="geonames"`), `asset_place`, `POST /v1/places/nearest`, `POST /v1/places/names`, the detail's `place`, the Lightbox's "Near …" |
| 3 | Robert tries the UI; his feedback | Waiting on 1 and 2 |
| 4 | Other plug-ins distilled from what Lumiverb already has (faces, transcripts, descriptions, location guesses, …) | Not until phase 3 |

## Overview

Robert, 2026-10-18: "I think we first need to build a mechanism by which plug-ins are enabled in the UI. They can provide information that allows the user to see licensing information like this, and which provides two options: an OK button which acknowledges the licensing, or a cancel button which turns the feature back off. The vast majority of plug-ins would not use this, but for plug-ins like this it's the opportunity for us to display the Creative Commons information in a very explicit way. It also opens the door to future opportunities for people to have to make other acknowledgments that might be important."

Then: "Let's start with just making GeoNames a plug-in and don't do the other plug-ins until we've had an opportunity to manually test the UI and provide feedback. But generally, I agree … there are other plug-ins that could be distilled from the existing system."

ADR-016 already says producers become plug-ins. This ADR starts with what every plug-in needs, whatever it does: a switch, and what turning it on asks.

## Decisions (with Robert, 2026-10-18)

| # | Question | Decision |
|---|----------|----------|
| 1 | What is a plug-in? | An optional part of Lumiverb, declared in code as a producer is (a registry), that an account turns on or off. Built in only: code from outside Lumiverb is a later ADR of its own (and a security review). |
| 2 | Which plug-ins now? | One: **Place names**, from GeoNames. No existing feature moves to a plug-in until Robert has tried the UI. |
| 3 | Who turns one on? | An account admin, for their account. What a plug-in keeps for the whole server (GeoNames' place list) is fetched once, when the first account turns it on. |
| 4 | What can a plug-in ask? | An **acknowledgment**, if it declares one: a title, the text, links (a license, a source) and a version. Turning the plug-in on shows it in full; **OK** turns it on and records who accepted which version and when; **Cancel** turns the switch back off and records nothing. Most plug-ins declare none and simply turn on. |
| 5 | The text changes in a new release? | Its version goes up. It's accepted again only when the plug-in says so (Robert, 2026-10-18: "if not accepted yet or if the plugin says 'must re-accept'"): a new license, say, but not new wording under the same one. When it says so, the plug-in is off (`waiting`) in every account that accepted an earlier version until an admin accepts the new text; Settings says why. See "When a new version asks again". |
| 6 | How does the API ask? | As every decision the API requires (cursor-api.md): turning on a plug-in with an acknowledgment is a 409 `acknowledgment_required` carrying the text and its version until the request says `acknowledged: "<version>"`, that version and no other. So no client turns one on without showing it: the web app's dialog, the CLI's prompt (or `--accept`, having been shown it). |
| 7 | What's kept of an acknowledgment? | Each acceptance, for good: the plug-in, the version, who, when. Turning the plug-in off doesn't erase it. |

## Design

### The registry

`src/plugins/`: one folder a plug-in, its `__init__.py` declaring a `PluginSpec` (`src/plugins/contract.py`), as a producer's folder declares its `ProducerSpec`:

- `plugin_id`, `title`, `description` (a sentence or two for Settings);
- `acknowledgment`: `Acknowledgment(version, title, text, links, reaccept_from)` or none;
- what it keeps for the server, if anything (`data`: how to fetch it, as `module:function`, and what it is, for the status line).

`src/plugins/__init__.py` lists them; everything else reads it.

### Each account's switch

Two tables in the account's database:

- `plugins`: `plugin_id`, `enabled`, `acknowledged_version` (the one accepted when it was turned on), `changed_by`, `changed_at`. A plug-in with no row is off.
- `plugin_acknowledgments`: every acceptance (`plugin_id`, `version`, `user_id`, `acknowledged_at`), never updated or deleted.

A plug-in is **on** when its row says enabled and, if it declares an acknowledgment, the version accepted is the version now. Enabled with an older version accepted is **off, waiting** (decision 5): nothing of it runs until it's accepted again.

### What a plug-in keeps for the server

What's the same for every account (a list of places) is kept once, in the control database, with its state in `plugin_data`: `missing`, `fetching` (with how far), `ready` (with what and when) or `failed` (with why). Turning a plug-in on fetches it if it isn't ready, in the background; upkeep resumes a fetch that stopped (a restart) and tries a failed one again later. It's kept when every account turns the plug-in off (it's small, and the next to turn it on doesn't wait).

### API

- `GET /v1/plugins` — signed in. Every plug-in: `plugin_id`, `title`, `description`, `state` (`on`, `off`, `waiting`: on, with an older acknowledgment), `acknowledgment` (`version`, `title`, `text`, `links`) or null, `acknowledged` (`version`, `by`, `at`; who, for admins) or null, and `data` (its state, how far, what's there) or null.
- `PUT /v1/plugins/{plugin_id}` — account admin. Body `{ "enabled": bool, "acknowledged": "<version>" }`. On, with an acknowledgment, without the version now: 409 `acknowledgment_required` (`details`: `plugin_id`, `version`, `title`, `text`, `links`). Off asks nothing. Again when it's on: nothing changes, except a fetch that failed starts again.

What a plug-in adds to the API answers 409 `plugin_off` when it's off for the account (`details.plugin_id`), so a client can say where to turn it on.

### Web and CLI

- Settings › Plug-ins (account admins): each plug-in with its switch, what it does, what it keeps for the server (fetching, ready, failed with Try again), and for one with an acknowledgment, who accepted which version when. Turning one on that asks opens the dialog: its title, the whole text, its links, **OK** and **Cancel**. Cancel puts the switch back.
- `lumiverb plugins` (the list), `lumiverb plugins enable ID` (prints the text and asks; `--accept` for no one to ask, having been shown it), `lumiverb plugins disable ID`.

## Place names (GeoNames), the first plug-in

ADR-017 phase 4 needs names for places. They come from GeoNames, under Creative Commons Attribution 4.0 (commercial use allowed; GeoNames is to be credited with a link, the license linked, and changes noted). Its acknowledgment says that in full before anything is fetched.

- **When it's fetched:** when the first account's admin turns Place names on and accepts its license, never at install; once for the server, whichever account turns it on next uses what's there.
- **What's fetched:** `cities1000.zip` (every town of 1,000 people or more: 171,329 places, 11 MB, 2026-10-18), `admin1CodesASCII.txt` (regions) and `countryInfo.txt` (countries), from `download.geonames.org`, once for the server (`LUMIVERB_GEONAMES_URL` names a mirror). Nothing about anyone's photos is sent. Kept in the control database: each place's id, name, ASCII name, point, feature code, country, region, population and time zone; not its alternate names (10 MB more; later, if search needs "Wien" for Vienna). A new fetch replaces the old in one transaction, so search never sees half a list.
- **What it adds (phase 2):** place search when setting a location, by name (`GET /v1/places?q=`), most populous first; and `PUT /v1/assets/locations` taking `{place_id}`, which sets a person's location at that place's point and records the place. Where place names show, the credit shows.
- **Later (ADR-017 phase 4's rest):** a name beside every location ("Near Port of Spain, Trinidad and Tobago"), and suggestions from text in photos.

## When a new version asks again (Robert, 2026-10-18)

Robert: "the requiring acknowledgment on an upgrade should be defined by the plug-in itself: going from Creative Commons to Creative Commons should not require a new acknowledgment; going from Creative Commons to MIT or another license could, but only the new plug-in can know that." Decided: "if not accepted yet or if the plugin says 'must re-accept'."

- **Not accepted yet** is the server's to know: an account with nothing accepted (a new install, or one that never turned it on) is always asked. Every turn-on asks too, as before (decision 4): turning a plug-in on shows its text.
- **"Must re-accept"** is the plug-in's to say, on its acknowledgment: `reaccept_from`, the earliest version whose acceptance still stands. A version whose terms changed names itself; one that only rewords (or downloads more under the same license) keeps the one before's. Empty: any version accepted stands.
- Versions are numbers with dots ("1", "2", "2.1"), compared as numbers (the registry refuses others). Naming a version rather than saying "this one asks" means a server that skips the release that asked still asks: one going from 1 straight to 2.1, where 2 asked, finds `reaccept_from = "2"` and waits.
- Kept as before: the acceptance an account is on by stays the version it accepted (`plugins.acknowledged_version`), every acceptance in `plugin_acknowledgments`. `repository/plugins.py` `stands` decides, in Python and in SQL (`on_sql`, for the producers' queue).

## A plug-in's producers (Robert, 2026-10-18)

Robert, on Place names showing in Settings › Plug-ins but not Processing: "Shouldn't this be a processor as well? A plugin processor?" What a plug-in does for every clip is a producer, the plug-in's: a producer declares `plugin` (`ProducerSpec.plugin`), and it applies only while that plug-in is on in the account (`lineage.APPLIES` puts `plugins.on_sql` before its own `applies`), so it has everything a producer has: a row in Processing with its counts, waits and Pause (and "Part of the … plug-in", saying when it's off), its redo when its settings change, failures shown. Its spec stays one folder that imports only the contract; the registry refuses a plug-in that isn't one.

The first: **Place names** (`src/producers/place/`), decided as proposed ("Let's do it as proposed"):
1. **What's named:** the location the clip shows: a person's, else the file's GPS, else an applied guess (never a suggestion); named again whenever that changes (the name is kept with the location it was found for, `asset_place.lat/lon`: a clip whose location no longer matches is due again, and its old name isn't shown meanwhile, though it stays until the new one is saved).
2. **How near:** the nearest town within 25 km (its setting, `radius_km`, which remakes); beyond, "Not near a town" (lineage outcome `empty`). A section of a town (PPLX, "Zürich (Kreis 7)"), and historical, abandoned or destroyed places, are never a town's name.
3. **Where it shows:** the Lightbox, under the location ("Near Port of Spain, Trinidad and Tobago · GeoNames"), signed-in only; never on a public page. Searching by place name stays later (ADR-017 decision 9).
4. **Turned off:** names stay and nothing more is named; turned on again, what's missing is named.

The scheduler's work asks the server for the job's clips' locations and nearest towns in one request, and saves what it found with the location it was found for: one that has moved since is refused as stale and named again. While the place list isn't fetched yet, or the plug-in was turned off meanwhile, its clips wait (held) instead of failing.

## Alternatives considered

- **Every optional feature a plug-in now.** Not until Robert has tried the UI (decision 2).
- **Acknowledgment by the server admin, once for the install.** Rejected: each account decides what it turns on; only fetching the data is the server's.
- **Ship the place list with the install.** Rejected for now: it's 11 MB that most installs may never turn on, and the acknowledgment comes before anything is fetched.

## What this does NOT include

- Plug-ins from outside Lumiverb.
- Moving faces, transcripts, descriptions, location guesses or anything else to plug-ins.
- Place names beside locations, and suggestions from text in photos (ADR-017 phase 4's rest).

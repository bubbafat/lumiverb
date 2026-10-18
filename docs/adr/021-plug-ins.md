# ADR-021: Plug-ins, and what turning one on asks

## Status

Accepted (2026-10-18, Robert). Phase 1 first: one plug-in, place names from GeoNames, until Robert has tried the UI.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | The mechanism: the registry, each account's switch, the acknowledgment and its record, the API, the CLI, Settings › Plug-ins | Built: `src/plugins/`, `repository/plugins.py`, `routers/plugins.py`, `lumiverb plugins`, `pages/settings/PluginsSection.tsx`; every turn-on asks (not only the first) |
| 2 | Place names (GeoNames): the place list downloaded once for the server; place search when setting a location (ADR-017 phase 4, its first part) | Built: `src/plugins/geonames/data.py`, `src/server/plugin_data.py`, `GET /v1/places`, `place_id` on `PUT /v1/assets/locations`, Set location's Place, the GeoNames credit beside a place's name |
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
| 5 | The text changes in a new release? | Its version goes up, and the plug-in is off in every account that accepted an older one until an admin accepts the new text. Settings says why. |
| 6 | How does the API ask? | As every decision the API requires (cursor-api.md): turning on a plug-in with an acknowledgment is a 409 `acknowledgment_required` carrying the text and its version until the request says `acknowledged: "<version>"`, that version and no other. So no client turns one on without showing it: the web app's dialog, the CLI's prompt (or `--accept`, having been shown it). |
| 7 | What's kept of an acknowledgment? | Each acceptance, for good: the plug-in, the version, who, when. Turning the plug-in off doesn't erase it. |

## Design

### The registry

`src/plugins/`: one folder a plug-in, its `__init__.py` declaring a `PluginSpec` (`src/plugins/contract.py`), as a producer's folder declares its `ProducerSpec`:

- `plugin_id`, `title`, `description` (a sentence or two for Settings);
- `acknowledgment`: `Acknowledgment(version, title, text, links)` or none;
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

## Open question: when a new version asks again (Robert, 2026-10-18)

Robert: "the requiring acknowledgment on an upgrade should be defined by the plug-in itself: going from Creative Commons to Creative Commons should not require a new acknowledgment; going from Creative Commons to MIT or another license could, but only the new plug-in can know that." And: "the plug-in wouldn't necessarily know it's a new install versus an upgrade, but I kinda wanna think about that."

What's known already: whether an account accepted anything, and which version (`plugin_acknowledgments`). A new install (or an account that never turned it on) has nothing accepted and is always asked. Only an upgrade has an older acceptance, so the question is the plug-in's alone: does what was accepted still cover it? Ways it could say so, for Robert to choose:
- **Terms apart from wording:** the acknowledgment names its terms (`terms="cc-by-4.0"`) beside its text's version; a new version with the same terms is covered, other terms ask again.
- **What it still covers:** the acknowledgment lists the earlier versions its text still covers (`covers=("1",)`); an acceptance of one of them stands.

Until then decision 5 stands as built: any new version asks again. No plug-in has a second version yet.

## Alternatives considered

- **Every optional feature a plug-in now.** Not until Robert has tried the UI (decision 2).
- **Acknowledgment by the server admin, once for the install.** Rejected: each account decides what it turns on; only fetching the data is the server's.
- **Ship the place list with the install.** Rejected for now: it's 11 MB that most installs may never turn on, and the acknowledgment comes before anything is fetched.

## What this does NOT include

- Plug-ins from outside Lumiverb.
- Moving faces, transcripts, descriptions, location guesses or anything else to plug-ins.
- Place names beside locations, and suggestions from text in photos (ADR-017 phase 4's rest).

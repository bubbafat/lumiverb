# ADR-024: Named data between producers

## Status

Accepted (2026-10-19, Robert: he answered its questions and set its phases). From Robert's question during ADR-023 phase 3: how does one producer know another's data is ready, and could a plug-in depend on data another plug-in makes? The decisions marked **Robert** are his, from that conversation, his answers to its questions included ("Robert's answers"); the rest are proposals that each phase's plan settles. Builds on ADR-023: it widens decision 4 (what a plug-in can read) and decision 5 (what it gives back).

Nothing here is built yet. ADR-023 phase 3 (GeoNames moved out, done in v0.1.104) didn't anticipate it.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Lumiverb's own data named (Robert: "refactor builtins to align to the new naming schema"): each built-in producer's data and protocol 1's inputs get `lumiverb.*` names in plain words, documented, and every built-in producer declares by name what it reads and makes | Not started |
| 2 | Lumiverb's plug-ins renamed (Robert: "refactor existing plugins to use the new schema where they use the prefix that matches the plugin name"): GeoNames' `at` and `near` become `geonames.landmark` and `geonames.town`, the example's `example.caption`, `example.taken` and `example.shape` | Not started |
| 3 | Plug-ins read each other's data: names, types, versions and contract in the manifest; the checks at install, upgrade and removal; waiting and the inputs' hash; broken chains warned of on Settings › Plug-ins | Not started |
| 4 | Search by what a clip has: `has:<name>`, and not | Not started |
| 5 | Search by value, by a name's type | Not started |

## How producers coordinate today

No producer tells another anything. The database is the one source: what's made is what's saved.

- A built-in producer declares what it's made from (`ProducerSpec.needs`, `src/producers/contract.py`): scenes from the analysis proxy, scene descriptions from the analysis proxy and scenes. The scheduler's queue lists a clip for it only once what it needs is made for that clip (`lineage.made_first`). "Ready" is the save that made it committing; nothing is announced.
- A clip that waits is said: Settings › Processing shows "N wait for Scenes" (the `needs` wait, `src/server/producer_waits.py`), and whether what makes it is paused. A clip whose earlier step was given up on isn't counted as work left.
- When something is made again, what was made from it goes with it (`redo_also`).
- A producer naming what no producer makes is refused when the registry is read (`src/producers/__init__.py`).
- An installed plug-in reads only Lumiverb's own inputs, a fixed list (`src/plugins/manifest.py` `INPUTS`: location, capture time, metadata, a photo's preview), refused at install otherwise. What it gives back is kept with a hash of the inputs it was handed (`plugin_outputs.inputs_hash`): when a clip's inputs change (a person moves its location) its output is due again by itself.

What a plug-in makes can't be read by anything else: no input names it.

## What Robert asked for

Robert, 2026-10-19: "This feels like namespaced data. So I could create a plugin that creates: fancy.plugin.company.metadata_a which is a string that represents something about an asset. My plugin declares: I need "video.proxy" (I'm making up terms) and "location.gps" and I produce "fancy.plugin.company.metadata_a". Now ... another person might create a plugin that needs that fancy new metadata_a. During install it would assert that and see the plugin that makes it exists. But it might also need metadata_b and there is no plugin for that. So it would complain at install time. Or it did exist and it was removed and now it might complain or something else. So we'd have well-known (the lumiverb standard) set of data and then extended (plugins, etc) set. And we could create as complex of relationships as we want EXCEPT that we should never have circular ones. That should be caught during installations. Now we could have queries like "Find all the assets that satisfy video.proxy and location.gps" or "find the assets that have "video.proxy" but not "location.gps"", etc."

And on privacy: "I think it's less about enforced privacy and more about documented privacy. The produced data should be part of an API contract or not. If someone depends on a non-contractual piece and breaks ... tough cookies. This also allows us to, someday, create a UX where we could show all public metadata and allow people to create UI-based workflows. Like "When any video has a location.gps applied to it, perform action X" - that's 100% forward looking and NOT something to build now. I do think namespaces are important, though. lumiverb.* would indicate that it's official. lumiverb.<asset type>.* could be useful too for exploration too but probably secondary."

## Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | How is data named? | **Robert.** By dotted, namespaced names. Every producer, built-in or plug-in, declares by name what it reads and what it makes. The graph Lumiverb's producers already form (`ProducerSpec.needs`) becomes the graph of every producer on an install. |
| 2 | Whose names are whose? | **Robert.** `lumiverb.*` is official: Lumiverb's own data, defined and documented by Lumiverb, versioned with the protocol, and made by Lumiverb alone. Every other name is a plug-in's, named as its author likes, with no scheme imposed: "Let's avoid looking too much like java. I think it's lumiverb.* for builtins and whatever people want for anything else. If someone does not want to be a good citizen and use a good name that's on them." `lumiverb.<media>.*` (`lumiverb.video.scenes`) groups what only one kind of clip has, for finding your way; data both have has no media level (`lumiverb.location`). |
| 3 | Can a plug-in keep its data from others? | **Robert.** Documented, not enforced. Each name a producer makes is part of its contract or not. A contract name is documented, has a type and a version, and stays the same within that version. Any other name can be read, with no promise: "If someone depends on a non-contractual piece and breaks ... tough cookies." Lumiverb still says when a reader breaks (decision 6); it never fails silently. Only contract names are listed for people (decision 10). |
| 4 | What's checked at install? | **Robert.** Every name a plug-in reads is made on this install (by Lumiverb or by an installed plug-in), and nothing is circular. A plug-in makes no `lumiverb.*` name, and no name another installed plug-in makes (decision 8). Otherwise the install is refused, saying what's missing, who makes the name already, or which names make the circle. Proposed: an upgrade is checked the same way (a new version could read something new, or close a circle). |
| 5 | What happens when what others read goes? | **Robert** (agreed). Uninstalling a plug-in, or upgrading it to a version that no longer makes a name, asks first and names the plug-ins reading it (as the API asks before any surprising outcome). If the server admin goes on, the readers stay installed, their work held with a reason shown on Processing and the Admin page, and their results shown only while a clip still has what they were made from (`plugin_outputs.shown_sql` already does this for inputs). Installing it again picks them up where they were. Turned off in one account, the same, in that account; turning a reader on offers to turn on what it reads. |
| 6 | When is a clip's data ready for its reader? | As today, from the database: a clip is handed to a reader only once what it reads is made for that clip, and Processing says what it waits for and whether that's paused; a clip its maker gave up on isn't the reader's work left. What a plug-in read is part of the hash its output is kept with, so when its maker's data for a clip changes, the reader's is due again by itself: no cascade to declare. A name whose maker is gone is a wait with its own code (`input_gone`), never silent (`tests/test_no_silent_waits.py`). |
| 7 | What shape is a name's data? | Proposed: each name has a type (text, number, a point, a time, a list of them; files later) and a major version. A reader names the major versions it takes (`fancy.metadata_a@1`). A contract name's type changes only with a new major version: an upgrade that changes it under the same version is refused. |
| 8 | Can two producers make the same name? | **Robert.** No: one maker per name, and a `lumiverb.*` name is Lumiverb's alone. Whether a plug-in may one day make a `lumiverb.*` name in place of Lumiverb's own (a better transcriber), or a mapping layer stand between a name and what makes it, is a future decision: "this feels like we're becoming needlessly complex. I think this is a future decision and for now they are not overloaded." |
| 9 | How are clips found by their data? | **Robert.** By what they have, and don't: "has `lumiverb.video.analysis_proxy` but not `lumiverb.location.gps`". A filter `has:<name>`, negated by the filter algebra's `not` (`src/server/models/query_filter.py`); "has" is what the clip shows now. By value later, by the name's type: text matched as search matches it, numbers and times by range, points by distance. The `missing_*` filters each producer has today become `not has:`. |
| 10 | Workflows ("When any video has a location.gps applied to it, perform action X")? | **Robert.** Not now: "100% forward looking and NOT something to build now." Contract names are what such a screen would list, which is a reason to keep the contract documented from the start. |
| 11 | How does a person see a broken chain? | **Robert.** Settings › Plug-ins looks for broken dependency chains whenever it's loaded and shows each as a warning: "The plugin UI should explicitly look for broken dependency chains and report them when the page is loaded as a warning." Proposed: the server works them out when asked (`GET /v1/plugins`, each plug-in's `broken`), never kept apart, and follows the whole chain: a plug-in whose reading reaches, through others, a name nothing makes now, or one whose maker is off in this account or isn't running, each shown with its chain ("Fancy B reads Fancy A's metadata_a: Fancy A isn't installed"). A reader held by decision 5 is among them. The Admin page's plug-ins row counts them. |
| 12 | How are names worded? | **Robert.** For people reading them: "Bias towards readability - so things like "town" or "landmark" are preferable to "municipality" or "nearest_historical_landmark"." Lumiverb's own plug-ins name their data under the plug-in's name (`geonames.town`, `geonames.landmark`). |

## Design

### Names for what exists

A first list, to be settled in phase 1 in plain words (decision 12). Each built-in producer's artifact, and each protocol 1 input, gets a name:

| Today | Name |
|-------|------|
| input `location` (a person's, else the file's GPS, else an applied guess) | `lumiverb.location` |
| the file's own GPS (input `location` with source `file`) | `lumiverb.location.gps` |
| input `capture_time` | `lumiverb.capture_time` |
| input `metadata` | `lumiverb.metadata` |
| `proxy` (preview and thumbnail; input `preview` for a photo) | `lumiverb.preview` |
| `probe` | `lumiverb.video.probe` |
| `video_preview` | `lumiverb.video.preview` |
| `analysis_proxy` | `lumiverb.video.analysis_proxy` |
| `scenes`, `scene_vision` | `lumiverb.video.scenes`, `lumiverb.video.scene_descriptions` |
| `transcript` | `lumiverb.video.transcript` |
| `video_faces` | `lumiverb.video.faces` |
| `vision`, `ocr`, `clip`, `faces` | `lumiverb.image.description`, `lumiverb.image.text`, `lumiverb.image.clip_vector`, `lumiverb.image.faces` |
| `location` (guesses) | `lumiverb.location.guess` |

A page lists them, each with its type, version and what it is, written from the producers (never a list kept apart). Phase 1's plan settles whether what's stored and sent under today's names (lineage's artifacts, `GET /v1/producers`' `artifact`, the `missing_*` filters) takes these names too, one name everywhere, or these stand beside them; either way a client using today's names is answered as before. Protocol 1's input names are still taken, so a plug-in written for it keeps working.

### The manifest

```json
"work": {
  "title": "Metadata A",
  "media": ["video"],
  "reads": ["lumiverb.video.analysis_proxy", "lumiverb.location.gps"],
  "outputs": {
    "fancy.metadata_a": {"kind": "fact", "type": "text", "version": 1, "contract": true,
                   "about": "What metadata A says about a clip"}
  }
}
```

It makes `fancy.metadata_a`, a name its author chose. Another plug-in reads it as `"reads": ["fancy.metadata_a@1"]`. Lumiverb's own plug-ins' outputs are renamed in phase 2 (GeoNames' `near` becomes `geonames.town`); a plug-in still on protocol 1, its outputs named without a dot, is read as its id and their key. A reader is handed each clip's values of the names it reads, as inputs are handed today, and still never learns which account it is or anything of other clips. Files (an analysis proxy) are handed as a read-only copy once a plug-in can read files (ADR-023's "later").

### Checks

At install, at upgrade and at uninstall, over every producer on the install, Lumiverb's and the plug-ins' together:

- every name read is made here, at a version the reader takes;
- an order exists in which each producer comes after what it reads (no circle);
- no plug-in makes a `lumiverb.*` name, and no name has two makers;
- what stops being made, or changes type, and who reads it (decision 5's question).

### The scheduler

A plug-in's producer needs the producers of what it reads (`ProducerSpec.needs`, as built-ins do), so its clips wait for them and Processing says so. The values it read go into its inputs' hash. A reader whose maker is gone is held with `input_gone`.

## Robert's answers (2026-10-19)

1. **When what others read is removed:** ask first, then hold the readers with a reason (decision 5). "agreed."
2. **A plug-in making a `lumiverb.*` name:** not for now (decision 8). "This is an interesting debate. Part of me says "overload is fine" and part says "they are protected but we can have a mapping layer that abstracts that away" but this feels like we're becoming needlessly complex. I think this is a future decision and for now they are not overloaded."
3. **Third parties' names:** no scheme: `lumiverb.*` for Lumiverb's own, anything else as its author likes (decision 2). "Let's avoid looking too much like java."

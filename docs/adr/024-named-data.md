# ADR-024: Named data between producers

## Status

Proposed (2026-10-19). From Robert's question during ADR-023 phase 3: how does one producer know another's data is ready, and could a plug-in depend on data another plug-in makes? The decisions marked **Robert** are his, from that conversation; the rest are proposed and wait for him. Builds on ADR-023: it widens decision 4 (what a plug-in can read) and decision 5 (what it gives back).

Nothing here is built. ADR-023 phase 3 (GeoNames moved out) doesn't anticipate it.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Names for what exists: Lumiverb's data listed and documented as `lumiverb.*` names, every built-in producer declaring by name what it reads and makes, protocol 1's inputs as names | Not started |
| 2 | Plug-ins read each other's data: names, types, versions and contract in the manifest; the checks at install, upgrade and removal; waiting and the inputs' hash | Not started |
| 3 | Search by what a clip has: `has:<name>`, and not | Not started |
| 4 | Search by value, by a name's type | Not started |

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
| 2 | Whose names are whose? | **Robert.** `lumiverb.*` is official: the standard set, defined and documented by Lumiverb and versioned with the protocol. Everything else is extended: a plug-in's names are under its own id. `lumiverb.<media>.*` (`lumiverb.video.scenes`) groups what only one kind of clip has, for finding your way; data both have has no media level (`lumiverb.location`). Proposed: a third party's plug-in id is a reversed domain (`com.company.fancy`), so two companies can't both be `fancy`; Lumiverb's own keep their short ids (`geonames`). |
| 3 | Can a plug-in keep its data from others? | **Robert.** Documented, not enforced. Each name a producer makes is part of its contract or not. A contract name is documented, has a type and a version, and stays the same within that version. Any other name can be read, with no promise: "If someone depends on a non-contractual piece and breaks ... tough cookies." Lumiverb still says when a reader breaks (decision 6); it never fails silently. Only contract names are listed for people (decision 10). |
| 4 | What's checked at install? | **Robert.** Every name a plug-in reads is made on this install (by Lumiverb or by an installed plug-in), and nothing is circular. Otherwise the install is refused, saying what's missing or which names make the circle. Proposed: an upgrade is checked the same way (a new version could read something new, or close a circle), and a plug-in makes only names under its own id. |
| 5 | What happens when what others read goes? | Robert left this open ("it might complain or something else"). Proposed: uninstalling a plug-in, or upgrading it to a version that no longer makes a name, asks first and names the plug-ins reading it (as the API asks before any surprising outcome). If the server admin goes on, the readers stay installed, their work held with a reason shown on Processing and the Admin page, and their results shown only while a clip still has what they were made from (`plugin_outputs.shown_sql` already does this for inputs). Installing it again picks them up where they were. Turned off in one account, the same, in that account; turning a reader on offers to turn on what it reads. |
| 6 | When is a clip's data ready for its reader? | As today, from the database: a clip is handed to a reader only once what it reads is made for that clip, and Processing says what it waits for and whether that's paused; a clip its maker gave up on isn't the reader's work left. What a plug-in read is part of the hash its output is kept with, so when its maker's data for a clip changes, the reader's is due again by itself: no cascade to declare. A name whose maker is gone is a wait with its own code (`input_gone`), never silent (`tests/test_no_silent_waits.py`). |
| 7 | What shape is a name's data? | Proposed: each name has a type (text, number, a point, a time, a list of them; files later) and a major version. A reader names the major versions it takes (`com.company.fancy.metadata_a@1`). A contract name's type changes only with a new major version: an upgrade that changes it under the same version is refused. |
| 8 | Can two producers make the same name? | Proposed: no, one maker per name on an install. Open for Robert: may a plug-in make a `lumiverb.*` name in place of Lumiverb's own, as the server admin's choice (a better transcriber making `lumiverb.video.transcript`)? It's how a built-in producer would move out of Lumiverb, and readers wouldn't notice. The name's shape stays Lumiverb's. |
| 9 | How are clips found by their data? | **Robert.** By what they have, and don't: "has `lumiverb.video.analysis_proxy` but not `lumiverb.location.gps`". A filter `has:<name>`, negated by the filter algebra's `not` (`src/server/models/query_filter.py`); "has" is what the clip shows now. By value later, by the name's type: text matched as search matches it, numbers and times by range, points by distance. The `missing_*` filters each producer has today become `not has:`. |
| 10 | Workflows ("When any video has a location.gps applied to it, perform action X")? | **Robert.** Not now: "100% forward looking and NOT something to build now." Contract names are what such a screen would list, which is a reason to keep the contract documented from the start. |

## Design

### Names for what exists

A first list, to be settled in phase 1. Each built-in producer's artifact, and each protocol 1 input, gets a name:

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

A page lists them, each with its type, version and what it is, written from the producers (never a list kept apart). Protocol 1's input names stay as they are, so GeoNames and the example plug-in change nothing.

### The manifest

```json
"work": {
  "title": "Metadata A",
  "media": ["video"],
  "reads": ["lumiverb.video.analysis_proxy", "lumiverb.location.gps"],
  "outputs": {
    "metadata_a": {"kind": "fact", "type": "text", "version": 1, "contract": true,
                   "about": "What metadata A says about a clip"}
  }
}
```

It makes `com.company.fancy.metadata_a`. Another plug-in reads it as `"reads": ["com.company.fancy.metadata_a@1"]`. A reader is handed each clip's values of the names it reads, as inputs are handed today, and still never learns which account it is or anything of other clips. Files (an analysis proxy) are handed as a read-only copy once a plug-in can read files (ADR-023's "later").

### Checks

At install, at upgrade and at uninstall, over every producer on the install, Lumiverb's and the plug-ins' together:

- every name read is made here, at a version the reader takes;
- an order exists in which each producer comes after what it reads (no circle);
- a plug-in makes names under its own id only (and `lumiverb.*` only as decision 8 settles);
- what stops being made, or changes type, and who reads it (decision 5's question).

### The scheduler

A plug-in's producer needs the producers of what it reads (`ProducerSpec.needs`, as built-ins do), so its clips wait for them and Processing says so. The values it read go into its inputs' hash. A reader whose maker is gone is held with `input_gone`.

## Questions for Robert

1. **When what others read is removed** (decision 5): ask, then hold the readers with a reason (recommended), or refuse until the readers are removed first?
2. **A plug-in making a `lumiverb.*` name** in place of Lumiverb's own (decision 8): allowed as the server admin's choice, one maker per name (recommended: it's how built-ins move out), or `lumiverb.*` made by Lumiverb alone?
3. **Third parties' ids** (decision 2): a reversed domain (recommended), or short ids, first come first served on an install?

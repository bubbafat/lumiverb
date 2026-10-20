# Writing a plug-in

A Lumiverb plug-in is a program of its own, in any language, run outside Lumiverb's processes (ADR-023). Lumiverb starts it, hands it what it asks for, and keeps what it gives back. It never gets a database, an API key or a path to a library: it answers questions on a socket, and that's all it does.

This page is protocol 1's whole contract: the manifest, how a plug-in is run, and every request it answers. `tests/test_plugin_guide_doc.py` holds what it says to the code.

Two plug-ins to read alongside it, both written against this page alone (Python's standard library, nothing of Lumiverb's):

- `src/plugin_examples/example/`: Lumiverb's example. Every kind of setting, its own settings check, a work of facts and a place lookup.
- `tests/plugins/hello/`: the smallest, and each way a plug-in can go wrong (it's what the runner's tests start).

## What a plug-in can do

- **Work on each clip.** Lumiverb hands it a batch of clips with the inputs it declares (a clip's location, when it was taken, its metadata, a photo's preview), and it gives back facts for each: short lines of text, each perhaps with a link. Lumiverb shows them with the clip ("Near: Anaheim") and can search them.
- **Answer a lookup.** A screen asks it something rather than a clip: today, Set location's Place search asks it for places by name.
- **Have settings.** Each account's admin, or the server admin, sets them in Settings › Plug-ins, which builds the form from the manifest.

## The folder

A plug-in is a folder: its program, anything the program needs, and `lumiverb-plugin.json`, its manifest. The server admin installs it on the machine:

```
lumiverb-server plugins install FOLDER --on
```

`--on` turns it on by default for every account that hasn't chosen (`--off`: each account's admin turns it on). Lumiverb reads the manifest before any of the plug-in's code runs, shows what it reads and reaches, and asks the server admin to accept that. Then it copies the folder into the install (`DATA_DIR/plugins/<id>/program/`), read-only, swapped in whole. A folder holds at most 4 GiB and 100,000 files.

## The manifest

`lumiverb-plugin.json`, at most 256 KB of JSON. Every problem in it is listed at once, each naming its field; a field Lumiverb doesn't know is refused, as is a value outside its bounds. A plug-in whose manifest fails is never started.

A complete one, small:

```json
{
  "id": "weather",
  "version": "1.0",
  "protocol": 1,
  "title": "Weather",
  "description": "Says what the weather was where and when each photo was taken.",
  "author": "Ana",
  "homepage": "https://example.com/weather",
  "run": ["python3", "weather.py"],
  "internet": true,
  "memory_mb": 256,
  "disk_mb": 50,
  "settings": [
    {"key": "units", "type": "choice", "default": "c", "label": "Units", "help": "How temperatures are said.",
     "choices": [{"value": "c", "label": "Celsius"}, {"value": "f", "label": "Fahrenheit"}], "remakes": true},
    {"key": "api_key", "type": "secret", "default": "", "scope": "server", "label": "API key",
     "help": "The weather service's key, for the whole server."}
  ],
  "check_settings": true,
  "work": {
    "title": "Weather",
    "version": "1",
    "media": ["image", "video"],
    "inputs": ["location", "capture_time"],
    "batch": 20,
    "outputs": [
      {"key": "weather", "kind": "fact", "label": "Weather", "searchable": true, "empty": "Not known"}
    ]
  }
}
```

### Its fields

| Field | What it is |
|---|---|
| `id` | Required. Lowercase letters, digits and dashes, starting with a letter, at most 40 characters: it's in URLs and commands. Not one of Lumiverb's own words (its data's names and its machine words: `lumiverb-server plugins install` refuses those), and never starting with `lumiverb`. |
| `version` | Required. Numbers with dots (`"1"`, `"2.1"`), compared as numbers: an install of a newer one is an update. |
| `protocol` | Required. The protocol it speaks: `1`. |
| `title` | Required, up to 100 characters. |
| `description` | Required, up to 1,000 characters: what it does, in a sentence or two. |
| `author`, `homepage` | Who made it, and an https link. |
| `run` | Required. Its command, a list of 1 to 32 strings, the program first: a path in its folder (`bin/weather`), or a program's name (`python3`, `node`). It's started in its installed folder. |
| `acknowledgment` | Something each account's admin accepts before turning it on (a license): `version`, `title`, `text` (up to 20,000 characters), `links` (`[{"label", "url"}]`, up to 10) and `reaccept_from`: a version from which it must be accepted again (otherwise the version accepted stands). |
| `credit` | Who to credit for what it shows or finds (a data source): `text`, `url`, and `license` (`{"text", "url"}`) when one asks for it. Shown beside its facts and places. |
| `downloads` | What it fetches, shown before it's accepted: `[{"url", "title", "about_mb"}]`, up to 20. It fetches them itself. |
| `data_version` | The version of the data it keeps, 1 or more (default 1). Lumiverb reads it and does nothing with it in protocol 1: it's the plug-in's own. |
| `internet` | Whether it reaches the internet besides its downloads (default false). Shown before it's accepted. |
| `memory_mb` | The memory it may use: default 1,024, at most 16,384. |
| `disk_mb` | What its data folder may hold, and the largest file it may write: default 1,000, at most 1,000,000. |
| `check_settings` | Whether it checks new settings itself too (`POST /v1/settings/check`, below). |
| `settings` | Up to 50 (below). |
| `work` | What it does for each clip (below). |
| `lookups` | The lookups it answers: `places` (Set location's Place search). |

An update that reads or reaches more than the version accepted (another input, a download from another host, the internet, a lookup) asks the server admin to accept it again.

### Settings

Each setting is `{"key", "type", "default", "label", "help", ...}`:

- `key`: lowercase letters, digits and underscores, starting with a letter, at most 40 characters.
- `type`: one of these.

| Type | Settings › Plug-ins shows | Lumiverb takes |
|---|---|---|
| `int` | a number | a whole number within `minimum` and `maximum` |
| `float` | a number | a finite number within `minimum` and `maximum` |
| `bool` | a switch | true or false |
| `text` | a text field | text up to `max_length` (at most 4,000), matching `pattern` (a regular expression, matched whole) when it has one, not empty unless its default is |
| `choice` | a menu | one of its `choices` (`[{"value", "label"}]`, up to 50) |
| `secret` | a password field | as `text`; never shown or sent back by the API (only whether it's set), never logged; its default is `""` |

- `label` (up to 80 characters) and `help` (a sentence under the field, up to 500) are required; `default` too, and within the setting's bounds.
- `minimum`, `maximum` and `unit` are a number's; `pattern` is text's; `max_length` is text's or a secret's.
- `scope`: `account` (the default: each account's admin sets it) or `server` (the server admin's, for the whole server: a key or a mirror).
- `advanced`: folded away until asked for.
- `remakes`: its outputs are made again when it changes (an account's setting only, never a secret). Lumiverb asks the admin first whether what was made is made again or kept.

Lumiverb checks every value against these before saving it or handing it to the plug-in.

### Its work

`work` is what it does for each clip: `{"title", "version", "media", "inputs", "batch", "outputs"}`.

- `title`: what Settings › Processing calls it, up to 80 characters.
- `version`: numbers with dots. Raise it when what it gives back would differ: what an older version made is then made again, once an admin starts it (as for Lumiverb's own producers).
- `media`: `image`, `video` or both (the default).
- `inputs`: what it's handed for each clip.

| Input | What it is |
|---|---|
| `location` | `{"lat", "lon", "sure_m", "source"}`: where the clip shows it was (a person's location, else the file's GPS, else a guess applied; never a suggestion), how sure in metres, and `source`: `person`, `file` or `guess`. A clip without one isn't one it works on. |
| `capture_time` | `{"utc", "offset_min"}`: when it was taken, ISO 8601 in UTC (`"2024-05-01T12:00:00Z"`), and its time zone's offset in minutes when known (else null). A clip without one isn't one it works on. |
| `metadata` | `{"media_type", "width", "height", "duration_sec", "camera_make", "camera_model", "lens"}`: every clip has it; a field not known is null. |
| `preview` | A photo's preview, `{"type", "base64"}`: a JPEG, PNG or WebP. Photos only: its `media` is `["image"]`. |

- `batch`: clips a request, 1 to 500 (default 50).
- `outputs`: up to 20, each `{"key", "kind", "label", ...}`. `key`: lowercase letters, digits and underscores, starting with a letter. `kind`: `fact` (protocol 1's only kind). `label`: what's shown before it, up to 80 characters. `searchable`: its text is found by the account's search. `empty`: what's shown when it has nothing to say for a clip ("Not near a town"), plain text up to 80 characters, never searched. `was`: the key it was saved under before a newer version renamed it; what was saved under that key is renamed, nothing made again.

A clip it would work on but for an input it lacks (no location, say) is counted apart on Settings › Processing ("Not made for 4,392 that have no location"), never as missing.

## How it's run

The plug-in runner starts it from its installed folder with its `run` command, only while it's on in some account (and again when the server admin changes its server settings). Its environment is clean:

| Variable | What it is |
|---|---|
| `LUMIVERB_PLUGIN_SOCKET` | The Unix socket to listen on: answer HTTP there. Remove a stale one before listening. |
| `LUMIVERB_PLUGIN_DATA` | Its own folder, the only one it may write. Kept across restarts and updates. |
| `LUMIVERB_PLUGIN_SETTINGS` | A JSON file of its server settings (secrets among them; only Lumiverb's user can read it), for what it does before anything asks it anything: fetching its downloads from a mirror, say. |
| `LUMIVERB_PLUGIN_ID`, `LUMIVERB_PLUGIN_PROTOCOL` | Its id, and the protocol it's spoken to in. |
| `HOME` | Its data folder. |
| `TMPDIR` | A folder of its own for temporary files. |
| `PYTHONUNBUFFERED` | `1`. |

Besides those, only `PATH`, the locale (`LANG`, `LC_ALL`, `TZ`) and this machine's proxy (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, and their lowercase spellings), when it has them: nothing of Lumiverb's settings, keys or database addresses.

What the runner holds it to:

- It answers its health within 60 seconds of starting, or it's ended.
- Its health is asked every 15 seconds, each answer within 5 seconds: missed 3 times in a row, it's restarted.
- More memory than its `memory_mb` (its whole process group's): ended and started again.
- A data folder holding more than its `disk_mb`: ended and left stopped, with why. No file it writes can be larger than that either, and it makes no core files. It runs at a lower priority than Lumiverb (nice 10).
- Stopping, it gets SIGTERM, then 10 seconds before what's left is killed.
- One that exits is started again, 2 seconds later at first and twice as long each time it stops soon after starting (within 3 minutes), up to 10 minutes; after 6 such stops in a row it's left stopped, with why.
- What it prints goes to the service's log a line at a time, named (`plugin weather | …`): lines over 2,000 characters are cut, and past 600 lines a minute the rest are dropped.

The server admin sees each plug-in's state (starting, ready, preparing, failing, stopped and why) on the Admin page and with `lumiverb-server plugins`.

## The protocol

HTTP/1.1 over the socket. Lumiverb asks; the plug-in only answers. Every answer is HTTP 200 with a JSON object, at most 64 KB (a work request's, 8 MB). Anything else (another status, not JSON, too big, too slow) counts as not answering.

### GET /v1/health

What it's doing:

- `{"state": "ready"}`: it answers everything below. It may say what it's doing in the background with `"detail"` (a line of text), `"done"` and `"total"` ("Fetching a newer list", 4 of 9).
- `{"state": "preparing", "done", "total", "detail"}`: getting ready (fetching or building its data). Its work waits meanwhile.
- `{"state": "failed", "error"}`: it can't work, and why, in a sentence: shown to the server admin.

It's asked every 0.5 seconds while starting, then every 15 seconds.

### POST /v1/settings/check

Only when its manifest says `"check_settings": true`. `{"scope", "settings"}`: `scope` is `account` or `server` (whose settings are changing), `settings` every one of its settings as they would be (each already checked against its type and bounds). It answers `{"errors": {}}`, or `{"errors": {"api_key": "This key was refused by the service"}}`: a sentence for each setting it refuses, shown beside that field, and nothing is saved. It has 5 seconds, and is asked only while it's ready: while it isn't, the settings are saved with Lumiverb's own checks alone, and the page says its check didn't run.

### POST /v1/work

A batch of clips:

```json
{"settings": {"units": "c", "api_key": "…"},
 "clips": [{"id": "1", "location": {"lat": 33.8, "lon": -117.9, "sure_m": 5.0, "source": "file"},
            "capture_time": {"utc": "2024-05-01T12:00:00Z", "offset_min": -420}}]}
```

- `settings`: every setting, the account's and the server's together, secrets too.
- `clips`: up to its `batch`, each with an `id` and the inputs it declares. An id means nothing outside the request: never which account, library, file or clip it is.

It has 120 seconds for the whole batch, and answers each clip by its id:

```json
{"clips": {"1": {"outputs": {"weather": {"text": "Sunny, 24 °C", "url": "https://example.com/w/1"}}}}}
```

- `outputs`: each output it declares, a fact `{"text", "url"}` or null (nothing to say: its `empty` line is shown). One left out is null too. `text` is plain text, 1 to 200 characters once its spaces are evened out: no HTML tag, no control character. `url`, when there is one, is an http or https link, up to 2,000 characters.
- Or `{"error": {"whose": "clip", "message"}}`: this clip can't be done (charged to the clip, and tried again later, less often each time, until it's given up after 10 tries). `"whose": "plugin"`: the trouble is the plug-in's (a service away): the clip waits, uncharged.

An output it doesn't declare, a fact that isn't one, or a clip left out is the plug-in's fault: the clip is tried again, and a plug-in that keeps doing it has its clips charged in the end.

What it gives back is kept with a hash of the inputs it was made from: a clip whose inputs change (a person moves its location) is handed to it again, and what it showed stays until the new answer is saved. Its facts are shown with the clip while it's on in the account; turned off, they're kept and hidden.

### POST /v1/lookups/places

Only when its manifest's `lookups` has `places`. Set location's Place search asks every plug-in on in the account that answers it, each with 3 seconds:

- `{"q", "limit", "settings"}`: places whose name starts with `q` (what was typed: a name, perhaps `"name, region or country"`), at most `limit` (1 to 25);
- `{"id", "settings"}`: the one place with that id, as it says it now (a place a person picked is looked up again by it).

It answers `{"places": [...]}`, up to 100, each:

- `id`: its own id for the place (a string, or a whole number), up to 100 characters, no spaces. Lumiverb calls it `<plugin id>:<id>`.
- `name`: required, plain text up to 200 characters.
- `region`, `country`: plain text up to 100 characters each.
- `kind`: up to 60 characters ("Town", "Park"); `matched`: what it matched, when not the name (another language's name), up to 200.
- `lat`, `lon`: required, a real point.
- `population`: a whole number, 0 or more.

One place that isn't one makes its whole answer unavailable: the search says that plug-in couldn't answer, and why.

## Updating and removing it

Install the folder of a newer version the same way: it's swapped in whole and started afresh, and if it reads or reaches more than the version accepted, the server admin accepts it again first. `lumiverb-server plugins default ID on|off` changes its default. `lumiverb-server plugins uninstall ID` says what it has in each account and what stops working without it, then stops it and removes its program, and does one of three with what it made: `--keep` (shown and searched as before, its settings and its data folder kept: installing it again picks it all up, nothing made again), `--trash` (hidden, deleted after 30 days unless it's installed again) or `--delete` (everything now, its data folder too).

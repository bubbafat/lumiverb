# Lumiverb for DaVinci Resolve (prototype)

A Workflow Integration for DaVinci Resolve Studio: search Lumiverb from inside
Resolve and add a clip to the current bin, or to the current timeline at the
playhead. It proves the basics (connecting, signing in, running inside Resolve,
the API) and nothing more yet.

## Install

Needs DaVinci Resolve Studio 21.1 or later on macOS (Workflow Integrations are
Studio-only, and the plugin uses the 21.1 scripting API).

```bash
bash clients/resolve/install.sh
```

Restart Resolve, then open **Workspace → Workflow Integrations → Lumiverb**.
Sign in with your Lumiverb server (the brain is `http://media`), email and
password.

## Use

- Search, then page through the results 10 at a time.
- **Project**: one of your Lumiverb projects (yours, or shared with you). The
  one named like the open Resolve project is picked when the plugin opens.
  Nothing links the two: a Lumiverb project is only something to search in or
  around.
- **All · In project · Not in project**: search everything, only the
  project's clips, or everything but them (`f=project:` / `f=not_project:`,
  filtered on the server, so pages are full and nothing is skipped). Picking a
  project starts on Not in project.
- **New from Resolve project…** (at the bottom of the project list) makes a
  Lumiverb project, named after the Resolve project, of the Lumiverb clips its
  Media Pool holds, and says how many files Lumiverb doesn't have. It's a
  snapshot: clips added to Resolve later aren't in it.
- **Type**: Video (the default), Images or All (`f=media:`).
- Changing the project, scope or type keeps your place: a cursor is a place
  in the search's order, whatever the filters. New search text starts over.
- **Add to bin** imports the original into the Media Pool's current bin. A clip
  already in that bin isn't imported twice.
- **Add at playhead** puts it on the current timeline at the playhead, on the
  first video track free there (and the audio track with the same number, for
  a clip with sound). When no track is free it adds one. It never covers
  another clip.
- **Also add to project** (on by default) adds the clip to the chosen project
  too, after Resolve. Off for a shared project: only its owner can add clips.
- How an add went shows on the clip's row, with the message inside it: a
  green border when it worked, red when it didn't, amber when the clip is in
  Resolve but adding it to the project failed.
- Files are imported by plain path. Resolve 21.1.0 refuses the docs'
  `ImportMedia([{ FilePath }])` for an ordinary file ("Invalid 'StartIndex' or
  'EndIndex' input specified"): it reads it as an image sequence.

## How it works

- `plugin/main.js` is the Electron main process Resolve starts. All Lumiverb
  and Resolve calls happen there; the page (`index.html`, `renderer.js`) is
  sandboxed and reaches it only through `preload.js`.
- **Paths.** Libraries store their root as the Mac sees it
  (`/Volumes/media-01/Media`), so a clip's original is the root plus its
  `rel_path`. A result whose file isn't there says "Not on this Mac".
- **Paths as the disk spells them** (`plugin/paths.js`). A root can be
  spelled differently from the mount it names: the brain's library says
  `/Volumes/media-01`, the disk is mounted as `/Volumes/MEDIA-01`, and the Mac
  treats them as one folder. Resolve stores the mount's spelling. So paths are
  compared by the file's real path (`fs.realpathSync.native`; Node's plain
  `realpathSync` keeps the case it's given), in NFC; offline media by its path
  as stored. Clips are imported by that real path, so they match ones imported
  from Finder.
- **New from Resolve project** (`plugin/projects.js`). Each Media Pool file
  is matched to its library by the deepest root holding it (roots spelled as
  the disk does), then looked up with `GET /v1/assets/by-path`, six at a time.
  The project is made with `POST /v1/projects` in the Media Pool's order.
- **Sign-in.** `POST /v1/auth/login`, then the JWT is refreshed on a 401
  (`POST /v1/auth/refresh`); the sign-in lasts 7 days from login. Server, email
  and token are kept in
  `~/Library/Application Support/io.lumiverb.resolve/session.json` (mode 0600),
  never the password.
- **Placing on the timeline.** `AppendToTimeline` never overwrites: a clip
  landing on another is dropped without an error. So `plugin/resolve.js` finds
  a free track first, places the clip with `recordFrame` at the playhead, then
  reads the track back to confirm it's there.

## Tests

```bash
node --test clients/resolve/test/
```

They drive the real modules: the API client against a fake `fetch`, and the
Resolve operations against a fake Resolve object.

## Needs

The `project:` / `not_project:` query filters (server PR, "search in a
project or everything but it"). Before they're deployed, In project and Not
in project are ignored by the server and search everything.

## Not verified yet

- Running inside Resolve (written against Blackmagic's sample plugin and the
  21.1 API docs).
- Whether `trackIndex` puts a clip's audio on the audio track with the same
  number. Only the video track is read back.

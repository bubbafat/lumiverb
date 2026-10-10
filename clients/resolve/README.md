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
- **Add to bin** imports the original into the Media Pool's current bin. A clip
  already in that bin isn't imported twice.
- **Add at playhead** puts it on the current timeline at the playhead, on the
  first video track free there (and the audio track with the same number, for
  a clip with sound). When no track is free it adds one. It never covers
  another clip.

## How it works

- `plugin/main.js` is the Electron main process Resolve starts. All Lumiverb
  and Resolve calls happen there; the page (`index.html`, `renderer.js`) is
  sandboxed and reaches it only through `preload.js`.
- **Paths.** Libraries store their root as the Mac sees it
  (`/Volumes/media-01/Media`), so a clip's original is the root plus its
  `rel_path`. A result whose file isn't there says "Not on this Mac".
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

## Not verified yet

- Running inside Resolve (written against Blackmagic's sample plugin and the
  21.1 API docs).
- Whether `trackIndex` puts a clip's audio on the audio track with the same
  number. Only the video track is read back.

# ADR-018: Remote Scanning

## Status

Proposed. From a design discussion with Robert on 2026-10-12. Nothing is built.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Each library names its reader; the reader checks in; the processing page says who scans | Not started |
| 2 | The client: the scan plus analysis proxies, against a remote server; what's due after a change on the server | Not started |
| 3 | Resumable uploads; videos from a client checked and rewritten | Not started |
| 4 | Device keys limited to their libraries; the server admin as a person who signs in | Not started |
| 5 | Quality as notches; the remake choice; changes that keep what exists until it's replaced | Not started |
| 6 | Library admins (roles per library) | Not started |
| 7 | A client that works offline (its own database, synced) | Not started |

## Overview

A server that does everything except read the originals, and a client on the machine that has them. The client scans, makes previews and analysis proxies, and uploads them. The server does the rest: CLIP, faces, OCR, descriptions, scenes, transcripts, faces in video, location and search.

That makes a hosted Lumiverb possible: someone installs a client on their Mac and the server does the work. Originals still never leave the machine they're on (ADR-016, principle 1). Only previews and analysis proxies travel.

## Motivation

- **A server can only process what it can read.** Today the server reads every library's files, on its own disks or over a network mount (ADR-016, roles). A server elsewhere can't mount a Mac at home.
- **Few things need the originals.** Of the producers, only probing (a video's facts), capture (EXIF's time and GPS accuracy) and the analysis proxy read the original, with the scan (hashes, EXIF, previews, thumbnails, video previews). Everything else reads the preview or the analysis proxy (each producer's `needs`, `src/producers/<artifact>/`).
- **The scan already works through the API.** It lists a library's clips, uploads previews and records moves over HTTP (`src/processing/scan.py`), and sends EXIF, capture facts and probe results with their lineage, so the server counts them as made.
- **The uploads are affordable.** Measured on a real library (2,771 videos, 14,436 clips): analysis proxies at about 1.1 Mbit/s, 512 MB an hour of footage; previews 200 KB each; thumbnails 23 KB. The whole library is about 16 GB; an hour of new footage, about 3½ minutes at 20 Mbit/s.

## Decisions

### 1. Each library names its reader

A library is read by **this server** or by **a registered device**, never both at once. It keeps its root as its reader sees it.

- A server that reads some libraries from its own disks and others through devices needs no special mode.
- What's per install today becomes per library: the root map (the reader's own business), the scan's pause, and where a library's folder can be chosen (on its reader).
- A device scans only the libraries it reads.
- The processing page and the Admin page say who reads each library: "Scanning: Robert's Mac Studio · 212 files (14 GB) to upload · checked in 1 min ago", or "not heard from in 3 days". A library read by a device is never shown as "Can't be reached".

### 2. The client works per file

When a file appears or changes, the client reads it once, makes everything that needs the original, and uploads it. Its work list for discovery is the scan's comparison of the folder with the server's listing of the library, as today.

- The client adds the analysis proxy to the scan's pass, and makes one again when the listing says the server doesn't have it (as the scan does for a video preview today).
- Within a run, every file's preview goes first and renders after, so a card of 400 clips is visible before its renders finish.
- No scheduler runs on the client. The server's scheduler leaves a device-read library's file work alone.

### 3. Changes on the server reach the client as a computed list

When something changes on the server that the client must act on (a quality setting, a new producer version, a lost file, an upload that never finished), the server lists what the client could do. The list is **computed from lineage**, as the scheduler's own queue is (`src/server/scheduler/queue.py`), and never stored as a table of jobs.

- Settings that go low → high → medium → low leave nothing to do: what's wanted equals what was made.
- One list covers every reason: a lost preview is missing; a new EXIF reader makes capture due ("send EXIF"); a failed upload is missing.
- Missing work is always due. Stale work is due only when a person chose to remake (decision 6).
- The client asks when the high-water mark moves (decision 4) and when it's idle. It takes a page, works through it, and keeps what's in hand so it never takes a clip twice. What it finishes leaves the list by itself, since the upload records how it was made.

Considered and rejected: the client working it out (every client would need every artifact's lineage and the rules); ignoring changes (new files must use new settings); a table of jobs (flapping settings pile up redundant work).

### 4. A heartbeat and a high-water mark

The client checks in every minute or so while it runs: its version; each library's state (scanning, idle, paused, drive not there); files and bytes waiting to upload; its last full scan.

- **The reply is the server's way to reach the client**, which sits behind a home router: scan now, settings changed, you no longer read this library, too old to continue.
- **It carries a high-water mark per library.** When it moves, the client asks for changes since its own: the files to skip, path filters, its settings, the list in decision 3. The answer may be everything at first (and says so); it can become only what changed without the client changing. Mark 0 is everything, which is also a fresh start.
- **The mark moves only after the change commits.** Otherwise a client could read the new mark, fetch before the change is visible, and miss it until the next change.

### 5. Sync: both ways, one writer per field

Information flows both ways; each field has one writer, so nothing is merged.

- **The client writes** what it reads from files: that a file exists, its path, hash, size, EXIF, probe facts, previews and analysis proxies.
- **The server writes** everything else: human data (labels, people, projects, trash) and what AI makes.
- **The client respects** what people did on the server: the files to skip (trashed, archived, deleted for good, each with its hash: another file at the path is a new clip). It never writes them. The server refuses an upload for a clip that's gone, rather than trusting the client to skip it.
- **People's edits are layers.** Corrected descriptions, OCR and tags, and a place a person set, are kept apart from what's derived, so nothing a client sends can undo them. A future correction (say, of when a clip was taken) is a layer too.
- **The scanning client writes no human data** in this ADR. Editing offline would make human data two-writer, which needs merge rules of its own.
- **Any implementation can be a client.** One may keep its own database (SQLite) to scan while disconnected and sync after. A fresh start rebuilds it from the folder and the server's listing, and keeps the server's clip ids (by path and hash), so human data survives.

### 6. Quality changes and remakes

- **Quality is a notch** (three to five, "good / better / best"), not an encoder's number. Each encoder maps a notch to its own setting (x264 or x265 CRF, VideoToolbox quality, NVENC CQ). Today's defaults (CRF 28; WebP 80) are notches, so nothing becomes stale when the setting is introduced. Retuning a notch is a new producer version.
- **A change to what the client makes asks:** "New files only" (the default) or "Also remake the 12,408 existing ones, after everything else". The choice is stored on the server.
- **Old ones are kept by choice.** Each records how it was made, so the processing page offers "12,408 previews at the earlier quality · Remake" as a standing offer, not a warning; health counts don't treat them as a problem.
- **"After everything else"** means the client's three resources are idle: the originals' drive, the encoder, the upload link. A remake yields at the next file when new work arrives, and waits for mains power and a network that isn't metered.
- **Remaking a preview doesn't redo the AI made from it.** CLIP, faces, descriptions and transcripts stay unless a person asks.

### 7. A change keeps what exists until it's replaced

Robert: "always keep existing data when going through a change that will eventually self-correct. This keeps the system functional as long as possible."

- A remake writes the new artifact beside the old one and swaps them in one step. The old one goes only once the new one is saved, and stays if the new one fails.
- Embeddings (CLIP, faces) can't mix two models in one search. A model change keeps searching with the old model until the new one covers the library, then switches over and drops the old.
- This is about changes that correct themselves, not removal a person asked for: turning location guesses off may drop them.
- It applies to the whole system, not only remote scanning: ADR-016, principle 11.

### 8. Admins and devices

- **Server admin:** the install: accounts, where previews live, network, backups, logs, upload caps. Today this is a key in the install's env file; a hosted server needs a person who signs in. Every setting that belongs to the server, not an account, is theirs alone once accounts share a server. On a Mac with one account, one person is both.
- **Account admin:** the account's people, devices and libraries (`require_tenant_admin` today).
- **Library admin:** one library's settings, its reader, and its people. Roles become per library: someone can edit one library and only view another. Every check that asks "an editor?" then asks "an editor of which library?", and search hides the libraries a person can't see.
- **Device:** not a person. Registered by an account admin, limited to the libraries it reads and to scanning (create clips, upload previews, record moves and missing files), revocable on its own. API keys gain enforced scopes for this.

### 9. Uploads are resumable

A 60-minute analysis proxy is hundreds of MB. One request is the wrong shape for that: a dropped connection or a sleeping laptop at 90% starts over; proxies in front of a server cap request size (nginx at 100 MB in Lumiverb's own config).

- **Three calls:** start (size, hash, what it is) → chunks, each retried alone → finish, where the server checks the hash, then checks and rewrites the video (decision 10) before keeping it.
- **A session sets the terms.** The client opens one with a hello: chunk size, protocol version, codecs and caps, fixed until the session ends (the client restarts, it expires, the server restarts). An admin's change applies to the next session. The heartbeat names its session. A session is ours, not the network connection: behind nginx, HTTP/2 and load balancers, the API can't see those.
- **The server counts bytes received, not chunks**, so an upload started with 16 MB chunks resumes in a later session with 32 MB chunks from the same byte.
- **Shaped like S3's multipart upload:** the server says where each chunk goes. A self-hosted server points them at its API; a hosted one at object storage, so large files never pass through the API servers.
- Small uploads (a photo's preview) can stay single requests.

### 10. What a client sends is less trusted

What the server made itself becomes a client's word. On a hosted server, other accounts' clients are part of the threat.

- **Images:** re-encoded by the server, only JPEG, PNG and WebP read (as every preview already is, `src/server/preview_image.py`).
- **Videos** (analysis proxies, video previews): only MP4 with H.264 or HEVC and AAC; checked, then rewritten without metadata before they're stored; their length measured by the server, never taken from the client. Decoding them (scenes, transcripts, frames) runs in a separate process with limits.
- **Caps**, set by the server admin: bitrate (bytes per second of video) rather than file size, so an hour is fine and an hour in 20 GB isn't; a longest length; storage and processing quotas per account; fair scheduling across accounts, since an hour of uploaded video is an hour of transcription.
- **Facts are claims:** hashes, EXIF, probe facts, paths. Typed and length-limited; good for the account's own catalog, never used across accounts (no spotting the same file in two accounts by hash); escaped wherever they leave the server (editor exports, download names, command lines).
- **The other way:** the client trusts the server's settings only as typed values. Anything that reaches its tools is a number from a known range, never a string.

### 11. Codecs

The client may send H.264 or HEVC. Measured on 17 clips of a real library (2026-10-12, an M4 Max): the first 60 seconds of each, a mix of iPhone H.264 and ProRes, DJI and Sony HEVC, a 5.3K GoPro, 1080p and 720p. Each encoder ran at several settings; every result was scored with VMAF against a lossless copy at the same size and compared at the VMAF of today's setting (x264, CRF 28).

| Encoder | Size at today's quality | Range |
|---|---|---|
| x264 (today's) | 100% | |
| x265 (software HEVC) | 55% | 37–76% |
| VideoToolbox HEVC (the Mac's hardware) | 102% | 68–151% |
| VideoToolbox H.264 | 134% | |

- **HEVC pays only from x265:** about half the upload at the same quality. A full render from a 4K original took about as long as today's (3.5–3.8× realtime against 3.7–3.9×, on 3 clips), since decoding the original is most of the work; the encode itself takes about three times the CPU, which a smaller Mac will feel.
- **The Mac's hardware encoders don't help:** the same size or bigger, and no faster here.
- **So the client defaults to x265**, where the upload matters, and a server making its own analysis proxies keeps x264, which plays everywhere and costs less.
- **Sizes:** with x265 at today's quality, about 260 MB an hour at 960px and about 400 MB an hour at 1280px (720p), audio included; today's encoder made 433 MB an hour on these clips.
- **Notches map per encoder:** today's x264 CRF 28 is about x265 CRF 33 by VMAF.

**Keyframes are set, not left to the encoder.** Scenes are found from the analysis proxy's keyframes alone, with thresholds tuned for keyframes about 2 seconds apart (`src/producers/scenes/detail.py`). Left to themselves, encoders add keyframes at changes of picture: in 60 seconds of a fast-moving clip, x264 as configured placed 50 (as close as 0.23 s apart), x265 38, VideoToolbox 30. Told to place one every 2 seconds and no others, x264 and x265 both give 30. Every analysis proxy, whichever encoder or machine makes it, has keyframes every 2 seconds and nowhere else, so scenes don't depend on who rendered it.

**Downstream, the codec doesn't matter otherwise.** At the same quality the models see the same detail; transcripts hear the AAC either way; a server decodes only keyframes for scenes and a few frames for faces, and a GPU decodes both codecs.

## Alternatives considered

- **The scheduler on the client.** It reads what's due from the database directly; a remote client would need the database exposed. The client's needs are a per-file pass and a list (decisions 2 and 3), not a scheduler.
- **A table of client jobs.** Rejected for decision 3's computed list.
- **The macOS app's scan as the client.** It's a second implementation of the same producers (Apple's image libraries, not libvips), the duplication ADR-016 removed. The first client is the Python scan in the Homebrew package; the app can become its interface. The protocol doesn't care which client sends.
- **Single-request uploads.** Rejected for decision 9.

## Before a hosted server

A server on the internet with many accounts needs TLS, the isolation in decision 10, and the hardening of an internet-facing install, before it takes its first client.

## Open questions

- Does "New files only" by default cover only what the client makes (previews, analysis proxies), or every producer, including the server's AI output? Recommended: only what the client makes, since that's where a remake costs someone else's machine and upload link.
- When to build library admins (phase 6): needed for teams, not for the first client.
- Browser playback of HEVC analysis proxies: the web player streams the analysis proxy; Safari plays HEVC, Chrome where the hardware can, Firefox and Linux browsers don't. Acceptable for device-read libraries, or should the client send x264 (no saving), or the server keep an H.264 copy for those browsers (server work)? AV1 (Firefox and Chrome play it; older Macs' Safari doesn't) is unmeasured.

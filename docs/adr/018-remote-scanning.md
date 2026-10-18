# ADR-018: Remote Scanning

## Status

Proposed. From a design discussion with Robert on 2026-10-12. Under way: see Progress.

The order of the work (Progress) follows decision 12: today's scan moves onto this protocol first, so a library this server reads exercises every call a device will make, before there is a device.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Each library names its reader (this server unless a device is chosen); the scan's pause becomes the library's | Done: `libraries.reader` ("server" until devices) and one check every caller asks (`src/shared/readers.py`: the scheduler's libraries, scans and storage looks, where a folder is chosen); each library's own Scans pause (`POST /v1/libraries/scans/pause`, the account's switch moved onto every library), honoured by the scan's start for whoever reads it |
| 2 | The server compares: the scanner says what it found, the server decides what's new, changed, moved and missing (decision 12); today's scan switched to it | Done: the server compares (#183); the scanner asks it (#184); a scan survives dropped connections, a lost answer and a crash mid-finish (decision 12, Interruptions) |
| 3 | What's due for the originals is one computed list (decision 3): the scan's missing previews and the scheduler's probe, capture and analysis proxies come from it | Done: `src/server/reader_work.py`, `GET /v1/libraries/{id}/reader-work`. Missing previews, which the scan's plan takes from it (`preview_missing`: a lost photo's preview too, where before only a video's that never arrived), and what each producer reading the originals makes, by the rule the scheduler's queue lists by (`lineage.queued`); remakes apart, while the producer's redo runs; nothing paused is listed |
| 4 | The reader checks in; a high-water mark per library for fetching what changed on the server, never a copy of the library (decisions 4 and 13) | Done: `POST /v1/readers/check-in` (this server's scheduler checks in every minute and before each scan pass, in place of reading the library list: each library's state and last full scan; the answer is the libraries it reads, each with its mark and Scan now), `libraries.reader_mark` moved by the database's triggers in the change's own transaction, `GET /v1/libraries/{id}/reader-changes` (everything for now, and says so; the scan takes its path filters from it), `POST /v1/libraries/{id}/scan-now` (Library settings › Scans, with the reader's last check-in). Files to skip aren't sent: a scan's plan skips them (decision 12) |
| 5 | Resumable uploads; videos from a client checked and rewritten; device keys limited to their libraries; the server admin as a person who signs in | Done. Videos checked and rewritten: done (`src/server/uploaded_video.py`: what a browser plays only, 422 `video_not_playable` otherwise; kept as a stream copy with its index first and no metadata; its length measured; the scheduler's own renders go the same way). Resumable uploads: done (`src/server/uploads.py`, `/v1/assets/{id}/uploads` → `PUT {chunk_url}?offset=` → `/finish`; bytes counted, not chunks; the same file started again resumes it; the scheduler sends its analysis proxies so; the session terms are the start's answer and the check-in's protocol). Device keys: done (`/v1/devices`, an account admin's: register, a new key, end its key, rename, remove; its key, role `device`, scans the libraries naming it and nothing else: one gate before every route, `src/server/api/devices.py`, turning it away but where a scan's route says, for a library it reads; `tests/test_device_keys.py` tries it on every route; a new password or the removal of whoever made its key ends it, as any key they made). Choosing a library's device and the web page for devices come with phase 6. The server admin: done (a person signed in, `users.server_admin`, never a key: the install's accounts and server admins, `/v1/admin`; where previews live, the network, backups, the log, `/v1/system`; 403 `server_admin_required` for anyone else, an account's admin too, `dependencies.require_server_admin`, `tests/test_server_admins.py`. Setup's first admin is one; an install with one account made its admins server admins; `lumiverb-server server-admins` names one on the machine. The env file's key stays the install's own, for its upkeep and scripts). Pages to make accounts and name server admins in the web app come later |
| 6 | Libraries a device reads: the scheduler leaves their file work alone; the client (the scan plus analysis proxies) against a remote server; the processing page says who scans | Done. Choosing a library's reader: done (`PUT /v1/libraries/{id}/reader`, or `reader` on creating one: this server or a registered device; the database holds it to those; only the reader starts, answers and finishes a scan, 409 `not_the_reader`, so a scan started before a handover changes nothing). The scheduler's split: done (what needs the originals only in the libraries it reads; what's made from previews and analysis proxies in every active library, a device's too; a device's due work on its originals is the `device` wait on Processing; a device's library isn't on the Admin page's storage row). The device client: done (`lumiverb device run`, `src/processing/device.py`: checks in, scans as the scheduler does and makes what needs the originals through the same producers' code and runner; failures reported for its own clips; reported changes read and acknowledged). The web pages: done (Settings › Devices: register, the key shown once with the commands for the device, rename, a new key, end its key, remove; a library's "Who reads it" and Add library's Read by; libraries say their reader's name, `reader_name`). Who scans: done (Processing's Scans rows and the Admin page's libraries say who reads each, from its check-ins: "Read by Studio Mac · scanning · checked in 1 minute ago", amber when not heard from for 5 minutes or it can't reach the folder; a device's library is never "can't be reached": `src/ui/web/src/lib/readers.ts`) |
| 7 | Quality as notches; the remake choice | Done. The remake choice: done (a change of a producer's settings or an AI job's model asks "New files only" or "Also remake them, after everything else", `redo` false or true on `PUT /v1/producers/{artifact}/settings` and `PUT /v1/ai/jobs/{job}`; new files only is stored as the producer's redo stopped by that person, `lineage.keep_made`; Settings › Processing says what's made another way plainly, not as a warning, with Remake as a standing offer). The analysis proxy's quality as a notch: done (`quality`, good / better / best, a `notch` setting standing for x264's CRF 32 / 28 / 23, NVENC's CQ two more; in lineage as the CRF, `shared/producers.resolved`, so better, the CRF 28 of before, made nothing stale; an account's CRF became its nearest notch, one no notch stands for holding the remakes for an admin, tenant migration `e9a4c2f7b1d3`). Previews' and video previews' quality as notches: done (a scan makes new files' with the account's, as reader-changes' `made_with` says, `processing/scan.py` `Previews`; the server keeps previews and thumbnails at it, `preview_image.quality`; better is what they were made with, so nothing is made another way). Previews remade: done (by scans: an unchanged file whose previews were made another way than now is uploaded again, plan reason `remake`, after the new and changed files, at most 250 a scan, while the previews' redo runs; Settings › Processing offers Remake and Stop for them as for any producer, `src/server/scans.py` `_remakes`). A device's remakes after everything else: done (they stop at the next clip when new work may have come, a scan due or asked for or the server's mark moved, `device.py` `_Remaking`; they wait on battery or a metered network, `src/processing/power.py`: a Mac's pmset, Linux's power supplies and NetworkManager, a Mac's network never taken for metered as nothing says it; its scans then plan no remake, `remakes: false`; its check-ins say why, `remakes_held`, on Processing's device wait and the reader line). A remake is never yet held back for a busy upload link or drive beyond that: what a device is doing is its own, one thing at a time |
| 8 | Library admins (roles per library) | Under way (decision 8, Roles per library). Stored: done (`library_roles`, a person's role on a library where it isn't their account role's; `libraries.private`; `users.every_library`; the rule, `src/server/repository/library_roles.py`; nothing set yet, so everyone keeps the role they have on every library). Every route asks it: done (one gate before every route, `src/server/api/library_access.py`: what a request names by its path, body, form or query, in a library the person can't see, is a 404; editing needs an editor's role on the library; `tests/test_library_access.py` walks every route). Next: what spans libraries (search, browsing, projects, people, public links) shows only what they can see; then setting roles, the library's admin, and the web pages |
| 9 | ~~A client that works offline (its own database, synced)~~ Dropped: a client keeps no copy it decides from (decision 13). A cache for speed comes only once measurements show it's needed | Dropped |

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
- **It carries a high-water mark per library.** When it moves, the client asks for changes since its own: the files to skip, path filters, its settings, the list in decision 3. The answer may be everything at first (and says so); it can become only what changed without the client changing. Mark 0 is everything, which is also a fresh start. The mark is only for fetching the server's changes; it never syncs a copy of the library to the client (decision 13).
- **The mark moves only after the change commits.** Otherwise a client could read the new mark, fetch before the change is visible, and miss it until the next change.

### 5. Sync: both ways, one writer per field

Information flows both ways; each field has one writer, so nothing is merged.

- **The client writes** what it reads from files: that a file exists, its path, hash, size, EXIF, probe facts, previews and analysis proxies.
- **The server writes** everything else: human data (labels, people, projects, trash) and what AI makes.
- **The client respects** what people did on the server: the files to skip (trashed, archived, deleted for good, each with its hash: another file at the path is a new clip). It never writes them. The server refuses an upload for a clip that's gone, rather than trusting the client to skip it.
- **People's edits are layers.** Corrected descriptions, OCR and tags, and a place a person set, are kept apart from what's derived, so nothing a client sends can undo them. A future correction (say, of when a clip was taken) is a layer too.
- **The scanning client writes no human data** in this ADR. Editing offline would make human data two-writer, which needs merge rules of its own.
- **Any implementation can be a client**, as long as it keeps no copy it decides from (decision 13): whatever it keeps is a cache it can drop, and a fresh start loses nothing.

### 6. Quality changes and remakes

- **Quality is a notch** (three to five, "good / better / best"), not an encoder's number. Each encoder maps a notch to its own setting (x264 CRF, NVENC CQ). Today's defaults (CRF 28; WebP 80) are notches, so nothing becomes stale when the setting is introduced. Retuning a notch is a new producer version.
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

#### Roles per library (phase 8)

A person's role on a library is one rule (`src/server/repository/library_roles.py`):

1. An account admin is "admin" on every library.
2. Otherwise, the role set for them on that library, if there is one: "admin" (the library's admin), "editor", "viewer", or "none" (they don't see it). It may be above or below their account role: an account viewer can edit one library.
3. Otherwise "none" on a private library (only its people see it), and "none" everywhere for someone who sees only the libraries they're given (`users.every_library` false: a contractor, say). A new library is then hidden from them until someone adds them.
4. Otherwise their account role, so an account with nothing set works as it did.

A key a person made acts as them, never above its own role. A key no person made (the scheduler's, an operator's) and a device's are not people: the first has its role on every library, the second the device gate's.

What follows from it:

- **A library they can't see doesn't exist for them.** Every route that names a library, a clip, a scan or an upload answers 404 for one in a library they can't see, as for a device. Editing needs "editor" on the clip's library, not in the account.
- **What spans libraries shows only what they can see.** Search, browsing, facets, similar, the map, trash, export.
- **Projects** show only the clips they can see, and say so in their counts. A project none of whose clips they can see is not listed, unless they made it.
- **People** are listed when they have a face in a library the viewer can see; the faces shown are those.
- **A public link** shows what the person who made it can still see. Taking a library away from them shrinks the link.
- **The account role** still decides what belongs to the account rather than a library: projects, people's names, saved searches. The account admin keeps the account's people, devices and libraries; a library's admin its settings, its reader and who has which role on it.

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
- **Videos** (analysis proxies, video previews): only what a browser plays (decision 11): MP4 with its index first, H.264 8-bit 4:2:0 and AAC; checked, then rewritten without metadata before they're stored; their length measured by the server, never taken from the client. Decoding them (scenes, transcripts, frames) runs in a separate process with limits.
- **Caps**, set by the server admin: bitrate (bytes per second of video) rather than file size, so an hour is fine and an hour in 20 GB isn't; a longest length; storage and processing quotas per account; fair scheduling across accounts, since an hour of uploaded video is an hour of transcription.
- **Facts are claims:** hashes, EXIF, probe facts, paths. Typed and length-limited; good for the account's own catalog, never used across accounts (no spotting the same file in two accounts by hash); escaped wherever they leave the server (editor exports, download names, command lines).
- **The other way:** the client trusts the server's settings only as typed values. Anything that reaches its tools is a number from a known range, never a string.

### 11. Codecs

**Every video Lumiverb keeps is one a browser plays**, whoever makes it: H.264, 8-bit 4:2:0 (High profile at most), AAC at most stereo, in an MP4 with its index first (faststart). The client makes x264, as the server does, and the server takes nothing else from a client (decision 10). The web player streams the analysis proxy, or the preview until it exists (`src/server/api/routers/playback.py`), and copies them as they are; nothing re-encodes for a browser. `tests/test_browser_playable_video.py` holds both renders to this.

Robert, 2026-10-14: "Revert to h264 high compatibility MP4's for preview/proxy. Sucks but the estimate is 70% of users impacted." HEVC halves the upload (below), but Firefox and Linux browsers don't play it, and Chrome only where the hardware decodes it. A second, H.264 copy for those browsers would be server work that gives the saving back. This closes the question of HEVC in the browser.

What was measured, for when browsers change (or for AV1, which Firefox and Chrome play and older Macs' Safari doesn't; unmeasured): 17 clips of a real library (2026-10-12, an M4 Max): the first 60 seconds of each, a mix of iPhone H.264 and ProRes, DJI and Sony HEVC, a 5.3K GoPro, 1080p and 720p. Each encoder ran at several settings; every result was scored with VMAF against a lossless copy at the same size and compared at the VMAF of today's setting (x264, CRF 28).

| Encoder | Size at today's quality | Range |
|---|---|---|
| x264 (today's) | 100% | |
| x265 (software HEVC) | 55% | 37–76% |
| VideoToolbox HEVC (the Mac's hardware) | 102% | 68–151% |
| VideoToolbox H.264 | 134% | |

- **HEVC pays only from x265:** about half the upload at the same quality. A full render from a 4K original took about as long as today's (3.5–3.8× realtime against 3.7–3.9×, on 3 clips), since decoding the original is most of the work; the encode itself takes about three times the CPU, which a smaller Mac will feel.
- **The Mac's hardware encoders don't help:** the same size or bigger, and no faster here. So the client makes x264, not VideoToolbox H.264.
- **Sizes:** x264 at today's quality made 433 MB an hour on these clips, audio included; x265 would make about 260 MB an hour at 960px and about 400 MB an hour at 1280px (720p).
- **Notches, if HEVC comes back:** today's x264 CRF 28 is about x265 CRF 33 by VMAF.

**Keyframes are set, not left to the encoder.** Scenes are found from the analysis proxy's keyframes alone, with thresholds tuned for keyframes about 2 seconds apart (`src/producers/scenes/detail.py`). Left to themselves, encoders add keyframes at changes of picture: in 60 seconds of a fast-moving clip, x264 as configured placed 50 (as close as 0.23 s apart), x265 38, VideoToolbox 30. Told to place one every 2 seconds and no others, x264 and x265 both give 30. Every analysis proxy, whichever encoder or machine makes it, has keyframes every 2 seconds and nowhere else, so scenes don't depend on who rendered it.

**Downstream, only the browser cares about the codec.** At the same quality the models see the same detail; transcripts hear the AAC either way; a server decodes only keyframes for scenes and a few frames for faces, and a GPU decodes both codecs.

### 12. Today's scan moves onto this protocol

Robert agreed all three (2026-10-12). Today's scan already works only through the API: the scheduler's scan pass (`src/server/scheduler/scans.py`) calls the same `run_scan` as `lumiverb scan` (`src/processing/scan.py`), over HTTP. What changes is the shape of the conversation and which side decides.

- **One protocol, and this server is one of its readers.** A library this server reads makes exactly the calls a device makes, so every one of them runs every day on a Mac and on Linux before there is a device. The Python scanner is the reference client, for the scheduler and for `lumiverb scan`.
- **The client says what it found; the server decides.** The client sends each file's path, size, modified time and content hash, with what it read from the file (EXIF, probe facts). The server decides what's new, changed, moved (with "follow moves"), missing (archived, with the guard against taking most of a library for missing), and what's skipped because a person removed it. Before phase 2 the client worked these out itself (`_detect_moves`, `_detect_deletions`, `_preview_missing` in `src/processing/scan.py`, gone since); every client would need that logic, and it's the part a less trusted client shouldn't own (decision 10). `lumiverb scan`'s prompt about moved files becomes the server's question, the 409 every client must ask (`DecisionRequiredError`).
- **On this server, probing, capture and analysis proxies stay the scheduler's producers**, which already share the GPU, the decoders and the pace. They take their work from the same computed list a device does (decision 3), so the rules are in one place either way.

**Behaviours carried over**, each with its new home:

| Today | Decided by | Under this protocol |
|---|---|---|
| The walk: path filters, hidden files, no symlinks, control characters, folders the server never reads, names in NFC | the client | the client; filters and refused folders from the server |
| Unchanged when size and modified time match; `--thorough` hashes everything | the client, against the library's whole listing | the server, against the listing each scan sends (no client record: decision 13) |
| Files a person trashed, archived or deleted for good, by content | the client (`/ignored-paths`) | the client skips them; the server refuses them anyway (decision 5) |
| Moves: the same content at a new path | the client | the server |
| Missing files archived; restored when they come back; the mass-missing guard | the client, the guard on the server | the server |
| A video preview that never arrived is made again | the client | the server's list (decision 3) |
| Previews, thumbnails, video previews, EXIF | the client | the client; the server re-encodes and checks (decision 10) |
| Probing, capture, analysis proxies | the server's producers, reading the originals | the library's reader, from the list |
| Changes a Mac reports, a full scan every 15 minutes (once a day until Oct 13), a file's retries backing off, a folder that can't be listed never taken for deleted | the server's scheduler | the reader (for this server's libraries, its scheduler) |
| Whether the drive is there | the scheduler's own look (since #179) | the reader, in its check-in |

**Phase 2 is the risk**, since it's where moves and missing files change sides. Its tests run the old and new comparisons over the same folders and server listings, and must agree.

**Interruptions** (2026-10-13, before a remote client depends on scanning). Every call but the start is safe to send twice, a repeat getting the answer the first got (the session keeps the plan's counts and the moves' and finish's answers), so the client tries each again through a dropped connection or the API restarting. The finish is one transaction: the archive, the handovers to copies and the scan's close, or none of them. Scans a killed client left are deleted by upkeep once expired. A library whose scan keeps failing waits a minute, then longer each time up to half an hour, and the Processing page's Scans row says so (`scan_failing`). A scan interrupted for longer still starts over at its next try (nothing lost, nothing wrongly archived): it costs a walk and a listing, so resuming one where it stopped waits until that's shown to matter (decision 13: the client keeps no record of what it sent).

### 13. One source of truth for each fact

Robert, 2026-10-13, worried that remote scanning turns one source of truth into two: the server's, and a client's view of it. Then, on reflection: a client is a source of truth, since it speaks for its file system. So the rule is one source for each fact, and no copies:

- **The file system is the truth for files:** which exist, and their content, size, time and EXIF. Its reader speaks for it (this server for its own disks, a client for a device's), as it reads it now. That's the client's side of decision 5's one writer per field.
- **The server is the truth for everything else:** clips, people's data, what AI made.
- **A client keeps no copy it decides from:** neither of the server's truth nor of its file system as it was. Each scan reads the disk and tells the server what's there; the server decides (decision 12). This is how phase 2 works: the client holds nothing between scans, and the scan session it fills is the server's, gone within a day.
- **Anything a client keeps is a cache for speed:** dropping it loses nothing, since the next scan rebuilds it from the disk and the server. No decision that changes data is made from a cache.
- **A full scan compares the disk with the server directly**, never with a cache, so drift is corrected.
- **A high-water mark fetches the server's changes** (the files to skip, filters, settings, the list in decision 3); it never syncs a copy of the library (decision 4).
- **No offline client** (phase 9 is dropped): editing or scanning against a client's own database would be a second truth with merge rules of its own.
- **No cache until it's proven needed.** Robert: start with it running without a cache, "and then we can build that when we've proven it's needed." The cost is a full listing sent with every scan, measured at about 20 µs a file (#184): nothing at home, a couple of MB for a full scan of 100,000 files over a slow link.

## Alternatives considered

- **The scheduler on the client.** It reads what's due from the database directly; a remote client would need the database exposed. The client's needs are a per-file pass and a list (decisions 2 and 3), not a scheduler.
- **A table of client jobs.** Rejected for decision 3's computed list.
- **The macOS app's scan as the client.** It's a second implementation of the same producers (Apple's image libraries, not libvips), the duplication ADR-016 removed. The first client is the Python scan in the Homebrew package; the app can become its interface. The protocol doesn't care which client sends. The app's own scan is removed (Oct 14): it browses, and the server's scheduler scans every library.
- **Single-request uploads.** Rejected for decision 9.
- **A client-side record of the library, synced** (the client's own database, from the start). Rejected for now for decision 13: a second truth to keep in step, for a speed nobody has shown is needed.

## Before a hosted server

A server on the internet with many accounts needs TLS, the isolation in decision 10, and the hardening of an internet-facing install, before it takes its first client.

## Open questions

- Does "New files only" by default cover only what the client makes (previews, analysis proxies), or every producer, including the server's AI output? Recommended: only what the client makes, since that's where a remake costs someone else's machine and upload link.

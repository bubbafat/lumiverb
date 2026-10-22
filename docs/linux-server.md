# A Linux server: setup and operation

Prod is the Linux box `media` (LAN 192.168.86.166, Tailscale 100.94.35.123). It runs Lumiverb, reads the DAS, and does all processing: one scheduler ranks every job and runs it here (ADR-016 phases 2 and 4). The Mac Studio keeps the DAS and editing, and browses. Nothing there scans: the scheduler's full scan (every 15 minutes unless set otherwise, How it runs) finds new and changed files.

```
Mac Studio ── DAS (media-01, media-02)
    │  SMB over 10GbE (10.10.10.1)          scanned by the scheduler, every 15 min
    ▼
media: /mnt/media-01, /mnt/media-02  ──►  lumiverb-scheduler ──►  API :8100  ◄── nginx :80 ◄── web, Mac, iOS
                                           (every job, ranked;   Postgres 18 :5434
                                            AI on Settings → AI) (data and search)
```

## Ports on this box

Settled so prod and dev run side by side. Don't use other ports without updating this table.

| What | Dev (unchanged) | Prod |
|---|---|---|
| Postgres | 5433 (Docker, pgvector:pg18) | **5434** (Ubuntu postgresql-18 + pgvector) |
| API | 8000 (uvicorn --reload) | **8100**, 127.0.0.1 only |
| Web | 5173 (Vite, 127.0.0.1 only) | **80** (nginx, any address, HTTP) |

Already taken by other things: 5432 (DaVinci Resolve's Postgres), 8080 (ResourceSpace), 3001 (Resolve pgAdmin), 9000 (thelounge). This box gets no firewall from the scripts (`--no-firewall`): ufw would cut those off.

## Setup (your steps: sudo and the Mac)

### 1. Where the DAS is on the Mac

Libraries keep the root the Mac sees, because exports point Resolve, Premiere and Final Cut there. Prod maps it to its own mount:

| On the Mac | On `media` |
|---|---|
| `/Volumes/media-01` | `/mnt/media-01` |

The first library is `/Volumes/media-01/Media`: about 14,400 photos and videos, 4,400 of them videos. Paths match case-sensitively, so use the folder names as the Mac shows them (`Media`, not `media`). To add `media-02` later, rerun the install with another `--root-map`, or add it to `LUMIVERB_ROOT_MAP` in `/etc/lumiverb/env` (JSON, single-quoted: `LUMIVERB_ROOT_MAP='{"/Volumes/media-01": "/mnt/media-01", "/Volumes/media-02": "/mnt/media-02"}'`) and restart the scheduler; `lumiverb config map-root` as the `lumiverb` user maps it for that user's CLI (`library list`). The scheduler reads only its environment, never the CLI's config (`src/server/scheduler/settings.py` lists its settings). It reads a library only through the map: a root not under a mapped prefix, or one that resolves outside the mapped folders or into the server's `DATA_DIR`, is refused (`Library <name> (<id>) refused: …` in its log).

### 2. Install the API and the scheduler

From this checkout (`~/src/lumiverb`):

```bash
sudo bash scripts/deploy-api.sh --app-host http://192.168.86.166 --pg-port 5434 --api-port 8100 --no-firewall --data-dir /mnt/ssd2/lumiverb --worker --root-map /Volumes/media-01=/mnt/media-01
```

Add `--dry-run` to see the settings it will use without changing anything. Once installed, it needs sudo too: the remembered settings are in `/etc/lumiverb/env`, which only root can read.

- **Data dir** `/mnt/ssd2/lumiverb` (proposed): 3.4 TB free. Previews, stills and analysis proxies live there. Analysis proxies take about 0.4 GB per hour of footage, plus about 20 MB per hour for each extra stereo audio track. The scheduler's caches go there too (`cache/`), not on the root disk, and its temp files, such as the audio Whisper reads (`worker-tmp/`), not in RAM. Before making anything, the install refuses a data dir whose drive isn't there: under a mount point `/etc/fstab` lists that nothing is mounted at, or a new folder `--data-dir` names on the system disk while a disk with nothing mounted is attached (make the folder first to keep it on the system disk).
- **Memory**: under 4 GB with no swap, the install says so, first and at the end: finding faces, CLIP and Whisper can need more than that at once, and the kernel stops what runs out. It doesn't add swap.
- **Postgres 18** comes from Ubuntu's own packages, and the cluster is created on 5434. Resolve's database on 5432 is never touched. Search is PostgreSQL's own, in each account's database (ADR-019): there's no search service to install.
- **The scheduler** (`--worker` installs it) runs as the `lumiverb` user. Everything outside the data dir is read-only to it, its home and the DAS mounts included, whatever the mount options say: what it runs keeps nothing in its home (its caches, matplotlib's settings and CUDA's compiled kernels are in the data dir's `cache/`).
- **Root and the `lumiverb` user.** The `lumiverb` user owns the checkout, the data dir and its home, so nothing it could have written steers what root does: the install and every update run git, uv and npm as `lumiverb` without that user's own settings for them (`~/.gitconfig`, `~/.npmrc`, `~/.config/uv`), root runs no git in the checkout, a symbolic link where Lumiverb keeps a folder in the data dir (`tmp`, `worker-tmp`, `cache`, `plugins`) stops the run with what to remove, `DATA_DIR` in `/etc/lumiverb/env` must be one full path on one line (no spaces, quotes or `$ % ; \`), and what's carried from that user's CLI config into `/etc/lumiverb/env` is read as that user and taken only as a single line of the type its setting takes (anything else is left out, and said). The upkeep timers hand curl the install's key on its stdin, never on a command line, which every local user can see; the first install's own downloads go in a folder of root's own.
- **Its processes and their secrets.** The API (uvicorn's own process and each worker), the scheduler and the plug-in runner make themselves not dumpable as they start, so another process of the `lumiverb` user (a plug-in's) can't read their environment (`/etc/lumiverb/env`'s secrets) or their memory; and the tools the API and the scheduler start get none of those secrets but the one they need (a backup's pg_dump, its database's password). The plug-in runner keeps only the databases' addresses and the sealing key. PostgreSQL lets the `lumiverb` user in only with the password: Ubuntu's own `pg_hba.conf` lets a local user in as the role of its own name, and the install's role is `app`, with nothing added for `lumiverb`.
- **Reruns are safe.** They keep the ports, the data dir, the branch, the Postgres version, `--no-firewall` (until a run with `--firewall`), the tenant, the keys and any settings added to `/etc/lumiverb/env` by hand, so `sudo bash scripts/deploy-api.sh` alone is enough. `deploy-web.sh` follows the same branch and firewall setting. A rerun on an install that still has Quickwit leaves it, and says the update removes it (Updating, below).

### 3. Install the web UI

```bash
sudo bash scripts/deploy-web.sh --domain _ --api-upstream http://127.0.0.1:8100 --no-firewall
```

Lumiverb is then at http://192.168.86.166 and http://100.94.35.123.

`--domain` is a domain name (or `_` for any name or address) and `--api-upstream` the API's address, `http://HOST:PORT` (a name, an IPv4 or a bracketed IPv6 address): both go into nginx's config as they're typed, so anything else is refused before anything is installed.

### 4. Create your user

The install has an account but nobody to log in. Make its first admin on
the command line; it asks for a password (12 characters or more):

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server setup --env /etc/lumiverb/env --email you@example.com
```

Or in the web app: its setup page appears while there's nobody to log in,
and only works from the server itself, so from another machine reach it
through an SSH tunnel (`ssh -L 8080:localhost:80 media`, then
http://localhost:8080). Both are the same step (`src/server/setup.py`), and
closed for good once there's a user. Finish it before putting a tunnel or
proxy on the box in front of Lumiverb (`tailscale serve`, cloudflared,
ngrok): its requests arrive from the box itself, so until then they could
set it up too. Later users: `lumiverb user create`,
or the web app's Admin › Users.

### 5. Create libraries with the Mac's paths

One per top-level area you want searchable:

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb library create --name "Media" --path "/Volumes/media-01/Media"
```

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb library list
```

`library list` shows a **Here** column with the mapped path. Within a minute the scheduler starts the first full scan. That is the fresh ingest of the DAS: probe, poster and preview, then analysis proxies, transcripts and scenes.

**Several accounts on one server.** The root map bounds what the scheduler reads for every account at once: without more, any account's admin can point a library at any folder under it, another account's too. A server admin gives each account its own folders (Admin › Accounts › Change folders, or `PUT /v1/admin/tenants/{id}/folders` with the install's `ADMIN_KEY`), each under the root map and none inside another account's. A library the server reads for that account is then made, moved and handed to the server only inside them (403 `outside_account_folders`), and the scheduler reads it only where its folder, resolved on disk, leads inside them: a library whose folder is a link to somewhere else is never read, and the scheduler's log says so once ("root leaves its account's folders"). An account with none of its own (every account's until given some, and all a one-account server needs) reads anywhere under the map, as before; Admin › Accounts says which, while there's more than one account.

### 6. AI machines

Descriptions, OCR, scene descriptions, video descriptions (written from a video's scene descriptions) and transcripts run on the account's **AI machines** (Settings → AI): GPU boxes, each an OpenAI-compatible endpoint such as Ollama, with the jobs it does and how many requests it takes at once. The job's one model must be on every machine doing it; until a model is chosen and a machine offers it, those steps wait rather than fail. The worker reads all of this from the server and keeps none of its own (a `vision_*` key left in an older worker config is ignored).

The model is `qwen3-vl:8b-instruct`. Plain `qwen3-vl:8b` is the thinking variant: its reasoning used up the 500-token answer budget, so most descriptions came back empty or cut off (Oct 8). Every machine doing descriptions needs it pulled (`ollama pull qwen3-vl:8b-instruct`).

On Oct 8 there are two:

| Machine | URL | At once |
|---|---|---|
| `media`'s 3080 | `http://10.10.10.2:11434/v1` | 2 |
| Mac Studio (over the direct 10 Gb link) | `http://10.10.10.1:11434/v1` | 4 |

**Prod's Ollama** is Lumiverb's own: `~/ollama/docker-compose.yaml` (`docker compose -f ~/ollama/docker-compose.yaml up -d`), with the image digest and model folder (`/mnt/ssd1/ollama-models`) it had as ResourceSpace's `ollama` service. Its port is published on every address of `media`, so it's at a fixed address: 10.10.10.2 (the direct link to the Mac), localhost, and the LAN. Ollama has no password. Until Oct 8 Lumiverb borrowed ResourceSpace's Ollama at a Docker-internal address, which changed when that container restarted and stopped vision. ResourceSpace is retired: its containers are stopped with `restart=no`. If its compose file is ever started again, take the `ollama` service out of it first: it would take the same container name and GPU. Every update (`update.sh`) checks that containers using the GPU still have it: systemd reloading its units takes the GPU from running containers, and Ollama then quietly runs its model on the CPU. One that lost it is restarted; `docker exec ollama nvidia-smi` shows the GPU when all is well, and `curl -s localhost:11434/api/ps` a non-zero `size_vram`.

A cloud API is a machine too, with its key: OpenAI's (`https://api.openai.com/v1`) or Anthropic's (`https://api.anthropic.com/v1`; its models list is asked for with the API version it wants). A model that refuses any temperature (Claude 4.7 and later, OpenAI's reasoning models) is asked without one from its first refusal on, and answers with its own.

To add a machine, as an admin: **Settings → AI → Add machine**, a name, the URL, **Connect** (it lists the models the machine offers), tick **Descriptions & text**, how many at once, **Add**. "At once" is how many images it works on together; more needs more GPU memory. It only helps if that Ollama runs that many in parallel (`OLLAMA_NUM_PARALLEL`); otherwise the rest wait there. A request is about 2k tokens (a 1280 px image, the prompt, a 500-token answer), so any context of 8k or more is plenty.

Every minute the scheduler checks every machine (and each one again before any work once the model changes), and sends work only to those online and offering the model, spread by how many each takes. Descriptions, OCR and scene descriptions each keep as many requests going as the online machines take together; scenes from several videos go out together, so short clips don't leave a machine idle. One that stops answering (asleep, out of memory, the model gone) is skipped and its work goes to the others; it's checked again a minute later. Settings → AI shows each machine's state, offline ones in red, with a red dot on AI in the Settings menu. Only when no machine is left does vision work wait, and no clip is charged a failure.

**Transcripts** are a job too, with one model (`small` until changed in Settings → AI → Models). Every account has a **Built in** machine: the scheduler's own Whisper (faster-whisper, on the 3080 here), doing transcripts one clip at a time, with nothing to set up. It has no URL; it can be renamed, given more at once (each one loads the model on the GPU beside Ollama: `small` takes about 1 GB, `large-v3` about 4 GB) or turned off, not removed. The worker checks it before transcribing (faster-whisper installed, the model one it knows), and a model that won't load moves its clips to another machine.

To transcribe on another GPU as well, run an OpenAI-compatible Whisper server there and add it like any machine, ticking **Transcripts**. [speaches](https://speaches.ai) is the one that fits: it serves faster-whisper with `GET /v1/models` and `POST /v1/audio/transcriptions`, and lists the same models by their Hugging Face names (`Systran/faster-whisper-small` is `small`). Download the job's model into it first (speaches: `POST /v1/models/Systran/faster-whisper-small`), or Connect won't list it. On a Mac, speaches runs on the CPU only. LocalAI's whisper backend (whisper.cpp, on Metal) works too: it lists each model by the name its config gives it, so give the job's name only to a build of the same model (`ggml-small.bin` for `small`). A bare whisper.cpp server lists no models, so Lumiverb can't check it has the right one: not supported. Nor is OpenAI's own API (25 MB a request).

Whichever machine hears a clip, the scheduler finds its speech first (faster-whisper's VAD, skipping silences of 500 ms or more, the transcript producer's setting) and sends only the speech, then puts the times back onto the clip. So the model and silences a transcript records are the ones it was made with, on any machine. How a server decodes isn't tracked, like which GPU it ran on: current speaches also skips silences over 160 ms within what it's sent and decodes pieces of at most 30 s at temperature 0, so its cues and an occasional word differ from the built-in's (on the Spanish test clip it heard "eras" where the built-in heard "eres").

A machine that doesn't answer, breaks off or can't load the model is skipped and its clips go to the others. A clip a machine refuses (too big for a proxy in front of it, say) goes to the others first. One that a machine is still working on after ten minutes and four times its speech, or that makes Whisper die, is marked failing and tried again later, 5 minutes, then 10, doubling to a day.

### 7. The Mac app

A browse client: it scans nothing and processes nothing. Its own AI (Apple Vision faces and OCR, CoreML ArcFace and CLIP, FeaturePrint, whisper.cpp) and its own scan are gone: the scheduler does it all. It reports no changes either: the full scan every 15 minutes finds new files (How it runs, below), and `lumiverb library report-changes` says one at once. Not pointed at prod yet (your call, Oct 8). A Mac can still serve a model as an ordinary endpoint, such as Ollama, as long as the model isn't Mac-specific.

### 8. The DAS mounts

`/mnt/media-01` and `/mnt/media-02` stay read-write in `/etc/fstab` (your call, Oct 8): Lumiverb can't write there anyway, because the scheduler's sandbox makes everything outside its data folder read-only.

The fstab lines don't say `soft` or `hard`, so the mounts are `soft`, the CIFS default: when the Mac Studio sleeps, reads fail instead of hanging forever. Never add `hard`: a sleeping Mac would then hang the worker's scans and renders indefinitely.

Keep the DAS mounts in `/etc/fstab`, not in hand-written systemd mount units or autofs: the scanner reads fstab to tell an unmounted share from a share that has lost some files. For a share fstab doesn't list only the empty-folder check is left, so stray files in its unmounted mount point would look like the whole library, and everything else would be archived (your call, Oct 8).

## How it runs

- **One queue.** The scheduler ranks every job, in every library: first what you see (scans, which make thumbnails and previews, and probes), then analysis copies, then the AI and the rest that makes clips findable (CLIP, descriptions and tags, text in images, transcripts, faces, scenes, scene descriptions). Within a tier, oldest first (in the order clips were added). Each resource has its own slots: scans one at a time, probes 2, analysis copies as below, CLIP and faces taking turns on the GPU (25 clips a batch), scenes 1, and the AI machines as many at once as Settings → AI says. A free slot takes the best job that uses it, so AI keeps going while renders wait on the storage, and a long backlog of one kind never holds up another.
- **Changes.** Paths reported changed (`POST /v1/changes`: `lumiverb library report-changes`, below, here or on the Mac) don't wait for the next full scan. Every 30 s the scheduler scans the one folder that covers a library's reported changes, then acknowledges them. A file modified in the last 30 seconds may still be copying, so it waits for the next look (one stamped more than 5 minutes in the future came from a camera clock running ahead, and doesn't wait). When a scan finds most of a library missing, the server asks first (409 `mass_missing`) and the scheduler doesn't answer: nothing is archived, and its log names the library and the count. If the files really are gone: `lumiverb scan --library <name> --allow-mass-delete` (on the server, after `lumiverb config map-root`, or on the Mac).
- **Files that fail.** A file that fails to scan doesn't hold back the changes around it. It's tried again on its own after 5 minutes, then 10, 20 and so on, up to once a day. Until then other scans, full scans too, leave it alone (it's on disk, so never archived as missing), unless its size or modified time changed since it failed: a file replaced is tried at once. A folder that can't be listed (no permission, or the share timing out mid-scan) keeps its changes for the same retry, and that scan removes nothing. A clip whose processing fails is tried again after 5 minutes, doubling up to a day, and given up after 10 tries (about two days); Settings → Processing ("Show failures") and `lumiverb producers failures` list failing clips with their errors, and Try again (`lumiverb producers retry`) starts over. One that waits without failing (the GPU out of memory, the AI machines down, its file gone since the last look) isn't charged, and waits an hour before the scheduler takes it again, unless someone asks for it to be tried again.
- **Full scans.** Each library is scanned in full every 15 minutes. That's how new and changed files are found (nothing reports changes on its own: the Mac app scans and reports nothing), and it catches a report missed. A big library on a network share takes longer to walk: `LUMIVERB_FULL_SCAN_EVERY_MIN` in `/etc/lumiverb/env` sets it, in minutes (`LUMIVERB_FULL_SCAN_EVERY_MIN=120`), read when the scheduler starts; it won't start with one that isn't a positive number, and its log says why. When each was last scanned in full, and which files and folders wait for a retry, are kept in `worker-state.json` beside the scheduler's lock (in `/mnt/ssd2/lumiverb/cache/lumiverb`; the names are the old worker's, so it carried them over), so a restart or deploy doesn't rescan everything; delete it to force full scans.
- **The Mac Studio asleep.** The scheduler doesn't scan its libraries. A folder `/etc/fstab` lists as a mount counts only while it's mounted, and an empty folder never counts, so an unmounted share is never scanned. Work from analysis proxies goes on; only probing and rendering wait, and they start as soon as the storage is back.
- **Missing files are archived.** On a healthy mount, a file the scan no longer finds is archived (marked missing) at once: its asset keeps ratings, projects, faces and transcripts. When the file comes back, at the same path or anywhere else in the library (matched by content), the asset is restored. Archiving is reversible, so nothing is held back, however many go at once. Archived clips (missing, or archived by a person) aren't deleted on their own: only by moving them to the trash, or with their library. The trash deletes what's been in it longer than the trash days (30 unless changed in Settings → Files) on the 5-minute upkeep timer.
- **Video first on the GPU.** In Settings → AI, a machine on this box (its Ollama) is marked "Shares the GPU with video work here". While a render decodes on the 3080, or scenes are being found, it takes one fewer request at a time (down to none); the Mac Studio keeps going. Only where renders decode on the GPU: with `LUMIVERB_ANALYSIS_PROXY_DECODER=cpu`, or "auto" finding no Vulkan, or a Mac (whose media engine decodes), nothing is held. The built-in Whisper always counts as sharing it.
- **Now.** Settings → Processing shows what the scheduler is doing (it says every 5 seconds) and what's next, or that it isn't running.
- **Redo on change.** Changing a model in Settings → AI is the approval: it asks once, naming how many clips it makes again, and then everything made with the old one is redone everywhere, after anything missing (the lowest tier). Settings → Processing shows each producer's stale clips being redone, with Stop and Resume (admins). A transcript a person wrote is never redone. New settings in Settings → Processing are the approval the same way. Scenes found again replace the clip's old ones, and their descriptions are made again with them. Proxies and previews show as stale but aren't redone yet.
- **How it saves.** Jobs save their results through the API's own routes on this machine, with the same checks and lineage as before; at start the scheduler makes itself an API key per account (labelled `scheduler`, and marked as its own: no other key may take that label) and revokes its previous one.
- **Analysis proxies** render up to three at once on `media`'s 20 cores. The RTX 3080 decodes the originals through Vulkan, about four times faster than the CPU (a 24-minute 4K HEVC clip in about 6 minutes instead of 22); One render at a time decodes there (`LUMIVERB_GPU_DECODES` in `/etc/lumiverb/env`), and only while the card has 1.5 GB free: one decode already keeps the 3080's video decoder near full, and three at once (about 300 MB of video memory each) ran the vision model out of memory beside it. The other renders decode on the CPU meanwhile. ffmpeg decodes on the CPU what the GPU can't (ProRes, 4:2:2), and a render whose GPU decoding fails runs again on the CPU. A render (and a scan's 10-second preview) is stopped only when ffmpeg makes no progress for 5 minutes, never for taking long: a starved CPU finishes it (`src/processing/video/ffmpeg_run.py`). `nvidia-smi dmon -s u` shows the decoder busy (the `dec` column). Until a video's is ready, it plays its 10-second preview. They are full-length copies at most 960 px on the long side, at most 30 fps, with every audio track ffmpeg can decode: each at most stereo, 48 kHz AAC at 48 kbps per channel. With several tracks, a stereo mix of them comes first ("Lumiverb mix"): it's what the web player plays and what transcription hears, so a lav on its own track counts. A track ffmpeg can't decode (iPhone spatial audio, for one) is left out rather than failing the clip. Scenes and scene vision read the proxies too, never the originals. They are not edit proxies.
- **Library health.** The libraries page shows a library as pending until its videos have analysis proxies.
- **Playback.** Signed in, the web plays each video in full from its analysis proxy, framed in green; until the proxy exists, the 10-second preview plays, framed in amber, and switches to the whole video when it's ready. Public pages play 10 seconds unless raised. Both are in Settings → Playback, or `lumiverb settings video-preview` / `public-preview` (`full` or seconds). The server enforces them; public transcripts stop where public playback does.

Useful:

```bash
journalctl -u lumiverb-scheduler -f
```

Ctrl-C there only stops watching the log; the scheduler keeps going. The web app shows the same journal, every Lumiverb unit's, at Admin › Logs: the API reads it as the `lumiverb` user, once you add that user to `systemd-journal` (`sudo usermod -aG systemd-journal lumiverb`, then restart lumiverb-api). Neither the install nor an update does it for you: the group reads the whole system journal (every service, sudo's command lines), not only Lumiverb's lines. With more than one account it shows only on the machine itself. To pause all processing:

```bash
sudo systemctl stop lumiverb-scheduler
```

and `sudo systemctl start lumiverb-scheduler` to carry on. A stop gives jobs in hand up to 25 seconds; once it begins, no more descriptions, OCR, CLIP or transcripts are saved (a face batch, a render or a probe in hand may still finish); restarts don't rescan from scratch, and what wasn't finished is simply due again. If the database restarts, the scheduler stops and systemd starts it again (it holds a lock there, so only one runs).

Or flip the pause switches, as Settings › Processing does, without an API key (in every account, or `--account NAME`): `all`, `scans` (`--library NAME` for one library's), `upkeep` or a producer. What's running finishes; the scheduler keeps running and picks the switch up.

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server pause all --env /etc/lumiverb/env
```

and `resume all` to carry on.

Plug-ins the server admin installs (`lumiverb-server plugins install FOLDER --on|--off`, ADR-023) run in `lumiverb-plugins`, each a process of its own: below the API and the scheduler in priority, given none of `/etc/lumiverb/env`'s settings, held to its own memory and disk, started again when it stops and left stopped, with why, when it keeps stopping (Settings › Plug-ins says which). It can't read the secrets of the processes around it (they aren't dumpable), but it isn't confined: it runs as the `lumiverb` user, so it can read whatever that user can (the data dir's previews and backups, every library it can reach) and write `DATA_DIR/plugins`, other plug-ins' folders there included, and reach the network whatever its manifest declares. Confining it is ADR-023's phase 4, deferred: install only plug-ins you trust. Their lines are in its journal, each named (`plugin hello | …`):

```bash
journalctl -u lumiverb-plugins -f
```

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb library report-changes /mnt/media-01/Media/New\ shoot
```

`report-changes` takes paths as this machine sees them; the root map turns them back into the Mac's.

## Updating

One command:

```bash
sudo bash /opt/lumiverb/scripts/update.sh
```

It pulls the install's branch, then runs `update-api.sh` (packages, migrations, units, restarts; it keeps processing's packages, and the first time replaces lumiverb-worker with lumiverb-scheduler) and `update-web.sh` (the web build and the nginx site: playback streams go straight through, unbuffered, and their links, good for hours, stay out of the access logs, nginx's and the API's). It ends with a summary: the commits that came in, each service's state and the API's health.

- **Another branch:** `--branch NAME` moves the install there and remembers it (in `/etc/lumiverb/env`, for the next update and for `deploy-api.sh`).
- **The log:** everything also goes to `/var/log/lumiverb/update-<date>-<time>-<pid>.log`, which belongs to whoever ran sudo, so it reads without sudo; `update-latest.log` is the newest. It ends with a line `Result: OK` or `Result: FAILED`. The last 20 are kept.
- **A failure** stops the update where it happened and says where; so does Ctrl-C or a dropped SSH session. Fix that and run it again.
- **One at a time:** a second update while one runs stops at once.
- **Never merges on the server:** a checkout with commits of its own stops at the pull. A `--branch` older than `update.sh` is refused before anything changes.
- **Nothing of the `lumiverb` user's steers it** (Root and the `lumiverb` user, above): a link in the data dir or a `DATA_DIR` that isn't one full path stops it, saying what to fix. The first update after this moves an install made before it: the API starts through `python -m src.server.api` (uvicorn, with its own process not dumpable), the scheduler's home is no longer writable to it, the upkeep timers pass the key on stdin, and root's mark on the checkout (`safe.directory` in `/etc/gitconfig`) is taken away. Run git there as `lumiverb` (`sudo -u lumiverb git -C /opt/lumiverb …`, as below), never as root.
- **Quickwit goes:** search is in PostgreSQL (ADR-019), so an update on an install that still has Quickwit stops and removes `lumiverb-quickwit`, its config (`/etc/lumiverb/quickwit.yaml`), its index (`quickwit` in the data dir: search was already rebuilt in each account's database) and `/usr/local/bin/quickwit`, and the API's unit stops waiting for it. Nothing else in the data dir is touched. The `QUICKWIT_*` lines in `/etc/lumiverb/env` stay (the file is yours): nothing reads them, and the update says once that they may be removed. Later updates, and installs that never had Quickwit, have nothing to do.

An install on a branch that has no `update.sh` gets the script from `main` itself, which moves the install there:

```bash
sudo -v && sudo -u lumiverb git -C /opt/lumiverb fetch -q origin && sudo -u lumiverb git -C /opt/lumiverb show origin/main:scripts/update.sh | sudo bash -s -- --branch main
```

The repo pins Python 3.12 (`.python-version`), the version the tests run on; this box's own Python is 3.14. The first update after the pin moves `/opt/lumiverb/.venv` to 3.12, which uv downloads for the `lumiverb` user along with a few GB of packages (torch, CUDA). It downloads everything into a side environment while Lumiverb keeps running, then stops the API and worker for the swap (seconds from the warm cache) and the rest of the update (migrations, units), typically under a minute, and starts them again. Every update ends by clearing uv's cache, which frees the old Python's packages. If anything fails while they're stopped, it says so: fix the error and run the update again, and it carries on.

Caches used to live in the `lumiverb` user's home, on the root disk. They are now all under `/mnt/ssd2/lumiverb/cache`, so after updating, the old ones can be deleted:

```bash
sudo rm -rf /var/lib/lumiverb/.cache/lumiverb
```

## Backups

The API backs up every database each night (the 5-minute upkeep timer starts it once it's past 03:00 and none was made since), into `DATA_DIR/backups` (here `/mnt/ssd2/lumiverb/backups`): pg_dump archives, one folder a backup (`src/server/backups.py`). Settings › Backups lists them and makes one now; the Admin page's Backups row turns red with none for 2 days. The API's unit may write only under `DATA_DIR` (`ProtectSystem=strict`), so a folder elsewhere is refused until the unit allows it. It uses the `pg_dump` on the PATH (PostgreSQL 18's, installed with the server), or `PG_BIN`'s.

To put one back (Settings › Backups › Restore… gives these with the backup's folder):

```bash
sudo systemctl stop lumiverb-scheduler lumiverb-api
```

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server restore --env /etc/lumiverb/env BACKUP_FOLDER
```

```bash
sudo systemctl start lumiverb-api lumiverb-scheduler
```

`lumiverb-server backup --env /etc/lumiverb/env` makes one now (`--before-upgrade`: an upgrade's, of which the 3 newest are kept however old). An update (`update.sh`) makes one itself before migrating, when the new version has anything to migrate (`backup --if-migrating`, as the service user, into the backups folder or else `DATA_DIR/backups`); one that can't be made is a warning in the update's log, and the update goes on. Restore refuses while a client is connected to the databases (the API and the scheduler name themselves: lumiverb-api, lumiverb-scheduler; PostgreSQL's own workers, such as autovacuum, don't count, and one closing gets 3 seconds to go), backs up what's there first (so it can be undone), restores each database beside the live one and swaps it in only once whole, and migrates to this version. Search is in the restored databases: there's nothing to remake.

## Where previews live

Previews (every stored artifact) are in `DATA_DIR` (here `/mnt/ssd2/lumiverb`). Settings › Storage can move them to another folder, but the API's unit may write only under `DATA_DIR` (`ProtectSystem=strict`), so another folder there is refused ("Lumiverb can't write to …", or for one not made yet "Lumiverb can't make a folder in …") until the unit allows it: add the folder to `ReadWritePaths` in `lumiverb-api.service` (deploy-api.sh writes it), then `sudo systemctl daemon-reload` and restart the API. A daemon-reload can take the GPU from running containers (prod's Ollama, see AI machines): check `docker exec ollama nvidia-smi` after it (`ollama ps` can still say GPU), and if the GPU is gone restart the container (`docker restart ollama`; `update.sh` does this itself after its own reloads), or recreate it if that doesn't bring the GPU back (`docker compose -f ~/ollama/docker-compose.yaml up -d --force-recreate ollama`), then check again. A folder under `DATA_DIR` works as it is. A folder on a mount `/etc/fstab` lists is refused while that drive isn't mounted ("There's no drive at /mnt/ssd2. Is it mounted?"): its empty mount point is on the system disk.

## Moving the dev database to Postgres 18

Tests run on pgvector:pg18, and `docker-compose.yml` now starts it on a new volume (`postgres18_data`). An existing pg16 volume is left alone. To bring its data over, dump it with the old image running:

```bash
docker exec lumiverb-postgres pg_dumpall -U app > /tmp/lumiverb-pg16.sql
```

Then start the new one and load the dump:

```bash
docker compose up -d postgres
```

```bash
docker exec -i lumiverb-postgres psql -U app -d postgres -q < /tmp/lumiverb-pg16.sql
```

"Already exists" errors for the role, the `control_plane` database and the `vector` extension are expected.

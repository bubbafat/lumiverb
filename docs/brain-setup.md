# The brain: setup and operation

The brain is the Linux box `media` (LAN 192.168.86.166, Tailscale 100.94.35.123). It runs Lumiverb, reads the DAS, and does all processing: one scheduler ranks every job and runs it here (ADR-016 phases 2 and 4). The Mac Studio keeps the DAS and editing; it reports file changes and browses.

```
Mac Studio ── DAS (media-01, media-02)
    │  SMB over 10GbE (10.10.10.1)          reports changes: POST /v1/changes
    ▼
media: /mnt/media-01, /mnt/media-02  ──►  lumiverb-scheduler ──►  API :8100  ◄── nginx :80 ◄── web, Mac, iOS
                                           (every job, ranked;   Postgres 18 :5434
                                            AI on Settings → AI) Quickwit :7290
```

## Ports on this box

Settled so prod and dev run side by side. Don't use other ports without updating this table.

| What | Dev (unchanged) | Brain (prod) |
|---|---|---|
| Postgres | 5433 (Docker, pgvector:pg18) | **5434** (Ubuntu postgresql-18 + pgvector) |
| Quickwit | 7280 (Docker) | **7290** REST, 7291 gRPC |
| API | 8000 (uvicorn --reload) | **8100**, 127.0.0.1 only |
| Web | 5173 (Vite) | **80** (nginx, any address, HTTP) |

Already taken by other things: 5432 (DaVinci Resolve's Postgres), 8080 (ResourceSpace), 3001 (Resolve pgAdmin), 9000 (thelounge). This box gets no firewall from the scripts (`--no-firewall`): ufw would cut those off.

## Setup (your steps: sudo and the Mac)

### 1. Where the DAS is on the Mac

Libraries keep the root the Mac sees, because exports point Resolve, Premiere and Final Cut there. The brain maps it to its own mount:

| On the Mac | On the brain |
|---|---|
| `/Volumes/media-01` | `/mnt/media-01` |

The first library is `/Volumes/media-01/Media`: about 14,400 photos and videos, 4,400 of them videos. Paths match case-sensitively, so use the folder names as the Mac shows them (`Media`, not `media`). To add `media-02` later, rerun the install with another `--root-map`, or run `lumiverb config map-root` as the `lumiverb` user.

### 2. Install the API and the scheduler

From this checkout (`~/src/lumiverb`), on `feat/brain` until the stack is merged:

```bash
sudo bash scripts/deploy-api.sh --app-host http://192.168.86.166 --pg-port 5434 --api-port 8100 --quickwit-port 7290 --no-firewall --data-dir /mnt/ssd2/lumiverb --worker --root-map /Volumes/media-01=/mnt/media-01 --branch feat/brain
```

Add `--dry-run` to see the settings it will use without changing anything. Once installed, it needs sudo too: the remembered settings are in `/etc/lumiverb/env`, which only root can read.

- **Data dir** `/mnt/ssd2/lumiverb` (proposed): 3.4 TB free. Previews, stills and analysis proxies live there. Analysis proxies take about 0.4 GB per hour of footage, plus about 20 MB per hour for each extra stereo audio track. The scheduler's caches go there too (`cache/`), not on the root disk, and its temp files, such as the audio Whisper reads (`worker-tmp/`), not in RAM.
- **Postgres 18** comes from Ubuntu's own packages, and the cluster is created on 5434. Resolve's database on 5432 is never touched.
- **The scheduler** (`--worker` installs it) runs as the `lumiverb` user. Everything outside the data dir and its home is read-only to it, the DAS mounts included, whatever the mount options say.
- **Reruns are safe.** They keep the ports, the data dir, the branch, the Postgres version, `--no-firewall` (until a run with `--firewall`), the tenant, the keys and any settings added to `/etc/lumiverb/env` by hand, so `sudo bash scripts/deploy-api.sh` alone is enough. `deploy-web.sh` follows the same branch and firewall setting.

### 3. Install the web UI

```bash
sudo bash scripts/deploy-web.sh --domain _ --api-upstream http://127.0.0.1:8100 --no-firewall --branch feat/brain
```

Lumiverb is then at http://192.168.86.166 and http://100.94.35.123.

### 4. Create your user

It asks for a password (12 characters or more).

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb user create --email you@example.com --role admin
```

### 5. Create libraries with the Mac's paths

One per top-level area you want searchable:

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb library create --name "Media" --path "/Volumes/media-01/Media"
```

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb library list
```

`library list` shows a **Here** column with the mapped path. Within a minute the scheduler starts the first full scan. That is the fresh ingest of the DAS: probe, poster and preview, then analysis proxies, transcripts and scenes.

### 6. AI machines

Descriptions, OCR, scene descriptions and transcripts run on the account's **AI machines** (Settings → AI): GPU boxes, each an OpenAI-compatible endpoint such as Ollama, with the jobs it does and how many requests it takes at once. The job's one model must be on every machine doing it; until a model is chosen and a machine offers it, those steps wait rather than fail. The worker reads all of this from the server and keeps none of its own (a `vision_*` key left in an older worker config is ignored).

The model is `qwen3-vl:8b-instruct`. Plain `qwen3-vl:8b` is the thinking variant: its reasoning used up the 500-token answer budget, so most descriptions came back empty or cut off (Oct 8). Every machine doing descriptions needs it pulled (`ollama pull qwen3-vl:8b-instruct`).

On Oct 8 there are two:

| Machine | URL | At once |
|---|---|---|
| Brain 3080 | `http://10.10.10.2:11434/v1` | 2 |
| Mac Studio (over the direct 10 Gb link) | `http://10.10.10.1:11434/v1` | 4 |

**The brain's Ollama** is Lumiverb's own: `~/ollama/docker-compose.yaml` (`docker compose -f ~/ollama/docker-compose.yaml up -d`), with the image digest and model folder (`/mnt/ssd1/ollama-models`) it had as ResourceSpace's `ollama` service. Its port is published on every address of the brain, so it's at a fixed address: 10.10.10.2 (the direct link to the Mac), localhost, and the LAN. Ollama has no password. Until Oct 8 Lumiverb borrowed ResourceSpace's Ollama at a Docker-internal address, which changed when that container restarted and stopped vision. ResourceSpace is retired: its containers are stopped with `restart=no`. If its compose file is ever started again, take the `ollama` service out of it first: it would take the same container name and GPU.

To add a machine, as an admin: **Settings → AI → Add machine**, a name, the URL, **Connect** (it lists the models the machine offers), tick **Descriptions & text**, how many at once, **Add**. "At once" is how many images it works on together; more needs more GPU memory. It only helps if that Ollama runs that many in parallel (`OLLAMA_NUM_PARALLEL`); otherwise the rest wait there. A request is about 2k tokens (a 1280 px image, the prompt, a 500-token answer), so any context of 8k or more is plenty.

Every minute the scheduler checks every machine (and each one again before any work once the model changes), and sends work only to those online and offering the model, spread by how many each takes. Descriptions, OCR and scene descriptions each keep as many requests going as the online machines take together; scenes from several videos go out together, so short clips don't leave a machine idle. One that stops answering (asleep, out of memory, the model gone) is skipped and its work goes to the others; it's checked again a minute later. Settings → AI shows each machine's state, offline ones in red, with a red dot on AI in the Settings menu. Only when no machine is left does vision work wait, and no clip is charged a failure.

**Transcripts** are a job too, with one model (`small` until changed in Settings → AI → Models). Every account has a **Built in** machine: the scheduler's own Whisper (faster-whisper, on the 3080 here), doing transcripts one clip at a time, with nothing to set up. It has no URL; it can be renamed, given more at once (each one loads the model on the GPU beside Ollama: `small` takes about 1 GB, `large-v3` about 4 GB) or turned off, not removed. The worker checks it before transcribing (faster-whisper installed, the model one it knows), and a model that won't load moves its clips to another machine.

To transcribe on another GPU as well, run an OpenAI-compatible Whisper server there and add it like any machine, ticking **Transcripts**. [speaches](https://speaches.ai) is the one that fits: it serves faster-whisper with `GET /v1/models` and `POST /v1/audio/transcriptions`, and lists the same models by their Hugging Face names (`Systran/faster-whisper-small` is `small`). Download the job's model into it first (speaches: `POST /v1/models/Systran/faster-whisper-small`), or Connect won't list it. On a Mac, speaches runs on the CPU only. LocalAI's whisper backend (whisper.cpp, on Metal) works too: it lists each model by the name its config gives it, so give the job's name only to a build of the same model (`ggml-small.bin` for `small`). A bare whisper.cpp server lists no models, so Lumiverb can't check it has the right one: not supported. Nor is OpenAI's own API (25 MB a request).

Whichever machine hears a clip, the scheduler finds its speech first (faster-whisper's VAD, skipping silences of 500 ms or more, the transcript producer's setting) and sends only the speech, then puts the times back onto the clip. So the model and silences a transcript records are the ones it was made with, on any machine. How a server decodes isn't tracked, like which GPU it ran on: current speaches also skips silences over 160 ms within what it's sent and decodes pieces of at most 30 s at temperature 0, so its cues and an occasional word differ from the built-in's (on the Spanish test clip it heard "eras" where the built-in heard "eres").

A machine that doesn't answer, breaks off or can't load the model is skipped and its clips go to the others. A clip a machine refuses (too big for a proxy in front of it, say) goes to the others first. One that a machine is still working on after ten minutes and four times its speech, or that makes Whisper die, is marked failing and tried again later, 5 minutes, then 10, doubling to a day.

### 7. The Mac app

Not pointed at the brain yet (your call, Oct 8): its Swift changes wait. Until then it keeps scanning and enriching its own server. The changes it needs are under "Mac app changes" below.

### 8. The DAS mounts

`/mnt/media-01` and `/mnt/media-02` stay read-write in `/etc/fstab` (your call, Oct 8): Lumiverb can't write there anyway, because the scheduler's sandbox makes everything outside its data folder and home read-only.

The fstab lines don't say `soft` or `hard`, so the mounts are `soft`, the CIFS default: when the Mac Studio sleeps, reads fail instead of hanging forever. Never add `hard`: a sleeping Mac would then hang the worker's scans and renders indefinitely.

Keep the DAS mounts in `/etc/fstab`, not in hand-written systemd mount units or autofs: the scanner reads fstab to tell an unmounted share from a share that has lost some files. For a share fstab doesn't list only the empty-folder check is left, so stray files in its unmounted mount point would look like the whole library, and everything else would be archived (your call, Oct 8).

## How it runs

- **One queue.** The scheduler ranks every job, in every library: first what you see (scans, which make thumbnails and previews, and probes), then analysis copies, then the AI and the rest that makes clips findable (CLIP, descriptions and tags, text in images, transcripts, faces, scenes, scene descriptions). Within a tier, oldest first (in the order clips were added). Each resource has its own slots: scans one at a time, probes 2, analysis copies as below, CLIP and faces taking turns on the GPU (25 clips a batch), scenes 1, and the AI machines as many at once as Settings → AI says. A free slot takes the best job that uses it, so AI keeps going while renders wait on the storage, and a long backlog of one kind never holds up another.
- **Changes.** The Mac reports paths it sees change: `POST /v1/changes`. Every 30 s the scheduler scans the one folder that covers a library's reported changes, then acknowledges them. A file modified in the last 30 seconds may still be copying, so it waits for the next look (one stamped more than 5 minutes in the future came from a camera clock running ahead, and doesn't wait).
- **Files that fail.** A file that fails to scan doesn't hold back the changes around it. It's tried again on its own after 5 minutes, then 10, 20 and so on, up to once a day. A folder that can't be listed (no permission, or the share timing out mid-scan) keeps its changes for the same retry, and that scan removes nothing. A clip whose processing fails is tried again after 5 minutes, doubling up to a day, and given up after 10 tries (about two days); Settings → Processing ("Show failures") and `lumiverb producers failures` list failing clips with their errors, and Try again (`lumiverb producers retry`) starts over. One that waits without failing (the GPU out of memory, the AI machines down, its file gone since the last look) isn't charged, and waits an hour before the scheduler takes it again, unless someone asks for it to be tried again.
- **Safety net.** Each library is scanned in full once a day, in case a report was missed. When each was last scanned in full, and which files and folders wait for a retry, are kept in `worker-state.json` beside the scheduler's lock (in `/mnt/ssd2/lumiverb/cache/lumiverb`; the names are the old worker's, so it carried them over), so a restart or deploy doesn't rescan everything; delete it to force full scans.
- **The Mac Studio asleep.** The scheduler doesn't scan its libraries. A folder `/etc/fstab` lists as a mount counts only while it's mounted, and an empty folder never counts, so an unmounted share is never scanned. Work from analysis proxies goes on; only probing and rendering wait, and they start as soon as the storage is back.
- **Missing files are archived.** On a healthy mount, a file the scan no longer finds is archived (marked missing) at once: its asset keeps ratings, projects, faces and transcripts. When the file comes back, at the same path or anywhere else in the library (matched by content), the asset is restored. Archiving is reversible, so nothing is held back, however many go at once. Archived clips (missing, or archived by a person) aren't deleted on their own: only by moving them to the trash, or with their library. The trash deletes what's been in it longer than the trash days (30 unless changed in Settings → Files) on the 5-minute upkeep timer.
- **Redo on change.** Changing a model in Settings → AI is the approval: it asks once, naming how many clips it makes again, and then everything made with the old one is redone everywhere, after anything missing (the lowest tier). Settings → Processing shows each producer's stale clips being redone, with Stop and Resume (admins). A transcript a person wrote is never redone. Scenes, proxies and previews show as stale but aren't redone yet.
- **How it saves.** Jobs save their results through the API's own routes on this machine, with the same checks and lineage as before; at start the scheduler makes itself an API key per account (labelled `scheduler`) and revokes its previous one.
- **Analysis proxies** render up to three at once on the brain's 20 cores. The RTX 3080 decodes the originals through Vulkan, about four times faster than the CPU (a 24-minute 4K HEVC clip in about 6 minutes instead of 22); One render at a time decodes there (`gpu_decodes` in the `lumiverb` user's CLI config), and only while the card has 1.5 GB free: one decode already keeps the 3080's video decoder near full, and three at once (about 300 MB of video memory each) ran the vision model out of memory beside it. The other renders decode on the CPU meanwhile. ffmpeg decodes on the CPU what the GPU can't (ProRes, 4:2:2), and a render whose GPU decoding fails runs again on the CPU. `nvidia-smi dmon -s u` shows the decoder busy (the `dec` column). Until a video's is ready, it plays its 10-second preview. They are full-length copies at most 960 px on the long side, at most 30 fps, with every audio track ffmpeg can decode: each at most stereo, 48 kHz AAC at 48 kbps per channel. With several tracks, a stereo mix of them comes first ("Lumiverb mix"): it's what the web player plays and what transcription hears, so a lav on its own track counts. A track ffmpeg can't decode (iPhone spatial audio, for one) is left out rather than failing the clip. Scenes and scene vision read the proxies too, never the originals. They are not edit proxies.
- **Library health.** The libraries page shows a library as pending until its videos have analysis proxies.
- **Playback.** Signed in, the web plays each video in full from its analysis proxy, framed in green; until the proxy exists, the 10-second preview plays, framed in amber, and switches to the whole video when it's ready. Public pages play 10 seconds unless raised. Both are in Settings → Playback, or `lumiverb settings video-preview` / `public-preview` (`full` or seconds). The server enforces them; public transcripts stop where public playback does.

Useful:

```bash
journalctl -u lumiverb-scheduler -f
```

Ctrl-C there only stops watching the log; the scheduler keeps going. To pause all processing:

```bash
sudo systemctl stop lumiverb-scheduler
```

and `sudo systemctl start lumiverb-scheduler` to carry on. A stop lets jobs in hand finish for up to 25 seconds, saving nothing more after it began; restarts don't rescan from scratch, and what wasn't finished is simply due again. If the database restarts, the scheduler stops and systemd starts it again (it holds a lock there, so only one runs).

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

The first time, the install is still on `feat/brain`, which has no `update.sh`, so the script comes from `main` itself and moves the install there:

```bash
sudo -v && sudo -u lumiverb git -C /opt/lumiverb fetch -q origin && sudo -u lumiverb git -C /opt/lumiverb show origin/main:scripts/update.sh | sudo bash -s -- --branch main
```

The repo pins Python 3.12 (`.python-version`), the version the tests run on; this box's own Python is 3.14. The first update after the pin moves `/opt/lumiverb/.venv` to 3.12, which uv downloads for the `lumiverb` user along with a few GB of packages (torch, CUDA). It downloads everything into a side environment while Lumiverb keeps running, then stops the API and worker for the swap (seconds from the warm cache) and the rest of the update (migrations, units), typically under a minute, and starts them again. Every update ends by clearing uv's cache, which frees the old Python's packages. If anything fails while they're stopped, it says so: fix the error and run the update again, and it carries on.

Caches used to live in the `lumiverb` user's home, on the root disk. They are now all under `/mnt/ssd2/lumiverb/cache`, so after updating, the old ones can be deleted:

```bash
sudo rm -rf /var/lib/lumiverb/.cache/lumiverb
```

## Mac app changes (Swift, not built yet)

1. **Report changes.** `LibraryWatcher` already gets FSEvents for each library root. After its debounce and the 30-second quarantine, send the changed paths, as the Mac sees them, to `POST /v1/changes` with body `{"paths": [...]}`.
   - Files or folders, up to 10,000 per request.
   - Retry on failure.
   - The response says how many matched a library (`accepted`) and how many didn't (`unmatched`).
2. **Stop scanning and enriching.** The brain does both now. Keep browsing.
   - Retire Apple Vision faces and OCR, CoreML ArcFace, FeaturePrint and whisper.cpp.
   - A Mac can still serve a model as an ordinary endpoint, such as Ollama, as long as the model isn't Mac-specific.
3. Until then, nothing breaks. If the Mac and the brain both transcribe a clip, the last write wins; phase 3 makes each artifact have one producer.

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

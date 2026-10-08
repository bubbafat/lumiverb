# The brain: setup and operation

The brain is the Linux box `media` (LAN 192.168.86.166, Tailscale 100.94.35.123). It runs Lumiverb, reads the DAS, and does all scanning and enrichment (ADR-016 phase 2). The Mac Studio keeps the DAS and editing; it reports file changes and browses.

```
Mac Studio ── DAS (media-01, media-02)
    │  SMB over 10GbE (10.10.10.1)          reports changes: POST /v1/changes
    ▼
media: /mnt/media-01, /mnt/media-02  ──►  lumiverb-worker  ──►  API :8100  ◄── nginx :80 ◄── web, Mac, iOS
                                           (scan, render,        Postgres 18 :5434
                                            enrich)              Quickwit :7290
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

### 2. Install the API and the worker

From this checkout (`~/src/lumiverb`), on `feat/brain` until the stack is merged:

```bash
sudo bash scripts/deploy-api.sh --app-host http://192.168.86.166 --pg-port 5434 --api-port 8100 --quickwit-port 7290 --no-firewall --data-dir /mnt/ssd2/lumiverb --worker --root-map /Volumes/media-01=/mnt/media-01 --branch feat/brain
```

Add `--dry-run` to see the settings it will use without changing anything. Once installed, it needs sudo too: the remembered settings are in `/etc/lumiverb/env`, which only root can read.

- **Data dir** `/mnt/ssd2/lumiverb` (proposed): 3.4 TB free. Previews, stills and analysis proxies live there. Analysis proxies take about 0.4 GB per hour of footage, plus about 20 MB per hour for each extra stereo audio track. The worker's caches go there too (`cache/`), not on the root disk, and its temp files, such as the audio Whisper reads (`worker-tmp/`), not in RAM.
- **Postgres 18** comes from Ubuntu's own packages, and the cluster is created on 5434. Resolve's database on 5432 is never touched.
- **The worker** runs as the `lumiverb` user. Everything outside the data dir and its home is read-only to it, the DAS mounts included, whatever the mount options say.
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

`library list` shows a **Here** column with the mapped path. Within a minute the worker starts the first full scan. That is the fresh ingest of the DAS: probe, poster and preview, then analysis proxies, transcripts and scenes.

### 6. Vision AI

Descriptions, OCR and scene descriptions need a vision endpoint; without one, the worker skips those steps rather than failing them. For now the brain uses `qwen3-vl:8b` in the Ollama container of the ResourceSpace stack (`~/dam-stack/resourcespace`), on the RTX 3080, reached at its internal address:

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb config set --vision-api-url http://172.18.0.6:11434/v1 --vision-model-id qwen3-vl:8b
```

That address changes if the container is recreated, and the endpoint goes with ResourceSpace: give Lumiverb its own Ollama before switching ResourceSpace off.

### 7. The Mac app

Not pointed at the brain yet (your call, Oct 8): its Swift changes wait. Until then it keeps scanning and enriching its own server. The changes it needs are under "Mac app changes" below.

### 8. The DAS mounts

`/mnt/media-01` and `/mnt/media-02` stay read-write in `/etc/fstab` (your call, Oct 8): Lumiverb can't write there anyway, because the worker's sandbox makes everything outside its data folder and home read-only.

The fstab lines don't say `soft` or `hard`, so the mounts are `soft`, the CIFS default: when the Mac Studio sleeps, reads fail instead of hanging forever. Never add `hard`: a sleeping Mac would then hang the worker's scans and renders indefinitely.

Keep the DAS mounts in `/etc/fstab`, not in hand-written systemd mount units or autofs: the scanner reads fstab to tell an unmounted share from a share that has lost some files. For a share fstab doesn't list only the empty-folder check is left, so stray files in its unmounted mount point would look like the whole library, and everything else would be archived (your call, Oct 8).

## How it runs

- **Changes.** The Mac reports paths it sees change: `POST /v1/changes`. Each cycle (every 60 s), the worker scans the one folder that covers a library's reported changes, then acknowledges them. A file modified in the last 30 seconds may still be copying, so it waits for the next cycle (one stamped more than 5 minutes in the future came from a camera clock running ahead, and doesn't wait).
- **Files that fail.** A file that fails to scan doesn't hold back the changes around it. It's tried again on its own after 5 minutes, then 10, 20 and so on, up to once a day. A folder that can't be listed (no permission, or the share timing out mid-scan) keeps its changes for the same retry, and that scan removes nothing.
- **Safety net.** Each library is scanned in full once a day, in case a report was missed. When each was last scanned in full, and which files and folders wait for a retry, are kept in `worker-state.json` beside the worker lock (in `/mnt/ssd2/lumiverb/cache/lumiverb`), even when the worker is stopped mid-cycle, so a restart or deploy doesn't rescan everything; delete it to force full scans.
- **The Mac Studio asleep.** The worker doesn't scan its libraries. A folder `/etc/fstab` lists as a mount counts only while it's mounted, and an empty folder never counts, so an unmounted share is never scanned. It keeps enriching from analysis proxies. Only probing and rendering wait, and they start as soon as the storage is back.
- **Missing files are archived.** On a healthy mount, a file the scan no longer finds is archived (marked missing) at once: its asset keeps ratings, projects, faces and transcripts. When the file comes back, at the same path or anywhere else in the library (matched by content), the asset is restored. Archiving is reversible, so nothing is held back, however many go at once. Emptying the trash never deletes archived clips; deleting a library asks whether to delete its archived clips for good or keep them.
- **Scanning first.** Each cycle scans every library before enriching any. Enrichment then gets 15 minutes and stops between items, so change reports never wait on a first ingest's days of renders and transcription; it goes on next cycle, least recently enriched library first. Probing and rendering stop as soon as the storage stops answering, rather than waiting out a timeout per file.
- **Pacing.** Enrichment runs again when a library's counts change, when its storage comes back, or hourly, so a clip that fails every time isn't retried every minute. Within a run, a clip enrichment tried in the last hour waits, so one that fails slowly (a 15-minute render, say) doesn't hold up the clips behind it. After a restart each is tried once more.
- **Analysis proxies** are full-length copies at most 960 px on the long side, at most 30 fps, with every audio track ffmpeg can decode: each at most stereo, 48 kHz AAC at 48 kbps per channel. With several tracks, a stereo mix of them comes first ("Lumiverb mix"): it's what the web player plays and what transcription hears, so a lav on its own track counts. A track ffmpeg can't decode (iPhone spatial audio, for one) is left out rather than failing the clip. Scenes and scene vision read the proxies too, never the originals. They are not edit proxies.
- **Library health.** The libraries page shows a library as pending until its videos have analysis proxies.
- **Playback.** Signed in, the web plays each video in full from its analysis proxy, framed in green; until the proxy exists, the 10-second preview plays, framed in amber, and switches to the whole video when it's ready. Public pages play 10 seconds unless raised. Both are in Settings → Playback, or `lumiverb settings video-preview` / `public-preview` (`full` or seconds). The server enforces them; public transcripts stop where public playback does.

Useful:

```bash
journalctl -u lumiverb-worker -f
```

Ctrl-C there only stops watching the log; the worker keeps going. To pause it:

```bash
sudo systemctl stop lumiverb-worker
```

and `sudo systemctl start lumiverb-worker` to carry on. Restarts don't rescan from scratch: files already in are skipped.

To run one cycle by hand, stop the service first; otherwise the run says a worker is already running. A manual run uses the same caches, lock and state as the service: the install sets `cache_home` in the `lumiverb` user's CLI config (`lumiverb config show`).

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb worker --once
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

It pulls the install's branch, then runs `update-api.sh` (packages, migrations, units, restarts; it keeps the worker's packages) and `update-web.sh` (the web build and the nginx site: playback streams go straight through, unbuffered, and their links, good for hours, stay out of the access logs, nginx's and the API's). It ends with a summary: the commits that came in, each service's state and the API's health.

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

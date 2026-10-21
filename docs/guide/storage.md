# Storage

Lumiverb leaves your originals where they are and keeps what it makes from them in folders of its own. This page says what it keeps, where, how to move the previews to a bigger drive, and what happens when a drive is unplugged.

## What Lumiverb keeps

| What | Where |
|---|---|
| Previews and thumbnails of every photo and video | The previews folder |
| A short preview of each video, and a full-length copy for analysis, which is also what the web app plays | The previews folder |
| Frames from video scenes, and face crops | The previews folder |
| The start of videos cut for capped playback (Settings › Playback) | The previews folder, at most 2 GB |
| The databases: people, libraries, clips, names, ratings, notes, projects, settings, and search | On a Mac, the data folder. On Linux, PostgreSQL's own folder |
| AI models for search, faces and transcripts | The data folder |
| Backups | The data folder, unless a server admin chose another (see [Backups and restore](backups.md)) |

The data folder is `$(brew --prefix)/var/lumiverb` on a Mac and `/var/lib/lumiverb` on Linux, unless the install chose another: it's `DATA_DIR` in the env file. Until someone moves them, the previews are in the data folder too.

The previews folder is the one that grows with your libraries, the copies of videos most of all. How big those copies are is set under Analysis proxies in Settings › Processing (see [Processing](processing.md)). Everything in it can be made again from the originals, which is why backups leave it out.

## Where previews live

**Settings › Storage** shows, to a server admin:

- **Previews**: the folder, and how much room is left on its drive.
- **Libraries with a folder of their own**, if any.
- **Data**: the data folder and its room. The database, the search index and the AI models stay there; Ollama keeps its models in its own place.

Settings shows a dot beside Storage when something needs a look: a drive not plugged in, a folder Lumiverb can't write to, or a move that stopped.

## Moving the previews

Move them to a drive with room, such as off a Mac's system drive. A server admin, on the computer Lumiverb is installed on:

1. In **Settings › Storage**, under Previews, click **Move them…**.
2. Type a folder: an empty one on the drive you want, or one to make, such as `/Volumes/Media/Lumiverb previews`. Click **Next**.
3. If it isn't there yet, Lumiverb asks; click **Make it**.
4. It says how much moves and how much room there is. Click **Move them** (or **Use it**, when there's nothing to move yet).

New previews go to the new folder at once, and the rest move in the background. The page shows how many files and how much are across. Lumiverb keeps working meanwhile: previews not moved yet show from where they are.

If the move stops (the new drive full or unplugged, say), the page says why. It's tried again in 15 minutes, or click **Try again**. **Back to the data folder…** moves them back.

From any other computer, the page says "Move them on the computer Lumiverb is installed on."

## A library's own previews folder

One library can keep its previews in a folder of its own, such as a big video library's on a drive of its own. An account admin, on the computer Lumiverb is installed on, opens the library's **Library settings › Previews** and uses **Move them…** the same way. **Back to the server's folder…** moves them back.

If that folder is on the same drive as the library's originals, the page warns you: with that drive unplugged, the library can't be browsed at all.

## When a drive is unplugged

When the drive with the previews isn't there, Settings › Storage says "This folder isn't there. Is its drive plugged in? Until it is, previews don't show and none are made." For a library's own folder, only that library is affected. Plug the drive back in and everything carries on.

Lumiverb never writes into a folder that stands in for a missing drive (such as an empty `/Volumes` folder on a Mac, or an unmounted mount point on Linux). Choosing a folder on a drive that isn't there is refused: "There's no drive at … Is it mounted?" A chosen previews folder holds a small file called `.lumiverb-previews`, which is how Lumiverb knows its drive is there. Leave it in place.

Lumiverb also tries writing to each chosen folder every few minutes. If it can't, Settings says "Lumiverb can't write to …" with the reason, and no previews are made there until it can.

## On Linux: folders outside the data folder

On Linux, the API's service may write only inside the data folder. A previews or backups folder anywhere else is refused ("Lumiverb can't write to …", or "Lumiverb can't make a folder in …") until the service is allowed to. Open an override for the service:

```bash
sudo systemctl edit lumiverb-api
```

Add these lines, with your folder:

```text
[Service]
ReadWritePaths=/mnt/big/lumiverb-previews
```

Save, then restart the API:

```bash
sudo systemctl restart lumiverb-api
```

Then choose the folder in Settings.

## Caches and their caps

A few things are kept only to save time, each with a cap in gigabytes, set in the env file:

| Setting | What | Default |
|---|---|---|
| `LUMIVERB_PLAYBACK_CACHE_GB` | Cut videos for capped playback, per account | 2 |
| `LUMIVERB_ANALYSIS_CACHE_GB` | Copies of videos for analysis the scheduler keeps, when it doesn't run on the same disk as the API | 50 |
| `LUMIVERB_PREVIEW_CACHE_GB` | Previews the scheduler keeps, the same way | 2 |

Past its cap, the least recently used go first, and are made again when next needed. After editing the env file, restart Lumiverb. On a Mac:

```bash
brew services restart lumiverb
```

On Linux:

```bash
sudo systemctl restart lumiverb-api lumiverb-scheduler
```

## What frees space

- Emptying the trash, by hand or after the trash days, deletes each clip's previews, scene frames and face crops with it.
- A daily clean-up removes files Lumiverb no longer needs.
- Once a day, Whisper and search models no account uses any more are removed.

Your originals are never touched by any of these.

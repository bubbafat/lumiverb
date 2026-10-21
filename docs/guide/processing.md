# Processing

Everything Lumiverb knows about a photo or video it works out on the server, in the background, after a scan finds the file. This page says what it makes and in what order, how to pause it or change how things are made, and how to see why something waits.

## What Lumiverb makes

Settings › Processing lists each thing Lumiverb makes, in this order:

| On the page | For | What it is |
|---|---|---|
| Video facts (probe) | Videos | Codecs, size, frame rate, timecode, audio |
| Capture time and GPS accuracy | All | When it was taken |
| Proxies and thumbnails | All | The pictures you browse |
| Video previews | Videos | The first seconds, as a short video |
| Analysis proxies | Videos | A smaller full-length copy that the browser plays and that scenes, faces and transcripts are made from |
| Scene descriptions (with Scenes) | Videos | The video cut into scenes, each described |
| Video descriptions | Videos | The whole video described |
| Descriptions and tags | Photos | A photo described, with tags |
| Text in images (OCR) | Photos | Signs, screens, documents |
| Visual search (CLIP) | Photos | What search by content and Find similar compare |
| Faces (with Faces in videos) | All | The faces found, for People |
| Transcripts | Videos | What's said, with times |
| Location guesses | All | Where a clip probably was, in libraries where Infer location is on |

A plug-in that's on adds its own row, such as Place names ([Plug-ins](plugins.md)).

Scans make the previews. Video facts, capture times and analysis proxies need the original files, so they wait while a library's drive is away; the rest is made from what Lumiverb keeps. Descriptions, text, scene descriptions and video descriptions need an AI machine doing Descriptions & text; without one they wait, and nothing fails ([AI machines](ai-machines.md)). Visual search, faces and transcripts run on the server itself, or on a machine you add ([A second machine for search and faces](ai-worker.md)).

## In what order

Oldest clips first within each step:

1. What lets you see a clip: scans (thumbnails and previews) and video facts.
2. Analysis proxies and capture times.
3. What makes clips findable: visual search, descriptions, text, faces, scenes, transcripts.
4. Remakes: clips made with an older model, settings or version.

Different kinds of work run side by side. Each library is scanned in full every 15 minutes; Library settings › Scans › **Scan now** asks for a scan within a minute.

## Reading the page

Anyone signed in can open it. The box at the top says when everything will be made ("Caught up in about 2 h"), what runs **Now** and what's **Next**. "The scheduler isn't running" means nothing is being made: see [Troubleshooting](troubleshooting.md).

**Counts for** picks the clips counted: all libraries, one library or one project. Each row counts what's current, missing, made another way (an older model, settings or version) and failing, and about how long is left.

## Pausing

Admins flip the switches; everyone sees them. What's running finishes; nothing more of it starts.

- **All processing**, at the top: from Running or Partly paused it pauses everything, every library's scans too; from Paused it resumes everything.
- **Scans**: each library has its own switch, with **Pause every library** and **Resume every library**. Paused scans find no new or changed files and make no thumbnails or video previews; everything else goes on. A library's admin can also use Library settings › Scans › **Pause scans**.
- **Upkeep**: emptying the trash, cleaning up files, spreading face names.
- **Each kind of work** has a switch on its row.

Pausing never stops the nightly backup.

On the server itself, the server admin can do the same without signing in. On a Mac:

```bash
lumiverb-server pause all --env "$(brew --prefix)/etc/lumiverb/env"
```

On Linux:

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server pause all --env /etc/lumiverb/env
```

`resume` undoes it. In place of `all`: `scans` (`--library NAME` for one), `upkeep`, or one kind of work by what it makes, such as `image.description`, `image.text`, `image.faces`, `video.transcript` or `video.analysis_proxy` (a plug-in's work by its id). A name it doesn't know lists the ones it does. It acts in every account unless you add `--account NAME`.

## Levels

Faces have a level for photos and one for videos (Off, Low, Normal, High); scenes have a Detail (Low, Normal, High). An admin's pick is saved at once. Raising one finds nothing again by itself: the row counts what was made lower ("120 photos below this"), and **Redo** looks at them again. Off finds nothing new; what was found stays.

## Changing settings

**Settings · version N** under a row opens its settings. Admins change those that can be changed (the others say why not) and **Save**; **Back to defaults** puts them back. Among them: the quality (Good, Better, Best) of analysis proxies, previews and video previews, and how faces are grouped on People.

When a change would change what's already made, saving asks:

- **New files only**: what was made stays. The row keeps a **Remake** button for later.
- **Also remake them**: made again after everything else. The row says "Remaking 1,200 clips, after everything else", with **Stop**.

A new model in Settings › AI asks the same.

## After an update

When a release makes something differently (a new version of it, or new default settings), what was made before isn't remade until an admin says so. The row says, for example, "A new version of Faces is ready: 8,000 photos made before it are made again once an admin resumes it." **Remake** starts it. An update alone never sends your library back through an AI machine you pay for.

## Why work waits

Under each row a line says why some of its work waits, amber when someone has to act:

| The row says | What to do |
|---|---|
| "… wait until it's resumed." | Resume it. |
| "… wait for the scheduler, which isn't running." | See [Troubleshooting](troubleshooting.md). |
| "… Its AI machines can't be used. Settings → AI shows why." | Settings › AI says what's wrong. |
| "… wait for its plug-in …" | Settings › Plug-ins says why. |
| "… wait for Scenes first." | Look at that row. |
| "… in Photos wait for its drive …" | Plug the drive in, or mount the share. |
| "… wait for Studio Mac, which reads it …" | See [Libraries on another computer](devices.md). |
| "… failed too many times and aren't tried again until someone asks." | **Show failures**, then **Try again**. |
| "12 files in Photos can't be read …" | See [Troubleshooting](troubleshooting.md). |

## Failures

A clip whose work fails is tried again after 5 minutes, then waiting twice as long each time, up to a day. After 10 tries it's given up. **Show failures** lists each clip with its error, its tries and the next one. Editors and admins click **Try again**, or **Try all again**, which starts the waits over. Work waiting for something else (an AI machine down, a drive away) never counts as failing.

From the [command line](command-line.md):

```bash
lumiverb producers failures
```

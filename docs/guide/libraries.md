# Libraries

A library is a folder of photos and videos that Lumiverb reads where it is. This page covers adding one, its settings, and deleting one.

## What Lumiverb does to your files

Lumiverb only reads your originals. It never writes to them, moves, renames, uploads or deletes them. What it makes (previews, descriptions, faces) goes in its own folders and databases. Deleting a clip or a library in Lumiverb removes it from Lumiverb, never from the disk.

It reads JPEG, HEIC and HEIF (an iPhone's photos), PNG, WebP, BMP, TIFF, DNG, camera RAW (Canon CR2, CR3 and CRW, Nikon NEF and NRW, Sony ARW, SR2 and SRF, Fujifilm RAF, Olympus ORF, Panasonic RW2 and RAW, Leica RWL) and MP4, MOV and MKV video. Other files are left alone, and so is a picture over a gigapixel (1,000 megapixels): its clip says it's too large to make a preview of.

An iPhone's Live Photo is two files, the photo and a .mov of the few seconds around it, saved with the same name. Lumiverb shows them as one clip, the photo, with **LIVE** on it in the photo's view: click it to play those seconds. It tells them apart by the identifier Apple writes in both, never by the name alone, so a short video that happens to share a photo's name stays a video of its own. Deleting the photo for good deletes its moving part with it.

## Adding a library

Only an admin adds libraries.

1. Open the Libraries page: **Manage libraries** in the sidebar, or **Libraries** in the bar at the bottom on a phone.
2. Click **Add library**.
3. Fill in **Name** and **Root path**: the folder's full path, such as `/Volumes/Photos/2024`.
4. Tick **Infer location** to place photos without a location from others taken around the same time (see [Location guesses](#location-guesses)).
5. Click **Create library**.

If another computer is registered in Settings › Devices, the form also asks **Read by**: this server, or that computer. See [Libraries on another computer](devices.md).

Within a minute or so Lumiverb starts its first scan, then works through everything it makes from each file. [Processing](processing.md) shows how far it has got.

### On a Mac

A Mac's Lumiverb can read any folder the Mac can, so a library's folder is chosen only on that Mac itself. From another device you get "Choose a library's folder on the computer Lumiverb is installed on."

### On Linux

The scheduler on a Linux server reads only folders under the prefixes in `LUMIVERB_ROOT_MAP` in `/etc/lumiverb/env` (the install's `--root-map`). A library elsewhere is refused, and the scheduler's log says so. The map is JSON, from the path a library stores to where that is on the server:

```text
LUMIVERB_ROOT_MAP='{"/Volumes/media-01": "/mnt/media-01"}'
```

After changing it, restart the scheduler:

```bash
sudo systemctl restart lumiverb-scheduler
```

## Library settings

On the Libraries page, click a library's **Settings**. What you see depends on your role on that library (see [Users and roles](users-and-roles.md)).

| Section | What it does | Who changes it |
|---|---|---|
| Path Filters | Which files in the folder are taken in | The library's editors and admins |
| Scans | Scan now, and pausing its scans | Scan now: editors; pausing: its admins |
| Who reads it | This server or a device, and the folder | Account admins (its admins see them) |
| Location guesses | Infer location on or off | Its admins |
| People | Who sees it, their role there, and whether it's private: see [Roles on one library](users-and-roles.md#roles-on-one-library) | Its admins |
| Previews | Where its previews are kept: see [A library's own previews folder](storage.md#a-librarys-own-previews-folder) | Account admins |

### Path filters

Patterns are matched against each file's path inside the library, ignoring case. `*` matches within one folder or file name; `**` matches any number of folders.

- **Include patterns**: only paths that match are taken in. Empty means everything.
- **Exclude patterns**: paths that match are left out, after the includes.

Examples: `Photos/**` takes in only the Photos folder; `**/Proxy/**` leaves out every folder called Proxy; `**/*.mov` leaves out every MOV file (`**.mov` would match only those directly in the library's folder).

An exclude pattern that matches clips already in Lumiverb asks first: they move to the trash, and are deleted for good after the trash days unless restored. If some are in projects, it says which and asks again.

A shortcut: in the sidebar's folder list, click the clip count beside a folder, then **Exclude folder** (or **Archive folder**).

Filters every new library starts with are set on the command line by an editor or admin, for example:

```bash
lumiverb filter add "**/Proxy/**" --tenant-default --exclude
```

### Scans

Lumiverb scans every library in full every 15 minutes, finding new, changed, moved and deleted files. Nothing needs to tell it a file changed.

- **Scan now** asks for a full scan; it starts within a minute.
- **Pause scans** stops new and changed files being found in this library. Everything else goes on. **Resume scans** starts them again.

The section also says when the library's reader last checked in, and when the last full scan was.

What a scan does with changes:

- A file that's gone is archived as missing (Archive › File missing). It comes back by itself when the file does.
- A moved or renamed file keeps its ratings, notes and projects, unless Settings › Files says every path is its own file.
- A clip you put in the trash or archived stays out while its file is unchanged.
- Files Lumiverb isn't allowed to read are listed in Settings › Processing under Scans. Make them readable and the next scan takes them in.
- A folder that's suddenly empty is taken for an unplugged drive, not for every file deleted: the library's work waits for it. The Admin page lists whether each library can be reached.

A big library on a slow network share can be scanned less often: set `LUMIVERB_FULL_SCAN_EVERY_MIN` (minutes, for every library) in the env file, then restart Lumiverb. On a Mac:

```bash
brew services restart lumiverb
```

On Linux:

```bash
sudo systemctl restart lumiverb-scheduler
```

### Who reads it

Each library is read by this server or by one registered device, never both. The reader scans it and makes what needs the files themselves, such as previews; the server does the rest. Choose **Read by**, give **Its folder** as that reader sees it, and click **Save**. This is also where a library's folder is changed. Both are an account admin's to choose, as adding a library is: a library's own admin sees who reads it and its folder, and "An account admin chooses its folder and who reads it." See [Libraries on another computer](devices.md).

### Location guesses

With **Infer location** on, photos and videos without a location are placed from ones in this library taken around the same time that have one, such as a phone's. A guess never replaces a location from the file or one a person set, and shows as a guess. Turning it off asks first, then removes the guesses. See [Places](places.md).

## A public link

An admin can make a library public: on the Libraries page, click **Not public** and confirm. Anyone with the link can then view its photos; **Copy link** copies it. Click **Public** to stop. See [Ratings, projects and sharing](ratings-projects-sharing.md).

## Deleting a library

1. On the Libraries page, click the library's **Delete**.
2. Read what it says and click **Confirm**.

The library moves to the trash with everything in it, its archived clips too. Editors and admins can do this. If any of its clips are in projects, it says which and asks again.

A library in the trash can be brought back with **Restore** for 30 days, or as long as an admin set in Settings › Files › Trash. It comes back not public. An admin can delete it at once with **Delete for good**, or every library in the trash with **Empty trash**. That can't be undone, and removes what Lumiverb made for it. The files in its folder are never touched.

## From the command line

An admin's key can add a library:

```bash
lumiverb library create --name "Photos" --path "/Volumes/Photos/2024"
```

More in [The command line](command-line.md).

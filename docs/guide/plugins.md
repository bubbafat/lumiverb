# Plug-ins

Plug-ins add to what Lumiverb knows about your photos and videos, such as the town and landmark where each was taken. The server admin installs them on the server; each account's admin turns them on or off in Settings › Plug-ins.

## What a plug-in is

A plug-in is a program of its own, in any language, that Lumiverb runs outside itself. It comes as a folder with a `lumiverb-plugin.json` that says what it reads, what it downloads, whether it uses the internet, and its settings. Lumiverb shows you all of that before you install it.

Each plug-in runs in a process of its own, below Lumiverb's priority, given none of Lumiverb's settings or keys, and held to its own memory and disk. If it stops, it's started again; one that keeps stopping is left stopped, and Settings › Plug-ins says why.

A plug-in isn't confined, though: it runs as the user Lumiverb runs as, so it can read the files Lumiverb can (on a Mac, everything in the account of whoever installed Lumiverb) and reach the internet whatever it says it does. On a Linux server it can't read the secrets the rest of Lumiverb holds. Install only plug-ins you trust.

What a plug-in can do:

- **Work on each photo and video.** It's handed only what it asks for (where a clip was taken, when, its size and camera, a photo's preview) and gives back short facts. They show in the clip's details in the Lightbox, under the plug-in's name, and search finds a clip by the ones the plug-in marks searchable.
- **Find places by name** for Set location.

## Turn one on

Account admins, in **Settings › Plug-ins**. Each plug-in installed on the server is listed with what it does, its version and who made it, whether it's running, and **What it reads and reaches**: what it reads of your clips, what it downloads and from where, and whether it uses the internet.

Tick its switch to turn it on. If it asks you to accept something, such as a license, the whole text opens: **OK** accepts it and turns the plug-in on, **Cancel** leaves it off. The row then says who accepted it and when. If a later version asks for its new text to be accepted, the plug-in turns off and says so, until an admin turns it on and accepts it.

Soon after it's turned on, a plug-in that works on clips starts on every one in the account. Its work has its own row in Settings › Processing, with its counts and why anything waits ([Processing](processing.md)).

Below the switch are its settings: **Its settings**, for this account, and **For the whole server**, for every account. A setting marked "Changing it asks what becomes of what it made" asks, when saved, whether to keep what was made (**New ones only**) or make it again (**Also remake them**).

## Place names (GeoNames)

Place names is one of Lumiverb's own plug-ins. It comes installed, off until an account's admin turns it on and accepts GeoNames' license (Creative Commons Attribution 4.0).

Turned on, it downloads about 600 MB of lists from GeoNames once for the server, and builds its own list of the world's towns of 1,000 people or more and its landmarks (parks, museums, monuments, mountains and more). Settings › Plug-ins shows how far it has got. Nothing about your photos, videos or account is sent to GeoNames.

Then, for every photo and video with a location, it names the landmark it was taken at and the nearest town. They show in the clip's details, and search finds clips by either: a search for "Magic Kingdom" finds the photos taken there. In Set location, **Place** finds places by name. See [Places](places.md).

Its settings:

| Setting | What it does |
|---|---|
| Nearest town within | How far away, in km, a town can be and still name where a clip was (25 to begin with) |
| Download from | Advanced, for the whole server: a copy of GeoNames' download folder to use when the server can't reach GeoNames itself |

A Linux install needs `DATA_DIR` in its env file for any plug-in to run, Place names included.

## Install one

The server admin installs plug-ins on the server machine itself. The commands below are a Mac's; on a Linux server the command is `/opt/lumiverb/.venv/bin/lumiverb-server`, with `--env /etc/lumiverb/env`.

To see what's installed, Lumiverb's own included:

```bash
lumiverb-server plugins --env "$(brew --prefix)/etc/lumiverb/env"
```

To install one:

```bash
lumiverb-server plugins install FOLDER --off --env "$(brew --prefix)/etc/lumiverb/env"
```

`FOLDER` is the plug-in's folder, a `.zip` of it, or an `https` URL to a `.zip`. With a download, `--sha256 HASH` installs it only if it's the file you meant. It first shows what the plug-in reads and reaches, and asks before installing.

The first time, say whether it's on or off by default:

- `--off`: off until each account's admin turns it on.
- `--on`: on in every account that hasn't turned it off. If it asks for a license to be accepted, you accept it for those accounts.

Installing a newer version is the same command; it says what the new version reads or reaches that the old one didn't. To change the default later:

```bash
lumiverb-server plugins default ID off --env "$(brew --prefix)/etc/lumiverb/env"
```

## Try the example

Lumiverb comes with an example plug-in. It gives each photo and video a caption you choose, when it was taken and its shape, finds a few famous places in Set location, and has one setting of every kind. `lumiverb-server plugins` prints the command that installs it.

## Uninstall one

```bash
lumiverb-server plugins uninstall ID --env "$(brew --prefix)/etc/lumiverb/env"
```

It first says what the plug-in has in each account and what stops working without it, then asks what becomes of what it made:

- **Keep** (`--keep`): its facts are still shown and searched. Installing it again picks it all up.
- **Trash** (`--trash`): hidden, and deleted after 30 days unless it's installed again.
- **Delete** (`--delete`): what it made, its settings and its own data, deleted from every account now.

A plug-in uninstalled with its facts kept is listed in Settings › Plug-ins under **Uninstalled, facts kept**, where an account admin can **Delete its facts here** for that account. One of Lumiverb's own that you uninstall stays uninstalled through updates.

## When one isn't working

Settings › Plug-ins says what each is doing: **Running** (with its memory), **Starting…**, **Getting ready**, **Not running**, **Not working** with why, or **Stopped** with why. Admin › Logs shows its lines under Plug-ins. On Linux they're in its journal too:

```bash
journalctl -u lumiverb-plugins -f
```

Server admins also see a row for Lumiverb's own plug-ins on the Admin page, red when one couldn't be installed.

## Write one

[Writing a plug-in](../writing-a-plugin.md) is a plug-in's whole contract: the manifest, how it's run, and every request it answers.

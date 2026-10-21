# Libraries on another computer

A library's files can stay on the computer they're on, with Lumiverb running on another. That computer becomes a **device**: it reads the library for the server, and the original files never leave it.

## What a device does

A device scans the libraries it reads (new, changed, moved and gone files) and sends the server what can only be made from the original files:

- the previews and thumbnails, and each video's short preview;
- video facts and capture times;
- each video's analysis proxy, the smaller copy that plays in the browser.

The server does everything else from those: search, faces, descriptions, scenes and transcripts. A library is read by the server or by one device, never both.

## Set it up

You need an account admin for the web app steps, and Lumiverb installed on the device. On a Mac that's the same Homebrew install as a server, without starting its service:

```bash
brew trust --tap bubbafat/lumiverb
```

```bash
brew install bubbafat/lumiverb/lumiverb
```

**Register it.** In the web app, **Settings › Devices**, under **Register a device**, type a name (such as "Studio Mac") and click **Register**. Its key appears once: click **Copy**. The panel also shows the next two commands with your server's address filled in.

**Connect it.** On the device, tell Lumiverb where the server is, and paste the key when it asks (it isn't shown, and stays out of your shell's history):

```bash
lumiverb config set --api-url http://your-server:6464 --api-key -
```

Then start it reading:

```bash
lumiverb device run
```

**Give it the library.** In the web app, open the library's settings. Under **Who reads it**, set **Read by** to the device, and **Its folder as Studio Mac sees it** to the library's folder on that computer, such as `/Volumes/Photos`. Click **Save**. The device takes over at its next check-in, within a minute, and the server stops reading it.

A new library can be given to a device from the start: once a device is registered, **Add library** has a **Read by** choice too.

The device has to reach the server. When Lumiverb runs on a Mac, a server admin turns on **Let other devices on my network use Lumiverb** in Settings › Network first ([Network, phones and HTTPS](network.md)).

## Keep it running

`lumiverb device run` checks in every minute and keeps going until you stop it with Ctrl-C, which lets it finish the clip in hand. It runs in that terminal window, so leave the window open. Each pass, it scans what's due (each library in full every 15 minutes, any change it was told of, and **Scan now**), sends new and changed files with their previews, then makes the video facts, capture times and analysis proxies the server asks for, video renders last.

Its options:

| Option | What it does |
|---|---|
| `--once` | One pass, then stop |
| `--full-scan-every-min N` | Scan each library in full every N minutes instead of 15 |
| `--name NAME` | The name its check-ins use, instead of the computer's |

Paused scans stay paused on a device too: a library whose scans are paused isn't scanned.

## On battery or a metered network

Remakes (clips made again after an admin changes a quality setting, or after an update) wait while the device runs on battery, or on a network that's metered; new files go on as usual. A Mac's battery is noticed. A metered network is noticed only on Linux, where NetworkManager says so; a Mac's network is never treated as metered.

## See how it's doing

- **Settings › Devices** lists each device, the libraries it reads, and when its key was last used.
- **Settings › Processing**, under Scans, shows each library's reader: "Read by Studio Mac · scanning · 212 files (14 GB) to upload · checked in 1 minute ago". It turns amber when the device hasn't been heard from for a while, or can't reach the library's folder, and says when remakes wait for mains power.
- Library settings › Scans says when the device last checked in, and when it last scanned the library in full.
- Work that needs the originals says so on its row: "40 videos in Footage wait for Studio Mac, which reads it (last checked in 2 hours ago)."
- The Admin page's Libraries list shows whether each library's files can be reached, from its device's check-ins.

## The device's key

The key only scans the libraries that choose that device; it can do nothing else. In Settings › Devices, an admin can:

- **Rename** the device.
- **New key**: the old key stops at once. Set the new one on the device with `lumiverb config set --api-key -`.
- **End key**: the device does nothing until it gets a new one.
- **Remove**: the device and its key. Give its libraries another reader first.

## Updating

Update Lumiverb on the device when you update the server (on a Mac, `brew upgrade lumiverb`), then stop `lumiverb device run` and start it again. A device too old for the server is turned away until it's updated.

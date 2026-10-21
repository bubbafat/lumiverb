# Install on a Mac

On a Mac, Lumiverb installs with [Homebrew](https://brew.sh) and runs as one service that starts again at login.
It reads your libraries where they are: on the Mac itself or on drives connected to it.

## Install

Homebrew installs from a tap that isn't its own only once you trust it, so the first command trusts Lumiverb's tap
and installs it:

```bash
brew trust --tap bubbafat/lumiverb && brew install bubbafat/lumiverb/lumiverb
```

Then start it:

```bash
brew services start lumiverb
```

Lumiverb brings everything it needs: its own PostgreSQL database (not shared with any other you run), its own web
server, and the tools it makes previews with (ffmpeg, exiftool, libvips). Its first start takes a minute while it
makes its database.

## Create your account

Open [http://localhost:6464](http://localhost:6464) on that Mac. While nobody has an account, Lumiverb asks for
yours: an email address and a password of 12 characters or more. You look after this Lumiverb, and can invite
other people later.

This page works only on the Mac itself, so nobody else on your network can create the first account. You can
also do it in Terminal:

```bash
lumiverb-server setup --env "$(brew --prefix)/etc/lumiverb/env"
```

Then [First steps](first-steps.md) walks you through the setup that follows and your first library.

## Using it from other devices

At first, only that Mac can open Lumiverb. To use it from a phone or another computer, an admin turns on
Settings › Network › Let other devices on my network use Lumiverb, on the Mac. It shows the addresses to open,
such as `http://your-mac.local:6464`. See [Network, phones and HTTPS](network.md) for HTTPS and other ports.

Don't put a tunnel or proxy in front of Lumiverb (Tailscale Serve, Cloudflare Tunnel, ngrok) before your account
exists. Their requests arrive from the Mac itself, so whoever reaches them could create the first account, choose
library folders, or reset a password with Forgot password.

## Where things are

| What | Where |
|---|---|
| Settings | `$(brew --prefix)/etc/lumiverb/env` (keep a copy with your backups) |
| What Lumiverb makes, and its database | `$(brew --prefix)/var/lumiverb` (previews can move to another drive: [Storage](storage.md)) |
| The log | `$(brew --prefix)/var/log/lumiverb.log`, also shown at Admin › Logs |

Ports, all chosen to stay clear of other software you may run:

| Port | What |
|---|---|
| 6464 | The web app, over HTTP |
| 6465 | The web app over HTTPS, once it's turned on |
| 6364 | Lumiverb's API, for this Mac only |
| 6468 | Lumiverb's own PostgreSQL, for this Mac only |

## Stopping and starting

Lumiverb stops and starts as one service:

```bash
brew services stop lumiverb
```

```bash
brew services start lumiverb
```

Stopping it stops everything: its database, web server and the processing. Your libraries are untouched.

To update to a new version, see [Updating](updating.md).

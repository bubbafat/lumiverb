# Install on a Linux server

On Linux, install scripts set Lumiverb up as a set of services, with its own PostgreSQL and nginx in front. There
are two ways: on the internet with a domain name and a certificate, or on your home network without one.

## What it needs

- Ubuntu 24.04 or later, and a user who can use `sudo`. Ubuntu 22.04's libvips (8.12) is too old: Lumiverb reads
  photos with libvips 8.13 or later, the first that can turn off the loaders it doesn't trust, and the install
  stops there before it installs anything.
- 4 GB of memory or more. With less and no swap, the install says so: finding faces, search and transcripts can
  need more than that at once.
- Room for what Lumiverb makes: about 15 GB per 100,000 photos, and up to 1 GB per hour of video. They can go on
  another disk (`--data-dir`, below).
- Your photos and videos in folders this machine can read: on its own disks, or mounted from a NAS.

## The folders it may read

Lumiverb reads a library only in folders you list when you install it, each with `--root-map`. For a folder
that's at the same place for you and for Lumiverb, both sides are the same path:

```text
--root-map /srv/photos=/srv/photos
```

List each top folder your libraries will be in, with one `--root-map` for each. A library outside them is never
read (the log says it's refused). To add one later, run the install command again with the new `--root-map`
(it keeps everything else).

## On the internet, with a domain

With a domain name already pointing at the server (an A record to its address), one command installs everything:
PostgreSQL 18, nginx with a certificate from Let's Encrypt, the web app and the processing.

```bash
curl -fsSL https://raw.githubusercontent.com/bubbafat/lumiverb/main/scripts/deploy-vps.sh | sudo bash -s -- --domain photos.example.org --email you@example.org --worker --root-map /srv/photos=/srv/photos
```

Put your own domain and email in place of the examples. `--worker` makes this machine do the processing: leave
it in. Add `--data-dir /mnt/big/lumiverb` to keep what Lumiverb makes on another disk.

It checks that the domain points here before it asks for a certificate, and says which record to add if it
doesn't. The firewall lets in SSH, HTTP and HTTPS only.

## On your home network

Without a domain, Lumiverb answers on the server's address over HTTP. Get the scripts:

```bash
git clone https://github.com/bubbafat/lumiverb.git
```

Install the API and the processing, with the address people will open (here the server is `192.168.1.50`):

```bash
sudo bash lumiverb/scripts/deploy-api.sh --app-host http://192.168.1.50 --worker --root-map /srv/photos=/srv/photos
```

Then the web app, answering on any name or address:

```bash
sudo bash lumiverb/scripts/deploy-web.sh --domain _ --api-upstream http://127.0.0.1:8000
```

Together they turn on the firewall, letting in SSH, HTTP and HTTPS only. If the server runs other services that
people reach, add `--no-firewall` to both and keep your own firewall.

Either way, add `--dry-run` to see the settings it will use without changing anything. Running a script again is
safe: it keeps the settings it was given before.

## Create your account

The install has nobody who can sign in yet. Create the first account on the server; it asks for your email and a
password (12 characters or more):

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server setup --env /etc/lumiverb/env
```

You can do it in the web app instead, but its setup page works only from the server itself: from another
computer, reach it through an SSH tunnel. Do this before you put any tunnel or proxy in front of Lumiverb.

Then open Lumiverb in a browser, sign in, and follow [First steps](first-steps.md). When you add a library, its
folder is the path on the server, inside a folder you listed with `--root-map`.

## Where things are

| What | Where |
|---|---|
| Settings | `/etc/lumiverb/env`, readable by root only (keep a copy with your backups) |
| The program | `/opt/lumiverb` |
| What Lumiverb makes | `/var/lib/lumiverb`, or the folder given with `--data-dir` |
| The logs | The system journal, also shown at Admin › Logs once allowed ([Troubleshooting](troubleshooting.md)) |

Lumiverb runs as these services: `lumiverb-api`, `lumiverb-scheduler` (the processing), `lumiverb-plugins`, and
nginx. The API listens on this machine only, behind nginx. To restart the API:

```bash
sudo systemctl restart lumiverb-api
```

To update to a new version, see [Updating](updating.md).

## Email for password resets

Without email, a forgotten password is reset on the server with `lumiverb-server reset-password`. To have
Lumiverb send reset links by email, fill in the `SMTP_` lines in `/etc/lumiverb/env` and restart the API.

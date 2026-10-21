# Updating

An update is one command. Lumiverb backs up its databases before any update that changes them, and never remakes
what it already made without an admin's say.

## On a Mac

```bash
brew upgrade lumiverb
```

Lumiverb restarts itself into the new version: there's no need to restart the service. It's away for a few
seconds while it does. When the new version changes the database, it backs it up first.

## On a Linux server

```bash
sudo bash /opt/lumiverb/scripts/update.sh
```

It brings the code up to date from GitHub, then installs what's new, backs up the databases when the update
changes them, updates them, and restarts the services. One update runs at a time.

Everything it does is shown and also written to `/var/log/lumiverb/update-latest.log`, which ends with
`Result: OK` or `Result: FAILED`. It keeps the settings in `/etc/lumiverb/env` and any changes you made to nginx's
site.

## What an update never does on its own

Sometimes a new version makes something better: a newer face model, a better way to write descriptions. What was
already made the old way isn't made again on its own, since that can take days, or cost money on an AI provider
that charges for each picture. Settings › Processing says so for each kind of work:

```text
A new version of Descriptions and tags is ready: 12,480 made before it are made again once an admin resumes it.
```

New photos and videos get the new version at once. An admin resumes the rest when it suits them, or leaves the
old ones as they are. More in [Processing](processing.md).

## If an update goes wrong

- The log says what failed: Admin › Logs, or on a Mac `$(brew --prefix)/var/log/lumiverb.log`, or on Linux
  `/var/log/lumiverb/update-latest.log`. See [Troubleshooting](troubleshooting.md).
- The backup made before the update is kept, among the last three made before an upgrade, and can be restored:
  [Backups and restore](backups.md).

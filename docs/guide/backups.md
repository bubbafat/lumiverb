# Backups and restore

Lumiverb backs up its databases every night: everything only it holds. This page says what's in a backup, where backups go, and how to put one back.

## What's in a backup

Every database, as PostgreSQL's own tools save them:

- The install's: who signs in, the accounts, API keys, server admins.
- Each account's: libraries, clips, descriptions, tags, text read in photos, transcripts, people and faces, names, ratings, notes, places, projects, settings, and search.

Search is in the databases, so a restored backup searches as before with nothing to make again.

Not in a backup:

- **Your originals.** Lumiverb never writes to them, and doesn't back them up either. Back them up however you back up your files.
- **Previews**, thumbnails, copies of videos for analysis, face crops and AI models. They're made again from the originals and the databases.

A backup holds password hashes, so its files are readable only by the user Lumiverb runs as.

## When backups are made

- **Nightly**: after 03:00 each night. A Mac asleep then makes it when it wakes.
- **Before an upgrade**: a Mac's service backs up before an upgrade changes its databases. A Linux update does too, when the new version has database changes.
- **By hand**: **Back up now** in Settings › Backups, or the command line (below).
- **Before a restore**: a restore backs up what's there first, so it can be undone.

Kept: every backup from the last 7 days, one a week for 4 weeks, and the last 3 before an upgrade and before a restore. The newest is always kept. Only folders Lumiverb made are ever removed from the backups folder.

## Settings › Backups

A server admin sees **Settings › Backups**:

- When the last backup was made and how big it was, and when the next is due.
- **Back up now**, with how far it has got while it runs.
- If the last one failed, why.
- **Made**: each backup, its kind and size, with **Restore…**.

Settings shows a dot beside Backups when the last one failed, when none was made for two days, or when the folder can't be written to. The Admin page's Backups row turns red for the last two, and yellow when the last one failed.

## Where backups go

By default, `backups` in the data folder, on the same drive as Lumiverb's data. A folder on another drive keeps them if that drive fails. To choose one, a server admin, on the computer Lumiverb is installed on:

1. In **Settings › Backups**, under Folder, click **Change**.
2. Type the folder, such as `/Volumes/Backup/Lumiverb`, and click **Save**. If it isn't there yet, click **Make it**.

It applies from now on: backups made already stay where they are. **Use the default** goes back. A folder on a drive that isn't plugged in is refused. On Linux, a folder outside the data folder needs the API's service allowed to write there first: see [Storage](storage.md#on-linux-folders-outside-the-data-folder).

## Keep a copy of the env file

The databases keep AI machines' keys and plug-ins' secret settings sealed, with a key that's in the install's env file, `LUMIVERB_SECRETS_KEY`. A backup on its own can't open them. Keep a copy of the env file with your backups, somewhere safe: it holds that key and others.

- On a Mac: `$(brew --prefix)/etc/lumiverb/env`
- On Linux: `/etc/lumiverb/env` (readable by root only)

Restored without that key, everything else comes back, and Settings › AI and Settings › Plug-ins ask for those keys and settings again.

## Backing up from the command line

On a Mac:

```bash
lumiverb-server backup --env "$(brew --prefix)/etc/lumiverb/env"
```

On Linux:

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server backup --env /etc/lumiverb/env
```

## Restoring a backup

A restore replaces everything since that backup with what the backup holds. Lumiverb has to be stopped. In **Settings › Backups**, click **Restore…** beside the backup: it gives the three commands for that backup, to run in a terminal on the computer Lumiverb is installed on. They're the ones below, with the backup's folder in place of `BACKUP_FOLDER`.

On a Mac:

```bash
brew services stop lumiverb
```

```bash
lumiverb-server restore --env "$(brew --prefix)/etc/lumiverb/env" BACKUP_FOLDER
```

```bash
brew services start lumiverb
```

On Linux:

```bash
sudo systemctl stop lumiverb-scheduler lumiverb-api
```

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server restore --env /etc/lumiverb/env BACKUP_FOLDER
```

```bash
sudo systemctl start lumiverb-api lumiverb-scheduler
```

The restore asks before it starts (`--yes` skips the question). It checks the backup is whole, backs up what's there now, then puts each database back beside the live one and swaps it in only once it's complete, so a restore that fails changes nothing. Then it brings the databases up to this version of Lumiverb, so a backup from an older version restores too.

If it says Lumiverb is still running, it names what's still connected to the databases. Stop that first and run it again. On Linux, that can be the plug-in runner:

```bash
sudo systemctl stop lumiverb-plugins
```

Start it again with the others afterwards:

```bash
sudo systemctl start lumiverb-plugins
```

To undo a restore, restore the backup it made first: it's listed as "Before a restore".

## Restoring on another computer

To move Lumiverb to a new computer, or recover after losing one:

1. Install Lumiverb on the new computer and start it once. There's no need to create an account there. See [Install on a Mac](install-mac.md) or [Install on a Linux server](install-linux.md).
2. Copy the backup's folder to it, somewhere the user Lumiverb runs as can read (on Linux, the `lumiverb` user).
3. Stop Lumiverb there, and in its env file, put the `LUMIVERB_SECRETS_KEY` line from your copy of the old env file in place of its own.
4. Restore as above, then start Lumiverb.

Everyone signs in again. Libraries keep the folders they had: if the originals are somewhere else on the new computer, change each library's folder in its settings (see [Libraries](libraries.md#who-reads-it)). Previews are made again from the originals.

# The command line

Lumiverb has two commands. `lumiverb` talks to a Lumiverb server over the network, as you, the way the web app does. `lumiverb-server` looks after the install itself, on the server machine.

## Where they are

On a Mac, Homebrew puts both on your path when it installs Lumiverb. On a Linux server they're `/opt/lumiverb/.venv/bin/lumiverb` and `/opt/lumiverb/.venv/bin/lumiverb-server`.

Every command has its own help:

```bash
lumiverb --help
```

```bash
lumiverb producers --help
```

## Point lumiverb at a server

`lumiverb` needs the server's address and an API key.

**Get a key.** In the web app, **Settings › API Keys** (editors and admins), under **Create key**: a **Label** to know it by, and a **Role** no higher than your own. Click **Create key** and copy the key: it's shown once. The key acts as you, at its role.

**Set it.** Paste the key when asked; it isn't shown, and stays out of your shell's history:

```bash
lumiverb config set --api-url http://your-server:6464 --api-key -
```

It's saved in `~/.lumiverb/config.json`, readable by you alone. With no address set, `lumiverb` uses `http://localhost:6464`, a Mac install on the same Mac. To see what's set:

```bash
lumiverb config show
```

A device uses its device key the same way ([Libraries on another computer](devices.md)).

## Useful lumiverb commands

What a command can do is what the key's role can do in the web app.

| Command | What it does |
|---|---|
| `lumiverb library list` | Your libraries, their folders and last scan |
| `lumiverb library create --name NAME --path FOLDER` | A new library (admins) |
| `lumiverb library report-changes PATH` | Tell the server files changed, so it scans them now rather than at the next full scan |
| `lumiverb library people --name NAME` | Who can see and edit a library, and their role on it |
| `lumiverb library role --name NAME --email EMAIL --role editor` | Someone's role on one library (`default` puts their account role back) |
| `lumiverb scan --library NAME --explain` | For a library the server reads, on a computer that sees its folder: every difference between the disk and Lumiverb, and why. Changes nothing |
| `lumiverb search --library NAME --query "beach at sunset"` | Search a library |
| `lumiverb similar --library NAME --image photo.jpg` | Clips that look like a picture |
| `lumiverb download --library NAME --path PATH --output FILE` | A clip's preview (`--size preview.thumbnail` for its thumbnail) |
| `lumiverb user list` | The account's people and roles |
| `lumiverb user create --email EMAIL --role viewer` | Invite someone: prints the link they set their password at |
| `lumiverb producers` | What's made, missing and failing, as Settings › Processing shows it |
| `lumiverb producers failures` | Failing clips, their errors and next tries |
| `lumiverb producers retry image.faces --all` | Try failing clips again now (editors and admins) |
| `lumiverb producers settings video.transcript` | A kind of work's settings; `KEY=VALUE` changes one (admins) |
| `lumiverb pause image.description` | Pause a kind of work, `scans`, `upkeep` or `all` (admins); `resume` undoes it |
| `lumiverb enrich image.faces --library NAME` | Ask for that work in a library now; `--redo` makes it again (admins) |
| `lumiverb ai list` | AI machines and each job's model, as Settings › AI shows them |
| `lumiverb ai machines add Ollama --url http://localhost:11434/v1 --jobs vision` | Add an AI machine (admins); `--key` asks for its key |
| `lumiverb ai machines test Ollama` | Send a machine a test picture and video scenes, and show its answers |
| `lumiverb ai model vision qwen3-vl:8b-instruct` | Choose a job's model (admins) |
| `lumiverb plugins list` | Plug-ins, and whether each is on here; `enable ID`, `disable ID` (admins) |
| `lumiverb project list` | Your projects |
| `lumiverb project export --id ID --format fcp7` | A project as a bin for DaVinci Resolve or Premiere Pro (`fcpxml` for Final Cut Pro) |
| `lumiverb trash list` | Clips in the trash, and when each is deleted for good |
| `lumiverb archive list --missing` | Clips whose files went missing |
| `lumiverb settings show` | The account's settings |
| `lumiverb device run` | Read libraries for a server elsewhere ([Libraries on another computer](devices.md)) |

A kind of work is named by what it makes, with or without `lumiverb.` in front: `image.description`, `image.text`, `image.likeness`, `video.scene_likeness`, `image.faces`, `video.faces`, `video.transcript`, `video.scenes`, `video.scene_descriptions`, `video.description`, `video.analysis_proxy`, `video.format`, `capture_time`, `location.guess`. A plug-in's work is named by its id. `lumiverb producers` shows each name in parentheses after its title.

## lumiverb-server

`lumiverb-server` runs on the server machine and reads the install's env file, which you always name with `--env`. On a Mac:

```bash
lumiverb-server server-admins --env "$(brew --prefix)/etc/lumiverb/env"
```

On Linux, with `sudo`, as the env file is root's:

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server server-admins --env /etc/lumiverb/env
```

| Command | What it does |
|---|---|
| `setup` | Make the first admin, while nobody can sign in yet (`--email`, `--account-name`) |
| `reset-password --email EMAIL` | Set someone's new password, typed twice. It signs them out everywhere and ends the API keys they made |
| `server-admins` | Who looks after the install: its accounts, network, backups and log. `add EMAIL` or `remove EMAIL` changes that |
| `pause NAME`, `resume NAME` | Pause or resume processing in every account, as Settings › Processing does: `all`, `scans` (`--library NAME`), `upkeep` or a kind of work. `--account NAME` for one account |
| `backup` | Back up every database now ([Backups and restore](backups.md)) |
| `restore BACKUP` | Put every database back from a backup, with Lumiverb stopped. What's there is backed up first |
| `migrate` | Bring the databases up to this version. Updates do it for you ([Updating](updating.md)) |
| `plugins` | Plug-ins: `install`, `default`, `uninstall` ([Plug-ins](plugins.md)) |
| `ai-worker` | Run this machine as an AI worker for another server ([A second machine for search and faces](ai-worker.md)) |
| `run` | A Mac's whole Lumiverb service. Homebrew starts it; you don't run it yourself |

For example, to give someone a new password on a Mac:

```bash
lumiverb-server reset-password --email you@example.com --env "$(brew --prefix)/etc/lumiverb/env"
```

To make someone a server admin on Linux:

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server server-admins add you@example.com --env /etc/lumiverb/env
```

`lumiverb-server` needs no API key: being on the machine, able to read its env file, is what lets it in.

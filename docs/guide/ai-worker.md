# A second machine for search and faces

The server finds faces and makes the vectors behind visual search itself. Another computer with a GPU can share that work by running Lumiverb's **AI worker**. This page sets one up on a Mac or a Linux machine and adds it to your server.

## What the AI worker is

The AI worker holds no library, no account and no files. The server sends it each photo's preview (and frames of videos, for faces) and it sends back what it found. It uses the same models as the server, so a photo comes out the same whichever machine did it. It uses an NVIDIA GPU, or a Mac's own GPU, when there is one.

It does visual search and faces only. Descriptions, text in photos and transcripts are other AI machines' work: see [AI machines](ai-machines.md).

The first photos it gets download its models once, into its own data folder.

## On a Mac

Install Lumiverb with Homebrew, as for a server:

```bash
brew trust --tap bubbafat/lumiverb
```

```bash
brew install bubbafat/lumiverb/lumiverb
```

Don't start its service (`brew services start lumiverb`): that's a whole server. Instead, install the worker as a service of its own:

```bash
lumiverb-server ai-worker --env "$(brew --prefix)/etc/lumiverb/env" --install
```

It starts now and again at login, and restarts itself after `brew upgrade`. Its log is `ai-worker/worker.log` in the install's data folder (`DATA_DIR` in the env file). To take it away:

```bash
lumiverb-server ai-worker --env "$(brew --prefix)/etc/lumiverb/env" --uninstall
```

## On Linux

On a Linux machine with Lumiverb installed, run the worker as a systemd unit. Create `/etc/systemd/system/lumiverb-ai-worker.service` with:

```ini
[Unit]
Description=Lumiverb AI worker (search and faces for another Lumiverb)
After=network-online.target

[Service]
User=lumiverb
EnvironmentFile=/etc/lumiverb/env
ExecStart=/opt/lumiverb/.venv/bin/lumiverb-server ai-worker
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then start it, now and at every boot:

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now lumiverb-ai-worker
```

Its log:

```bash
journalctl -u lumiverb-ai-worker -f
```

## Its key

Every request to the worker needs its key, so only your Lumiverb can use it. The worker makes one the first time and keeps it. On a Mac:

```bash
lumiverb-server ai-worker --env "$(brew --prefix)/etc/lumiverb/env" --show-key
```

On Linux, once the unit has started, it's in the data folder (`DATA_DIR` in `/etc/lumiverb/env`, `/var/lib/lumiverb` unless you chose another):

```bash
sudo cat /var/lib/lumiverb/ai-worker/key
```

## Add it to your server

As an admin, in the Lumiverb it should help:

1. **Settings › AI › Add machine**. Leave **Where** at Custom URL.
2. **Name**: anything, such as "Gaming PC".
3. **Endpoint URL**: `http://` and that computer's address, then `:6366/v1`, such as `http://192.168.1.30:6366/v1`.
4. **API key (optional)**: the worker's key.
5. Click **Connect**. It says "Connected: offers ViT-B-32, buffalo_l".
6. Under **Does**, tick **Visual search (CLIP)** and **Faces**, then **Add**.

The machine's row then says "Online · checked …", or "Offline:" and why. Lumiverb checks every machine every minute.

The server's own **Built in** machine keeps doing visual search and faces too, and work is spread across both. To leave them to the worker, click **Edit** on Built in and untick them under **Does**.

## Requests at once

Each machine works on one photo at a time on its GPU. **Requests at once** in Settings › AI (2 to begin with) is how many the server sends it together, so the next is ready when one finishes. The worker itself takes 2 at once unless started with `--at-once N`.

## Its network

The worker listens on port 6366 on every network interface, over plain HTTP. `--port` and `--host` change that; on a Mac, give them with `--install` so the service keeps them. Its key keeps others from using it, but the previews it's sent cross your network unencrypted, as they do to an Ollama machine. Run it only on a network you trust.

If **Connect** can't reach it, check that the worker is running (its log), that the address and port are right, and that nothing on that computer blocks port 6366.

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

## Its key and its certificate

Every request to the worker needs its key, so only your Lumiverb can use it. The worker serves HTTPS with a certificate of its own, and your Lumiverb takes that certificate and no other, by its fingerprint: the key and the photos it's sent cross your network encrypted, to the worker alone. It makes both the first time and keeps them, in `ai-worker/` in its data folder.

When it starts, and when it's installed with `--install`, it prints what to add: its `https://` address, its certificate's fingerprint, and how to see its key. On a Mac:

```bash
lumiverb-server ai-worker --env "$(brew --prefix)/etc/lumiverb/env" --show-key
```

```bash
lumiverb-server ai-worker --env "$(brew --prefix)/etc/lumiverb/env" --show-fingerprint
```

On Linux, once the unit has started, its key is in the data folder (`DATA_DIR` in `/etc/lumiverb/env`, `/var/lib/lumiverb` unless you chose another), and its log says the rest:

```bash
sudo cat /var/lib/lumiverb/ai-worker/key
```

```bash
sudo journalctl -u lumiverb-ai-worker | grep -A2 "add a machine"
```

## Add it to your server

As an admin, in the Lumiverb it should help:

1. **Settings › AI › Add machine**. Leave **Where** at Custom URL.
2. **Name**: anything, such as "Gaming PC".
3. **Endpoint URL**: `https://` and that computer's address, then `:6366/v1`, such as `https://192.168.1.30:6366/v1`.
4. **Certificate fingerprint**: the one the worker printed, such as `7F:3A:…` (32 pairs).
5. **API key (optional)**: the worker's key.
6. Click **Connect**. It says "Connected: offers ViT-B-32, buffalo_l".
7. Under **Does**, tick **Visual search (CLIP)** and **Faces**, then **Add**.

From the command line, the same with `lumiverb ai machines add Worker --url https://192.168.1.30:6366/v1 --fingerprint 7F:3A:… --jobs clip,faces --key`.

If the worker's certificate is ever not the one it was added with (its data folder made again, say, or another computer at that address), its row says "Its certificate isn't the one it was added with", and nothing is sent to it. **Edit** it and give the fingerprint the worker prints now.

The machine's row then says "Online · checked …", or "Offline:" and why. Lumiverb checks every machine every minute.

The server's own **Built in** machine keeps doing visual search and faces too, and work is spread across both. To leave them to the worker, click **Edit** on Built in and untick them under **Does**.

## Requests at once

Each machine works on one photo at a time on its GPU. **Requests at once** in Settings › AI (2 to begin with) is how many the server sends it together, so the next is ready when one finishes. The worker itself takes 2 at once unless started with `--at-once N`.

## Its network

The worker listens on port 6366 on every network interface, over HTTPS. `--port` and `--host` change that; on a Mac, give them with `--install` so the service keeps them.

If **Connect** can't reach it, check that the worker is running (its log), that the address and port are right, that it starts with `https://` and has the certificate fingerprint, and that nothing on that computer blocks port 6366. Without the fingerprint, **Connect** says its certificate isn't signed by an authority the server trusts.

## A worker from before HTTPS

A worker set up before it served HTTPS goes on serving plain HTTP after an update, as your Lumiverb has its `http://` address: nothing stops working. Its log says so loudly each time it starts, and Settings › AI marks the machine as unencrypted. To switch:

1. Start it with `--https`. On a Mac:

   ```bash
   lumiverb-server ai-worker --env "$(brew --prefix)/etc/lumiverb/env" --install --https
   ```

   On Linux, add `--https` to the end of `ExecStart` in its unit, then:

   ```bash
   sudo systemctl daemon-reload
   ```

   ```bash
   sudo systemctl restart lumiverb-ai-worker
   ```

2. It makes its certificate and prints what to add. Until the next step, your Lumiverb can't reach it.
3. In Settings › AI, **Edit** the machine: change `http://` to `https://` in its URL (its saved key stays), paste the **Certificate fingerprint**, **Connect**, then **Save**.

`--plain-http` serves plain HTTP on purpose, said just as loudly: its key and the previews it's sent cross your network unencrypted, as they do to an Ollama machine. Use it only on a network you trust.

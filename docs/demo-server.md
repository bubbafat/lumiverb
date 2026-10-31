# A demo server (try.lumiverb.io)

A Linux install like any other (`docs/linux-server.md`), on the internet,
with two settings that make it a demo:

- **Try it as a viewer.** The sign-in page offers it: anyone is signed in,
  with no password, as one viewer, for a session that reads only (search,
  browse, a clip's details and people, nothing changed). `LUMIVERB_DEMO_VIEWER`
  names that viewer (`src/server/demo.py`).
- **Only what a visitor's pages call.** From anywhere but the machine
  itself, every other route answers 404, as one that isn't there: no
  sign-in with a password, no setup, admin, settings, uploads, nothing a
  scanner or device calls (`LUMIVERB_EXPOSURE=demo`, `src/server/api/exposure.py`;
  the routes are in `docs/cursor-api.md`, "What a server answers from
  elsewhere"). Public libraries' and projects' pages work as anywhere.

Everything an admin does, they do on the machine itself: a command in an
SSH session there, or the web app through an SSH tunnel. No screen is
needed: "the machine itself" is any connection from it, and from there
every route answers, as on any install. The content is
copied onto the server's own storage and scanned there; nothing is uploaded
through the API.

## Setup

### 1. The droplet

- Ubuntu LTS, with an SSH key for a sudo user (not root), and password
  login off.
- DNS: an A record for `try.lumiverb.io` to the droplet's address.
- The Volume mounted, in `/etc/fstab` as DigitalOcean's instructions put
  it, say at `/mnt/demo`. Two folders on it: `/mnt/demo/library` for the
  clips, `/mnt/demo/lumiverb` for what Lumiverb makes.

### 2. The API and the scheduler

On the droplet, from a checkout of this repository:

```bash
sudo bash scripts/deploy-api.sh --domain try.lumiverb.io --worker --exposure demo --data-dir /mnt/demo/lumiverb --root-map /mnt/demo/library=/mnt/demo/library
```

The API listens on 127.0.0.1:8000 only, and ufw lets in SSH alone until the
web step. `--exposure demo` writes `LUMIVERB_EXPOSURE=demo` in
`/etc/lumiverb/env` (remembered by reruns): from the first start, only what
a visitor's pages call answers from the internet. Signing in with a
password isn't one of them, so nothing an admin does is open to anyone else
while the demo is set up.

### 3. The web app and its certificate

```bash
sudo bash scripts/deploy-web.sh --domain try.lumiverb.io --api-upstream http://127.0.0.1:8000 --email you@example.com
```

certbot gets the certificate and redirects HTTP to HTTPS; ufw lets in 22,
80 and 443. Let's Encrypt is asked only once the domain points at this
machine: until then the script says which record to add, at what address,
and the site serves HTTP until it's run again.

### 4. The tunnel, and the server's limits in nginx

In `/etc/nginx/sites-available/lumiverb`, in the server block that listens
on 443, add a second `listen`, for the tunnel: plain HTTP on this machine
alone.

```nginx
    listen 127.0.0.1:8080;
```

In its `location /v1/` block, lower what a request may send (the API
reads only small bodies here) and hold each address to a rate:

```nginx
        client_max_body_size 1m;
        limit_req zone=lumiverb burst=300 nodelay;
```

and name that rate in its own file, `/etc/nginx/conf.d/lumiverb-limits.conf`:

```nginx
limit_req_zone $binary_remote_addr zone=lumiverb:10m rate=30r/s;
```

Then `sudo nginx -t` and `sudo systemctl reload nginx`. An update keeps
these: `update-web.sh` adds what the site lacks and changes only lines that
are still as Lumiverb wrote them (`src/server/lifecycle/site_update.py`
says what each update brings).

From your own computer, the tunnel, and Lumiverb at http://localhost:8080:

```bash
ssh -N -L 8080:127.0.0.1:8080 try
```

(`try` being your `~/.ssh/config` entry for the droplet.) Requests through
it come from the machine itself, so every route answers them.

### 5. The first admin

In your SSH session on the droplet (it asks for a password of 12 characters
or more):

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server setup --env /etc/lumiverb/env --email you@example.com
```

Then sign in through the tunnel, at http://localhost:8080: from
`https://try.lumiverb.io`, signing in with a password answers 404.

### 6. The content

Copy the clips onto the Volume from your own computer:

```bash
rsync -rltv --chmod=D755,F644 --progress --exclude='.DS_Store' ~/Demo/ try:/mnt/demo/library/
```

(`-rlt` and `--chmod` rather than `-a`: a Mac's own owner and modes would come along, and a file only its
owner may read is one the scheduler, the `lumiverb` user, can't open.)

then, through the tunnel, **Add library** with the folder
`/mnt/demo/library`. The scheduler scans it within 15 minutes, makes the
previews and analysis copies, and runs CLIP, faces and transcripts on the
droplet's CPU (slow for long videos: keep them short, or pause transcripts
in Settings › Processing). More clips later: rsync again; the next scan
finds them.

### 7. Descriptions, once, with OpenAI

Through the tunnel, **Settings › AI › Add machine**: a name (OpenAI), the
URL `https://api.openai.com/v1` (or Anthropic's, `https://api.anthropic.com/v1`, for Claude), your API key (typed there, never sent
anywhere else: Lumiverb keeps it sealed with the install's own key),
**Connect**, a vision model it lists (gpt-4.1-mini, say), tick
**Descriptions & text**, 4 at once, **Add**. Or, in your SSH session, as
the service user (it asks for the key, and never shows it):

```bash
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb ai machines add OpenAI --url https://api.openai.com/v1 --jobs vision --at-once 4 --key
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb ai machines models OpenAI
sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb ai model vision gpt-4.1-mini
```

(`lumiverb ai list` shows the machines and each job's model; `machines
edit` and `machines remove` change or remove one.) Before the batch, try it
(**Test** on its row, or `lumiverb ai machines test OpenAI`): it's sent a
picture Lumiverb draws and a video's scenes with your settings, and says
each answer and the tokens it used, or why it wouldn't do (a model that
thinks first can spend all of an answer's room). Each clip is one request: a
1280 px image and the prompt. Settings › Processing shows what's left;
when nothing is, remove the machine (its key goes with it). Transcripts
stay on the built-in Whisper (OpenAI's own API takes 25 MB a request: not
supported).

### 8. What visitors see

- **A public library**: Libraries, its **Not public** button (it turns to
  **Public**), then **Copy link**. Through the tunnel that link starts
  `http://localhost:8080`: visitors open it at `https://try.lumiverb.io`.
- **A public project**: the project's Edit, visibility **Public (anyone
  with link)**: `https://try.lumiverb.io/public/projects/<project id>`.
- **Try it as a viewer**: one viewer, made once, whom every visitor is
  signed in as (not someone invited per visitor). On the droplet, as the
  service user, whose command line the install set up for the account:

  ```bash
  sudo -u lumiverb -H /opt/lumiverb/.venv/bin/lumiverb user create --email demo@lumiverb.io --role viewer > /dev/null
  ```

  Its invite link is thrown away unseen, so the viewer's password stays one
  nobody knows. Then, in `/etc/lumiverb/env`:

  ```
  LUMIVERB_DEMO_VIEWER=demo@lumiverb.io
  ```

  and `sudo systemctl restart lumiverb-api`. A viewer sees every library
  unless it's private (Library settings › People); projects aren't a
  viewer's (an editor's and up), so show those as public projects.
  (`LUMIVERB_EXPOSURE=demo` is there already, from the install; for an
  install without `--exposure demo`, add it here by hand.)

### 9. Checks, from anywhere else

```bash
curl -s https://try.lumiverb.io/v1/auth/demo
```

says `{"on":true}`.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://try.lumiverb.io/v1/auth/login
```

and

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://try.lumiverb.io/v1/system/health
```

each say `404`. The API's log names each route turned away (`not exposed
here`).

### 10. lumiverb.io's page and docs.lumiverb.io, from the same droplet

The front page is the checkout's `site/` folder, served as it is by the
same nginx, as a site of its own. With A records for `lumiverb.io` and
`www.lumiverb.io` to the droplet's address:

```bash
sudo bash /opt/lumiverb/scripts/deploy-site.sh --domain lumiverb.io --domain www.lumiverb.io
```

(`--email you@example.com` only if certbot has no account here yet: step 3
made one.) Until this step, `https://lumiverb.io` answers with the web app's
certificate: nginx falls back to the one site it has. It writes `/etc/nginx/sites-available/lumiverb-site` (the `site/` folder
and nothing around it; the page's own Content-Security-Policy as a header),
takes it out again if nginx refuses it, and gets a certificate for each name
that points here, saying which record to add for any that doesn't. Run it
again once that record is there. Nothing in it reaches the API: `/v1/` there
is a 404.

The user guide is the checkout's `docs-site/` folder (built from `docs/guide/`
by `scripts/build_docs.py` and kept built in the repository, so nothing is
built here). With an A record for `docs.lumiverb.io`:

```bash
sudo bash /opt/lumiverb/scripts/deploy-site.sh --folder docs-site --domain docs.lumiverb.io
```

It's a site of its own too (`/etc/nginx/sites-available/lumiverb-docs-site`),
with its own certificate.

## Updating

```bash
sudo bash /opt/lumiverb/scripts/update.sh
```

keeps the env file's settings and nginx's edits. Run the checks again after.
The front page changes with the checkout: an update brings its changes too,
with nothing to rerun.

The checkout, `/mnt/demo/lumiverb` and the service user's home are the
`lumiverb` user's, and nothing in them steers what the update does as root
(linux-server.md, "Root and the `lumiverb` user"): it stops, saying what to
fix, at a symbolic link where Lumiverb keeps a folder in the data dir, and
root runs no git there. To look at the checkout, run git as that user
(`sudo -u lumiverb git -C /opt/lumiverb log -1`). A plug-in on the demo runs
as that user too, and can read what it can: install only plug-ins you
trust.

## Turning it off

Remove `LUMIVERB_DEMO_VIEWER` (every demo session ends at its next
request) or `LUMIVERB_EXPOSURE` (every route answers from anywhere again)
from `/etc/lumiverb/env`, and restart the API.

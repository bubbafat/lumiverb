# Troubleshooting

When something isn't right, start with the Admin page and the log: between them they usually say what's wrong. This page also covers the problems people meet most.

## The Admin page

Admins have **Admin** in the sidebar, with a dot when something needs a look. Under **System health**, each row is green, yellow or red, with one line saying why and a link to where it's fixed. It refreshes every 10 seconds.

| Row | When it isn't green |
|---|---|
| Website | The database isn't answering |
| Processing | Paused, clips failed in the last day, or the scheduler (Lumiverb's processing) has stopped reporting |
| AI machines | A machine is offline, or no machine can do a job that has work |
| Search | Many clips aren't searchable yet; they're added within minutes |
| Storage | A library's folder can't be reached |
| Disk | Server admins: the data disk or the previews' drive is nearly full or not there |
| Backups | Server admins: no backup for 2 days, or the last one failed |
| Lumiverb's own plug-ins | Server admins: one couldn't be installed |

Under **Libraries**, each library says whether its files can be reached: Reachable, Can't be reached, or Not checked lately.

## The log

Server admins see **Admin › Logs**: newest first, filtered by part (API, Processing, Plug-ins and more), with **Search**, **Problems only**, **Follow**, and **Download these lines** to send to someone helping you. On a server with more than one account, it shows only on the machine Lumiverb runs on.

When a page says something like "Load failed" on a phone, the request never reached Lumiverb, so the server saw nothing. The web app remembers each one and sends it once a request gets through. It shows in the log under API as "web: … couldn't reach the server for …", with who, when, the page and their browser. Search for **web:** to find them.

On a Mac the log is one file, kept to the latest 50 MB:

```bash
tail -f "$(brew --prefix)/var/log/lumiverb.log"
```

On Linux it's in the system journal:

```bash
journalctl -u lumiverb-api -u lumiverb-scheduler -f
```

Admin › Logs can show the journal once Lumiverb's service user may read it. Lumiverb doesn't do this for you, because it lets Lumiverb read the whole system journal (every service, and the commands people ran with sudo), not only its own lines:

```bash
sudo usermod -aG systemd-journal lumiverb
```

```bash
sudo systemctl restart lumiverb-api
```

## Restart Lumiverb

On a Mac:

```bash
brew services restart lumiverb
```

On Linux:

```bash
sudo systemctl restart lumiverb-api lumiverb-scheduler lumiverb-plugins
```

## Nothing is being processed

Settings › Processing says "The scheduler isn't running", or the Processing row is red.

- If it says **Paused**, an admin paused everything: turn **All processing** back on ([Processing](processing.md#pausing)).
- Otherwise restart Lumiverb, and look at the log's Processing lines for why it stopped.
- On Linux, processing runs only on a server installed with `--worker`, as `lumiverb-scheduler`.

## Work waits

Each row in Settings › Processing says why its work waits, in a sentence, amber when someone has to act. [Why work waits](processing.md#why-work-waits) lists them.

## A forgotten password

- **At the Mac Lumiverb is installed on**: on the sign-in page, click **Forgot password?**, type your email and click **Continue**. It goes straight to setting a new password.
- **Anywhere else**: Forgot password emails a link if the server has email set up (on Linux, the `SMTP_` lines in `/etc/lumiverb/env`). If not, the page says so.
- **On the server itself, any install**: set a new one. It asks for the email, then the new password twice, and signs that person out everywhere. On a Mac:

```bash
lumiverb-server reset-password --env "$(brew --prefix)/etc/lumiverb/env"
```

On Linux:

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server reset-password --env /etc/lumiverb/env
```

## A phone or another computer can't reach Lumiverb

A page that says "Load failed" or "Couldn't reach the server" got no answer at all.

- **Lumiverb on a Mac** is for that Mac only until a server admin, on that Mac, turns on **Let other devices on my network use Lumiverb** in Settings › Network. Then open one of the addresses listed under "On another device, open:".
- If the Mac asks whether nginx may accept incoming network connections, choose **Allow**.
- The phone must be on the same network, and the Mac awake.
- After a change of ports or HTTPS in Settings › Network, use the new address.
- If one browser on the phone fails and another works, use the one that works.

More in [Network, phones and HTTPS](network.md).

## A library's drive is unplugged

The Storage row says "Can't reach Photos", and rows in Settings › Processing say "… in Photos wait for its drive: it can't be reached since …". Lumiverb looks at each library's folder about every 30 seconds. An empty folder counts as not there, so nothing is marked missing while the drive is away. Browsing and search go on, and so does work made from the previews; only work that needs the original files waits. Plug the drive back in, or mount the share, and Lumiverb carries on by itself.

If the previews are on a drive of their own, the Disk row says "The previews folder isn't there" until it's back ([Storage](storage.md)).

If a scan finds most of a library gone while its drive is there, Lumiverb asks before archiving anything, and archives nothing. The log names the library and gives the command to run if the files really are gone.

## Files that can't be read

Under the library in Settings › Processing › Scans: "12 files in Photos can't be read: the lumiverb user isn't allowed to read them (…). Make them readable and the next scan takes them in." Give the user it names permission to read those files and their folders. On Linux that's the `lumiverb` user; on a Mac, the user Lumiverb runs as.

## An AI machine isn't working

Settings › AI shows each machine as "Online" or "Offline:" with why. When no machine can do a job, a red box says so: "The work waits until it's fixed; no clip is marked as failed for it."

- **Edit**, then **Connect**, asks the machine for its models now, and says why when it can't.
- **Test** sends it a picture Lumiverb draws and a video's scenes, with the account's settings and the Descriptions & text model, and shows each answer, or what's wrong with it. It needs that model chosen first.
- Common causes: Ollama not listening on the network, the model not pulled there, or a model that thinks before it answers and leaves the answer empty. See [AI machines](ai-machines.md).

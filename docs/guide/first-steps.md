# First steps

Once your account exists, Lumiverb walks you through setting it up, one step at a time, ending with your first
library. Each step says what skipping it costs, and you can leave and pick it up again later.

## Sign in

Open Lumiverb in a browser (on a Mac, [http://localhost:6464](http://localhost:6464); on a server, its address)
and sign in with the email and password you chose. The first time, an admin is taken to **Set up Lumiverb**.

## Set up Lumiverb

The steps come in the order that matters: what's hardest to change later comes first. Each says what skipping it
leaves undone. **Continue** moves on, whether you changed anything or not; **I'll finish later** leaves it for
another time.

1. **Where previews live.** Previews grow with your libraries: about 15 GB per 100,000 photos, and up to 1 GB per
   hour of video. Moving them later copies them all, so choose a drive with room now. The same step chooses the
   folder for backups of the database: one on another drive keeps them if this one fails. Skipped, both stay on
   the computer's own drive. More in [Storage](storage.md) and [Backups and restore](backups.md).
2. **Who can open it.** On a Mac, only the Mac itself can open Lumiverb at first. Let the devices on your network
   use it so your phone, other computers and the people you invite can reach it. On a Linux server this was
   decided by the install. More in [Network, phones and HTTPS](network.md).
3. **What to find.** How hard Lumiverb looks for faces, in photos and in videos: Off, Low, Normal or High. Set this
   before your first library, since faces are found as it's scanned. Skipped, faces are found at Normal in photos
   and Low in videos. More in [People and faces](people.md).
4. **AI machines.** Most of Lumiverb needs nothing more. Descriptions, tags and the text in photos need an AI
   machine: a computer running Ollama with a vision model, or a provider you have a key for. Skipped, those wait,
   and catch up on everything once a machine is added. More in [AI machines](ai-machines.md).
5. **People.** Invite the people you share with: each gets a link and chooses their own password. Viewers browse and
   rate; editors can also name and organize; admins can change everything. More in
   [Users and roles](users-and-roles.md).
6. **Your first library.** A name, and the folder its photos and videos are in. Lumiverb reads it and never
   changes it. **Guess places** places the photos and videos that have no location from those taken around the
   same time. More in [Libraries](libraries.md) and [Places](places.md).

The first two steps are the install's, so only a server admin sees them (the person who created the first account
is one). Until every step is done, the Admin page offers **Continue setting up**, or **Don't show this again**.

## Your first library

A library is a folder of photos and videos, and everything in the folders inside it. On a Mac, the folder is a
path such as `/Volumes/Photos/Family` or `/Users/you/Pictures/Archive`, and it can be chosen only on the Mac
itself. On a Linux server, it's a path on the server, inside a folder the install lists
([The folders it may read](install-linux.md#the-folders-it-may-read)).

More libraries are added at **Manage libraries › Add library**. Make one for each part of your collection that
you'd browse or share on its own: family photos, a client's shoots, a film's footage. A library can be kept
private to some people ([Users and roles](users-and-roles.md)).

## What happens next

Lumiverb starts on the library at once:

1. **The scan** finds every photo and video and makes its preview, so the library fills in as you watch.
2. **Then the rest**, clip by clip: faces, copies of videos to analyze, scenes and transcripts, similar-photo
   search, and, with an AI machine, descriptions, tags and the text in photos.

A large library takes a while, and video longest. You can browse and search what's done at any time. Settings › Processing shows what's left of each kind of work, and why
anything is waiting ([Processing](processing.md)).

After that, Lumiverb looks for new and changed files in every library every 15 minutes, and works through them
the same way. Nothing in your folders is ever changed. A file you move is followed to its new place; a file you
delete there is archived in Lumiverb as missing, with what Lumiverb knew about it.

## Next

- [Browse and search](browse-and-search.md): finding things.
- [AI machines](ai-machines.md), if you skipped that step.
- [Users and roles](users-and-roles.md): inviting people.

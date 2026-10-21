# Lumiverb

Lumiverb makes your photos and videos searchable by what's in them. It runs on your own Mac or Linux server,
reads the folders your files are already in, and never changes, moves or uploads them.

## What it does

Point Lumiverb at a folder and it finds every photo and video in it, then works through them on its own:

- **Previews** of every photo and video, so the library browses quickly in any browser, phones included.
- **Descriptions, tags and the text in photos** (signs, screens, documents), written by an AI machine you
  choose: one on your own computer, or a provider you have a key for.
- **Search in your own words.** Type "a red car in the snow" and it finds the photos and videos whose
  descriptions, tags, text, people, places or spoken words match.
- **Similar photos.** From any photo, find more that look like it. This needs no AI machine.
- **Faces and people.** Faces are found in photos and videos and grouped; name a person once and find them
  everywhere.
- **Videos, scene by scene.** Each scene is described and what's said is transcribed, so a search reaches
  inside every video.
- **Places**, from your camera's and phone's GPS, with place names and landmarks if you turn them on.

It keeps looking: new files in a library are found every 15 minutes and worked through in turn.

## How it works

Lumiverb is a server: one program on one machine, which you open in a browser.

- **On a Mac**, it installs with Homebrew and runs as one service that starts at login. It reads your folders
  where they are, on the Mac or its drives.
- **On a Linux server**, an install script sets it up with its own web server and certificate.

Either way, everything it makes and learns (previews, descriptions, faces, ratings, projects) is kept apart from
your originals, in its own folder and database, and backed up every night. Your originals are only ever read.

Everyone you invite opens the same address in their browser. Each person has a role (viewer, editor or admin),
and each library can be open to everyone or kept private.

## Where to start

- [Install on a Mac](install-mac.md), or [on a Linux server](install-linux.md).
- Then [First steps](first-steps.md): your account, the setup that follows, and your first library.
- To look around before installing anything, [the demo](https://try.lumiverb.io) is a real Lumiverb with two
  small libraries, open to anyone as a viewer.

Lumiverb is free and open source, under the MIT license. Its code, and where to report a problem, are on
[GitHub](https://github.com/bubbafat/lumiverb).

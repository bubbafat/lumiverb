# Videos

Lumiverb plays your videos in the browser, cuts them into scenes and describes them, transcribes what's said, and finds the people in them. Your video files are only read, never changed.

## What Lumiverb makes for a video

Once a video is scanned, Lumiverb makes these for it in the background, each when what it needs is ready:

| What | Made by | Where you see it |
|---|---|---|
| A 10-second preview | the scan | The grid, when you hover; the viewer, until the analysis copy is ready |
| An analysis copy | the server | The video the viewer plays. Scenes, faces and the transcript are made from it |
| Scenes | the server | Nowhere directly: they're what gets described |
| Scene descriptions and tags | an AI machine | Search |
| Each scene's picture as a visual search (CLIP) vector, as photos have | the server | Nowhere directly: kept with each scene |
| The video's description and tags | an AI machine, from its scene descriptions | The viewer's **Description** and **Tags** |
| A transcript | the server | The viewer's **Transcript** |
| Faces | the server | The viewer's **People** |

The analysis copy is a smaller copy that every browser plays: full length, every audio track, 960 pixels on its longest side unless an admin changes it in **Settings › Processing** (**Analysis proxies**).

Descriptions need an AI machine doing **Descriptions & text** in **Settings › AI** (see [AI machines](ai-machines.md)). Until there is one, they wait, and the rest carries on. **Settings › Processing** shows how far each has got and why anything waits (see [Processing](processing.md)).

## Playing a video

Click a video to open it in the viewer. A colored frame around the player says how much of it plays:

- Amber, "Short preview: first 10 seconds.": the analysis copy isn't ready yet. "The full video plays once it's processed."
- Green, "Full video": the whole video. Or "Plays the first … seconds" when your account limits playback.

If the video moves to the full copy while you watch, it carries on from the same moment. Press **Shift+F** for full screen; the other viewer keys are in [Browse and search](browse-and-search.md#the-viewer).

How much plays is the account's choice, in **Settings › Playback**. Everyone sees it there; an admin changes it:

- **Signed in**: **Whole video**, or the **First** so many seconds.
- **Public pages**: **Show the first** so many seconds (10 unless an admin changes it), or **Show the whole video**. A public page never plays more than signed-in people get.

## Scenes and descriptions

Lumiverb cuts each video into scenes where the picture changes, then joins neighbouring scenes that look alike, so a talking head is one scene. How finely is the **Detail** of the **Scene descriptions** row in **Settings › Processing**: **Low**, **Normal** (the default) or **High**. An admin changes it. A new Detail applies to videos from then on; the row then counts the videos made at a lower one, and an admin can click **Redo** for them.

An AI machine describes each scene and tags it. Once every scene of a video is described, it writes the video's own description and tags from them, in order. That's what the viewer shows as the video's **Description** and **Tags**, and editors and admins correct it the same way as a photo's (**Edit**, marked **Edited**).

The web app doesn't list a video's scenes. Their descriptions are what search reads, and what the video's description is written from.

The server also turns each scene's picture into the kind of vector visual search keeps for a photo (**Visual search in videos (CLIP)** in **Settings › Processing**), with no AI machine to set up: the **Built in** machine does it, or one you add for visual search ([A second machine for search and faces](ai-worker.md)). When a video's scenes are found again, a scene on a picture that was already done keeps its vector, so only new pictures are done.

## Transcripts

The server transcribes what's said in each video with Whisper, built into Lumiverb, skipping the silences. Nothing to set up. An admin can choose another Whisper model, or a machine of your own, for **Transcripts** in **Settings › AI** (see [AI machines](ai-machines.md)).

The viewer's **Transcript** shows each line with its start time, and the language heard. Click a line to jump the video there; this works once the whole video plays, and only within what your account plays.

- **Download** saves it as an `.srt` file.
- **Replace** (editors and admins) uploads an `.srt` of your own. It's shown instead, and the machine never writes over it.
- **Remove** (editors and admins) takes your transcript away, and the machine makes one again; or removes the machine's for good.
- A video with no transcript offers editors and admins **Upload SRT**.

On a public page, the transcript stops where playback does.

## Search inside videos

A search finds a video when its words match: its description and tags, its scenes' descriptions and tags, or what's said in it. Put a phrase in quotes to find words said together. See [Browse and search](browse-and-search.md#search) for how words match.

The web app opens a video it found at the start. The command line's search also lists the scene that matched, with its start and end:

```bash
lumiverb search --library Family --query "blowing out candles"
```

## People in videos

Each person seen in a video is one face, with every time they're seen. The viewer's **People** section lists them, and clicking a time plays from there. How many frames are looked at is the **Videos** level of the **Faces** row in **Settings › Processing**: **Low** (the default) looks at one frame from each scene. See [People and faces](people.md#people-in-a-video).

## Sending videos to an editing app

Put the clips in a project and export it for DaVinci Resolve, Premiere Pro or Final Cut Pro: see [Send to an editing app](ratings-projects-sharing.md#send-to-an-editing-app).

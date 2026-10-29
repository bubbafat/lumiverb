# Browse and search

Lumiverb's web app shows the photos and videos in every library you can see, and finds them by what's in them. This page covers the grid, search, filters, the viewer, and putting clips away.

## Where to browse

- **A library**: click its name in the sidebar, or press ⌘K (Ctrl+K) and type part of its name. Its folders appear under it, each with how many clips it holds; click one to see only that folder. On a phone, the menu button beside the library's name opens its folders.
- **All photos** in the sidebar: every library you can see, together.
- **Favorites** in the sidebar: what you hearted (see [Ratings](ratings-projects-sharing.md#ratings)).

Which libraries you see depends on your role in each: see [Users and roles](users-and-roles.md).

The grid puts clips under a heading for each day; the **Grid density** slider at the top right sizes the thumbnails. Videos show their length and play a silent preview when you hover over them. The grid updates itself as new clips are scanned.

## Search

Type in the **Search ...** box. Results come after a pause, or when you press Enter. On a library's page the search looks in that library (and folder, if one is chosen); in **All photos** it looks in every library you can see.

A search reads the words Lumiverb has for each clip:

- its description and tags, written by an [AI machine](ai-machines.md) or by a person;
- the text read in a photo;
- its note;
- a video's scene descriptions and transcript (see [Videos](videos.md#search-inside-videos));
- the landmark and town it was at, when the Place names plug-in is on (see [Places](places.md#place-names-and-landmarks));
- the names of its file and folders.

How words match:

- With several words, a clip matching any of them is found. Clips matching more of them, or all of them in a row, come first.
- Quotes make a phrase: `"birthday cake"` finds only those words, in that order.
- Words joined by `_` or `.`, as in a file's name, are one thing, as if quoted: `IMG_7616.JPG` finds that file (and a photo of a screen that shows its name), not everything with "IMG" or "JPG" in it. The start of a name finds every file it starts: `DJI_20261025` finds that day's DJI clips.
- A date typed with hyphens finds a folder named so: `2026-10-25`.
- A word of three letters or more also matches the start of longer words: `disney` finds "Disneyland".
- Plurals and accents don't matter: `dogs` finds "dog", and `zurich` finds "Zürich".

Results are ranked, each rule deciding before the next:

1. A clip the search names comes first: its file name, with or without the extension (`DJI_20261025123119_0003_D`, `IMG_7616.JPG`), then the clips in a folder of that name (`Dockcase`).
2. Then the clips matching more of the search, a rare word counting for more than a common one: in `dundee beach`, a clip that says Dundee beats one that only says beach, and one that says both beats either.
3. Then by where it matched: your own words (a note, a description or tags you wrote) first, then where it was or who's in it for a search that names a place or a person, then its description, tags and scenes, the text read in it and what's said, a plug-in's facts, and its file and folder names last.
4. Then how well: the words together and in order, and whole words over the start of longer ones.
5. Then the newest first.

A search shows its 5,000 best matches at most.

### Best matches

A search shows its **Best matches** first, with how many there are: the clips it names, or that match everything you typed. A desktop shows one row of them, and **More** adds more, by how well they match. A phone shows two rows, the last tile saying how many more (**+192**), which shows them. Below them is everything the search found, day by day, in the page's sort: the best matches are among them too, and each says what found it. A clip opened from the best matches steps through them; one opened below steps through the days.

### Why a clip was found

A clip found by something other than its description says so in the corner of its thumbnail: **Text in photo**, **Note**, **Folder or file name**, **Said at 1:23**, **Scene at 0:42**, a plug-in's name (**Place names**), and for a search understood, **Near** a place or **Recognised** and a name. Hover over it for the words that matched, as `Text in photo: Dundee Diner Kitchen`; open it and **Found by** says the same at the top of the panel. A video found by a scene or by what's said in it opens at that moment.

### Who, where and what kind

A search also understands who, where and what kind of clip you ask for. `a video of Sue dancing at California Adventure` finds videos where Sue is recognised, taken within 5 km of California Adventure, that match "dancing":

- **Who**: a name of someone you've named on the People page, their first name, last name or both (`Horvick` finds every Horvick, and `the Tuttles` every Tuttle). Common short forms count both ways: `Sue` finds Susan, and `Bob` finds Robert.
- **Where**: the name of a place (with the Place names plug-in on) where some of your clips were taken. A clip within 5 km of it matches, wherever its GPS or its location guess put it. When places share a name, it's each of them you've been to, up to three, those with the most clips first: `Portland` finds your clips from Portland, Oregon and Portland, Maine, its card says **Portland (Oregon or Maine)**, and each clip's tag says which.
- **What kind**: `video`, `videos`, `photo`, `pictures` and the like.

Each counts by its words too, as before: `Sue` also finds a clip whose note says "Sue". Everything you ask for must match, and the rest of the words rank the results.

Under the box, **Searched for** shows what the search understood, a card each. Click × on a card to search those words as plain words: the search puts them in quotes, and quoted words are never understood. On a public page a search reads only the words.

### AI-assisted search

A search is local unless you ask for help: free, and no AI machine is asked anything. Where an admin has turned on **Let searches use the machine that describes photos** (Settings › AI › Search), an **AI** switch shows beside the search box. With it on, a word that names a kind of thing is also searched with the particular things of that kind, as your AI machine lists them: `snacks` finds a scene described as "a basket of tortilla chips" or "a bag of popcorn". **Searched for** shows them as a card, `snacks: popcorn, chips, …`; click its × to search the word alone.

Each word is asked about once and the answer kept, so a word costs one request to the machine, ever. A word that's already particular (`churro`) is kept as it is. If the machine is busy or off, the search goes ahead with your words. Your browser remembers the switch for new searches, and the page's address keeps it with the search.

Type two letters of a person's name and pick them from the **People** list under the box to see only their clips.

## Filters and sorting

Beside the search box:

- **All**, **Photos** or **Videos**.
- The sort list: **Date Taken** (the default), **Date Added**, **Last Modified**, **File Size**, **ISO**, **Aperture**, **Focal Length** or **Filename**, with the arrow beside it for ascending or descending. Sorting by a date groups clips by that date.
- The calendar button: **Today**, **Yesterday**, **This week**, **This month**, **This year**, or **Custom** for a range of dates taken.
- **Filters**: **Camera** and **Model**, **Lens**, ranges for **ISO**, **Aperture (f/)** and **Focal (mm)**, **Has location**, **Includes guesses** (see [Places](places.md)), **Has faces**, **Person...**, and **Rating** (**Favorites**, **Stars** and **Color**). Each shows once there's something to pick.

Each filter shows as a chip beside the box. Click a chip to remove it, or **Clear filters** to remove them all. Clicking in the viewer filters too: a tag, the date taken, the camera, the lens, a shutter speed, aperture, ISO or focal length.

The page's address holds the search and filters: bookmark it to come back to them.

## Saved searches

In **All photos**, with a search or filter set, click **Save search**, give it a name and click **Save**. It appears in the sidebar under Favorites. Hover over it and click ⋯ to **Rename** or **Delete** it. Only you see your saved searches.

To keep the clips a search finds today as a fixed list, [save it as a project](ratings-projects-sharing.md#save-a-search-as-a-project) instead.

## The viewer

Click a clip to open it. The panel beside it shows the file name and its folders, **Description**, **Text in Image**, **Tags**, a video's **Transcript** and **People**, your **Rating**, **Notes**, and **Details**: when it was taken, camera, lens, exposure, size and **Location**. Below them, as your role allows: **Add to project**, **Archive**, **Move to trash**, **Show faces** and, on a library's page, **Find similar**.

Editors and admins can click **Edit** beside the description, the text in a photo or the tags. What a person writes is marked **Edited** and the machine never writes over it; **Remove** lets the machine write its own again. They can also **Add note**: everyone who can see the clip reads it, with who wrote it and when.

Keys in the viewer:

| Key | What it does |
|---|---|
| ← and → | Previous and next clip |
| Esc | Close, or leave full screen |
| Shift+F | Full screen |
| Space | Start or stop a slideshow (3 seconds a clip; while it runs, a note at the top says Space stops it, again whenever you move the mouse) |
| f | Favorite on or off |
| 1 to 5 | Stars (the same number again clears them) |
| 0 | Clear the stars |
| 6 to 9 | Red, orange, yellow or green label (the same number again clears it) |
| Backtick | Clear the color label |
| d | Show or hide the faces in a photo |
| ? | Show or hide the key hints |

## Selecting several clips

Hover over a thumbnail and tick the box in its corner. While anything is selected, a click selects instead of opening, and Shift-click selects everything in between. Click a day's heading to select the whole day. The bar at the bottom sets a heart, stars or a color on all of them. Editors and admins also get **Add to project**, **Set location…** (see [Places](places.md#setting-a-location-by-hand)), **Archive** and **Move to trash**. **Clear** unselects.

## Similar photos and search by picture

Open a photo or a video from a library's page and click **Find similar** for up to 20 photos and videos in that library that look like it; those with the same named people rank higher. A video is compared by its scenes' pictures, so a video found opens at the scene most like what you started from, and a video finds what looks like any of its scenes. A video's scenes are compared once **Visual search in videos** has run on it (Settings › Processing).

The web app has no search by a picture of your own; the [command line](command-line.md) does:

```bash
lumiverb similar --library Family --image ~/Desktop/beach.jpg
```

## Archive and trash

Neither touches your files: they stay on disk where they are.

- **Archive** (editors and admins) keeps clips out of browse and search, with everything they have, until someone unarchives them. To archive a folder, click the number beside it in the folder list, then **Archive folder**. The **Archive** page lists them under **Archived by hand** and **File missing** (clips whose file disappeared: they come back by themselves when it returns). Select clips and click **Unarchive**.
- **Move to trash** (editors and admins) hides clips until they're deleted for good, 30 days later unless an admin changes it in **Settings › Files**. Clips in projects ask before they go. On the **Trash** page, select and click **Restore**; an admin can also **Delete for good** or **Empty trash**. A clip deleted for good comes back only if its file changes, moves or is renamed.

Both offer **Undo** briefly.

## Downloads

Lumiverb never hands out your original files: open them where they live. The web app downloads a video's [transcript](videos.md#transcripts) and a project's [file for an editing app](ratings-projects-sharing.md#send-to-an-editing-app). The command line saves a clip's preview image:

```bash
lumiverb download --library Family --path "2024/Beach/IMG_0412.jpg" --size preview --output ~/Desktop/
```

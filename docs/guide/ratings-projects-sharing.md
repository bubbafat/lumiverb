# Ratings, projects and sharing

Rate clips for yourself, gather the ones you want into projects to work on or send to an editing app, and share a project or a whole library with people who don't sign in.

## Ratings

There are three kinds, and you can use all three on a clip:

- **Favorite**: a heart.
- **Stars**: 1 to 5.
- **Color**: a red, orange, yellow, green, blue or purple label.

To rate one clip, hover over its thumbnail and click the heart in its corner, or open it and use the **Rating** section of the viewer. In the viewer, the keys are faster: **f** for favorite, **1** to **5** for stars, **0** to clear them, **6** to **9** for red, orange, yellow and green, and backtick to clear the color. Clicking the same star or color again clears it. To rate many at once, select them and use the heart, stars and colors in the bar at the bottom (see [Selecting several clips](browse-and-search.md#selecting-several-clips)).

Ratings are yours. Everyone signed in, viewers too, has their own, and nobody sees anyone else's. Your stars show in a thumbnail's corner and your color as a stripe along its bottom. Ratings don't exist on public pages.

## Finding what you rated

- **Favorites** in the sidebar shows everything you hearted, in every library you can see.
- **Filters** › **Rating** narrows any grid or search: **Favorites**; **Stars**, where clicking one star shows exactly that many and clicking a second makes a range; and **Color**.

A search with a rating filter can be kept with **Save search** (see [Saved searches](browse-and-search.md#saved-searches)).

## Projects

A project is a list of clips you choose, from any of your libraries: the picks for a client, a film's footage, a family album. Projects are for editors and admins; viewers don't have them. Clips in a project stay where they are in their libraries, and the files are never touched.

To make one:

- In **Projects**, click **New project**, type a name and, if you like, a description, and click **Create**.
- Or select clips, click **Add to project**, then **Create new project**, type a name and click **Create & add**.
- Or save a search as a project (see [Save a search as a project](#save-a-search-as-a-project)).

To add clips, select them, or open one in the viewer, and click **Add to project**, then the project. Type in **Search projects...** to find it in a long list. You add clips to your own projects.

To take clips out, open the project, select them and click **Remove from project**.

A project's page shows its clips under a heading for each day, with **Export** and **Settings** at the top. In **Settings**, the project's owner changes its **Name**, **Description**, **Sort order** (**Manual**, **Date added** or **Date taken**) and **Visibility**, or clicks **Move to trash**. The sort order is the order an export lists the clips in.

The **Projects** page has three tabs. **Active** shows your projects and those shared with you, each with its first clip as a cover; one someone else owns says **Shared**, and a public one **Public**. Hover over one of yours for **Archive**, which moves it to **Archived** (still readable and exportable; **Restore** brings it back), or **Delete**, which moves it to **Trash** with **Undo**. In **Trash**, click **Restore**, **Delete forever** or **Empty trash**. A project in the trash is deleted for good after the account's trash days (30 unless an admin changes them in **Settings › Files**).

When some of a project's clips are in the trash, archived or missing from disk, its page says so and leaves them out. **Restore them** brings back clips someone moved to the trash.

## Save a search as a project

On a library's page or in **All photos**, with a search, a filter or a folder chosen, click **Save as project** beside the filters (editors and admins), type a name and click **Save**. The project holds the clips the search finds now. It doesn't change as new clips arrive; to keep the search itself, use **Save search**.

A search that finds too many is refused: more than 10,000 clips, or more than 5,000 for a search with words. Narrow it first.

## Send to an editing app

On a project's page, click **Export**. The first time, **Send to editor** asks for a **Format**:

- **DaVinci Resolve / Premiere Pro** (an `.xml` file)
- **Final Cut Pro** (an `.fcpxml` file)

Tick **Make default** to skip the question next time in this browser, and click **Export file**. The arrow beside **Export** asks again.

The file points at your original files by their paths on the server. If the editing app runs on another computer where those paths differ, relink the clips there. Photos come in as stills. Clips in the trash, archived or missing from disk, and videos whose length isn't known, are left out, and Lumiverb says how many.

## Who sees a project

| Visibility | Who sees it |
|---|---|
| **Private (only you)** | You. New projects start private. |
| **Shared (all team members)** | Every editor and admin in your account who can see at least one of its clips. They can open and export it, not change it. |
| **Public (anyone with link)** | Anyone with its link, without signing in. |

Only an admin can make a project public. Its owner can take it back to private or shared, which ends the link.

A project's clips show only to people who can see their library, and a public project's visitors see only the clips its owner can still see. See [Users and roles](users-and-roles.md).

## Public links

**A public project.** Once a project is public, its page for visitors is its own address with `/public` in front of `/projects`. If you open it at `https://photos.example.com/projects/1a2b3c`, share `https://photos.example.com/public/projects/1a2b3c`. Moving the project to the trash stops the link until it's restored.

**A public library.** On the **Libraries** page (**Manage libraries** in the sidebar), an admin clicks **Not public** beside a library and confirms to make it **Public**. **Copy link** then copies the address to share. Click **Public** to stop sharing it.

Visitors must be able to reach your server: see [Network, phones and HTTPS](network.md).

## What a public page shows

Visitors see:

- a project's name, description and how many items it has; or a library's grid, which they can search (a limited number of times a minute), narrow to photos or videos and to dates, sorted by date taken;
- each photo, and each video's first 10 seconds unless an admin changes that in **Settings › Playback**;
- descriptions, tags, the text in photos, and a video's transcript as far as it plays.

They never see file names or folders, locations, camera details, notes, faces or people, or ratings, and they can't change anything.

# People and faces

Lumiverb finds the faces in your photos and videos and groups the ones nobody has named yet, so you can name a whole group at once. Each person you name gets a page with every photo and video they're in.

## How faces are found

Faces are found on your server after a clip is scanned (or on a [second machine](ai-worker.md), if you set one up), with a face model Lumiverb downloads once. Your photos aren't sent anywhere. A video's faces are found in frames of its analysis copy, after its scenes (see [Videos](videos.md)).

How hard Lumiverb looks is a level, one for photos and one for videos. An admin sets them in **Settings › Processing**, in the **Faces** row (**Photos** and **Videos**), or in the setup wizard's **What to find** step.

| Level | Photos | Videos |
|---|---|---|
| Off | No new faces are found. The faces already found, and their names, stay. | The same |
| Low | Only clear faces near the camera | One frame from each scene (the default) |
| Normal | The default | Three frames from each scene |
| High | Small faces in crowds too, and more things that aren't faces | Each scene's frame and one every 3 seconds; slower |

A new level applies to clips found from then on. After raising one, the Faces row says how many clips were looked at with a lower one, and an admin can click **Redo** to look at them again. Faces someone named, or said aren't a certain person, are kept through a redo.

## Naming groups of faces

Click **People** in the sidebar. Named people come first. Below them, **Unnamed clusters** holds faces that look alike, grouped. Each card shows a few faces and how many there are; click the count to see them all.

Editors and admins name a group from its card:

- **Name all** (with the group's count) names every face in it: type the name and click **Save**.
- **All … are...** (with the count) adds them to someone already named: pick from the people they look most like, or type to search by name.
- **Dismiss** hides the group for good, for faces you don't want to name (strangers, posters). **Undo** appears for a few seconds.

A group can mix two people. Click a single face to open its photo or video with **Who is this?** already open for that face (in a photo, its box is red). Once you name it, the viewer moves on to the next face in the group.

Above the cards, sort the groups (**Largest first**, **Smallest first** or **Newest**) and use **Min faces** to hide small ones. Groups are worked out again within a few minutes of faces changing, and the heading says **Updating** meanwhile. After naming several, click **Refresh clusters** to see the new groups. **Merge close groups**, in the Faces row of Settings › Processing, joins groups close enough to be one person; it's on unless an admin turns it off.

Once someone is named, Lumiverb names new faces that look very like them by itself, and keeps doing so as you name more.

## Naming a face in a photo

In the viewer, click **Show faces**, or press **d**. Each face gets a box: white when no one has named it, green with the name when someone has, gray when it was dismissed. Editors and admins click a box:

- For an unnamed face, **Who is this?** lists the people it looks most like. Pick one, type in **Search by name...**, or click **+ New person**, type the name and click **Create**.
- For a named face: **View person**, **Remove tag**, or a name under **Change to:**. A tag you remove isn't put back by Lumiverb.

For a viewer, clicking a named face opens that person's page.

## People in a video

A video gets no boxes. Its **People** section in the viewer lists one face for each person seen, with the times they're on screen, such as `0:12–0:40`. Click a time to jump there; this works once the whole video is ready to play (see [Playing a video](videos.md#playing-a-video)). Editors and admins click **Who is this?**, or the name, to name or change it as for a photo.

## A person's page

Click a person on the People page. Their page shows each of their photos and videos once. A video's tile shows their face and **▶ Video**, and under it the first few times they're seen; click a time to play from there.

Editors and admins can:

- click the name to rename them;
- click **Merge into...** and pick who this person really is: all their faces move there, and this person goes;
- click **Delete person**: the name goes, and their faces go back to unnamed groups.

Merging and deleting take every face the person has, in every library. If some of their faces are in libraries you can see but don't edit, the page says how many, and only an account admin can merge or delete them. The same goes for restoring someone dismissed.

## Dismissed faces

**Dismissed**, at the top right of the People page, lists the groups dismissed. An editor or admin clicks **Restore & Name**, then types a new name and clicks **Restore**, or picks a person already named to add them to.

## Finding someone's photos

- In the search box, type two letters of their name and pick them from **People**.
- Or use **Filters** › **Person...**.
- **Filters** › **Has faces** shows every clip with a face found.

Each adds a filter you can combine with a search or other filters (see [Browse and search](browse-and-search.md#filters-and-sorting)).

## Who can do what

- Everyone signed in sees the People page, but only faces in the libraries they can see: someone with no faces there isn't listed (see [Users and roles](users-and-roles.md)).
- Editors and admins name, change, merge, dismiss and delete. Naming a group names only the faces in libraries they edit; merging, deleting and restoring a dismissed person are an account admin's when the person has faces in libraries they don't edit.
- Faces and names never show on public pages.

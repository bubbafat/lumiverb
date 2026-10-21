# Places

Lumiverb knows where a photo or video was taken from the GPS in the file, from a location a person set, or from a guess made from clips taken around the same time. With the Place names plug-in on, it also names the nearest town and the landmark it was at.

## Where a location comes from

A clip has at most one location shown, chosen in this order:

| Kind | Where it comes from | What the viewer shows |
|---|---|---|
| Set by a person | An editor or admin chose it | The point, then "Set by" and who; the file's own GPS, if any, on a line of its own ("From the file") |
| From the file | The GPS the camera or phone wrote | The point |
| A guess | Worked out from other clips (see [Location guesses](#location-guesses)) | The point and a **Guess** label |

A location from the file is read when the file is scanned. A position of exactly 0, 0 counts as no location: some devices write it when they have no fix.

## Seeing a clip's location

Open a clip and look at **Location** under **Details**. The coordinates are a link that opens the spot in Google Maps in a new tab. For a guess, click **Guess** to see how sure it is and why, for example "About 2 km · From iPhone 15 Pro photos 14:02–14:40".

Lumiverb has no map view of its own.

## Photos nearby and Has location

- **Photos nearby**, under a clip's location in the viewer, shows every clip within 1 km of it. A "Within 1 km" chip appears beside the search box: choose another distance on the chip itself, from 0.2 to 500 km, or clear it with its **x**.
- **Filters** › **Has location** shows only clips with a location from the file or a person.
- **Filters** › **Includes guesses** counts guesses too, for both. It appears once there are guesses.

## Setting a location by hand

Editors and admins can set the location of any clips:

1. Select the clips: tick the box in a thumbnail's corner (see [Selecting several clips](browse-and-search.md#selecting-several-clips)).
2. Click **Set location…** in the bar at the bottom.
3. Choose how:
   - **Place**: type two or more letters of a town or landmark and pick it from the list. This needs a plug-in that finds places, such as Place names (see below); without one, the dialog says so.
   - **Coordinates**: type latitude and longitude, such as `48.85837, 2.29448`.
   - **Same place as**: with several clips selected, pick the one whose location the others take.
4. Click **Set location**.

If some of the clips already have a location, from the file or a person, Lumiverb asks first: click **Replace** (it says how many) to go ahead, or **Back**. A file's own GPS stays visible under the new location.

Lumiverb never changes a location a person set, and where guesses are on, it helps place the clips taken around it. To undo it, click **Clear** beside "Set by" in the viewer: the clip goes back to its file's location, or a guess.

## Location guesses

Many cameras have no GPS, but a phone in the same pocket does. With **Infer location** on, Lumiverb places a clip that has no location from the clips in the same library that have one and were taken shortly before or after it, on any device. It corrects for a camera clock that's off (still on home time, say) by matching photos the camera and the phone took of the same scene. With a located clip on each side it places the clip between them, otherwise where the nearest one was, and says how sure it is: "About 100 m", or "About 20 km" when the nearest is hours away.

A guess never replaces a location from the file or one a person set, and it shows as a guess. When the clips it came from change or go, Lumiverb checks it again.

**Infer location** is each library's, and off unless turned on: tick it when adding the library, or later under **Location guesses** on the library's settings page (**Manage libraries**, then the library's **Settings**). Only the library's admin can change it. Turning it off removes that library's guesses after asking (**Remove guesses** or **Keep it on**); locations people set stay.

How far before or after a clip Lumiverb looks is the account's **Inference window**: 360 minutes unless an admin changes it in the **Location guesses** row of **Settings › Processing**.

## Place names and landmarks

**Place names (GeoNames)** is a plug-in that comes with Lumiverb, off unless an admin turns it on in **Settings › Plug-ins**. Turning it on shows GeoNames' license (Creative Commons Attribution 4.0) to accept with **OK**. The server then downloads GeoNames' lists once, about 600 MB, and builds its own list of the world's towns of 1,000 people or more and its landmarks; Settings › Plug-ins shows how far along it is. Nothing about your photos is sent to GeoNames.

Once it's ready:

- Each clip with a location, a guess included, gets a **Place names (GeoNames)** section in the viewer: **At**, the landmark it was at, and **Near**, the nearest town (or "Not near a town").
- Search finds clips by those names: `"Magic Kingdom"`, say.
- **Set location…** finds places by name.

How far away a town can be and still count is **Nearest town within**, 25 km unless an admin changes it in Settings › Plug-ins. See [Plug-ins](plugins.md) for the rest.

## Who sees locations

Everyone signed in sees the locations of the clips they can see. Public pages never show a location of any kind, and their visitors can't filter by one.

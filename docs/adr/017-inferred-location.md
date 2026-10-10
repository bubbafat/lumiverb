# ADR-017: Inferred Location

## Status

Accepted. Decisions were made with Robert on 2026-10-09. Phases 1 to 3 built.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Groundwork: better EXIF, the `asset_location` table, the effective location in filters, nothing on public pages | Built. New and changed files only: backfilling clips already ingested moves to phase 3 |
| 2 | A person's location: set it for explicit clips; Lightbox row | Built |
| 3 | The `location` producer: camera clips located from phone photos by date and time; backfill `taken_at_offset_min` and `gps_accuracy_m` for clips already ingested | Built |
| 4 | Offline place names and suggestions from text in the image | Not started |
| 5 | Landmark suggestions through the vision job | Not started |

## Overview

Many clips have no GPS, for example:
- clips from a camera with no GPS (a Blackmagic or a mirrorless body);
- scans and screen recordings;
- files whose metadata was stripped.

The same shoot nearly always has phone photos that do have GPS, often in a sibling folder. Take `Carnival/BRoll/Phone` and `Carnival/BRoll/BMC`: same day, two devices, one place. Lumiverb can place the camera clips from the phone photos taken around the same time.

A location is one of three kinds, kept apart:
- **from the file**: what the camera wrote;
- **set by a person**;
- **a guess**: inferred, with a radius and a reason.

A guess never overwrites the other two kinds. Location of any kind never leaves on a public request.

## Motivation

- "Has location" and "Photos nearby" miss every camera clip, even when the phone photos from the same minute have GPS.
- A person cannot set a location today. The scan overwrites `gps_lat`/`gps_lon` on every pass (`tenant.py` `update_exif`), so a hand-set value would be lost on the next scan.
- Some devices write (0, 0) when they have no fix, and it is accepted as a real location.

## Decisions (with Robert, 2026-10-09)

| # | Question | Decision |
|---|----------|----------|
| 1 | Can a person's location override GPS from the file? | Yes. The file's value stays visible as "From the file". |
| 2 | Do guesses count in "Has location" and "near"? | Not by default. A separate "Includes guesses" toggle adds them. |
| 3 | Can text or landmark results apply themselves? | No. They are suggestions until a person picks "Use this". |
| 4 | Can a guess be made from other guesses? | No. Only GPS from the file and a person's locations are fixes. |
| 5 | How are guesses gated and bounded? | Two settings: **Infer location**, off by default, and **Inference window**, 360 minutes by default. Cross-device matching (phone to camera) by date and time is the main signal and is not limited to one folder. |
| 6 | Where do place names come from? | An offline gazetteer (GeoNames) on the brain. Nothing leaves the box. |
| 7 | Does a person's location spread to neighbours? | Yes. A person's location is a fix, so nearby clips get guesses from it. |
| 8 | How are bulk location actions targeted? | Only by an explicit list of clips. Replacing an existing location needs a 409 decision. |
| 9 | Are place names searchable as text? | Later, signed-in only, and hidden on public pages. |
| 10 | Is a map view in scope? | Not now. |

## Design

### Data Model

`assets.gps_lat` / `gps_lon` keep meaning "what the file says". Only the scan writes them.

Scan changes:
- (0, 0) is rejected as no fix.
- `OffsetTimeOriginal` (or `OffsetTime`) is stored as `taken_at_offset_min`, an integer, NULL when absent.
- `GPSHPositioningError` is stored as `gps_accuracy_m`.
- `taken_at` is the camera's wall clock as the file writes it, stored as if it were UTC, by every scanner (phase 3). A zone written in the date itself (a phone video's `CreationDate`, "…+02:00") isn't applied: it's `taken_at_offset_min`. So the instant is `taken_at` minus `taken_at_offset_min` when the file says, and unknown otherwise. Before phase 3 the Python scan applied a zone written in the date and the Mac scanner read the wall clock in the Mac's own zone; the `capture` producer reads every such clip again.

The new tenant table `asset_location` has one row per clip, holding only what the file doesn't say:

```sql
CREATE TABLE asset_location (
  asset_id    TEXT PRIMARY KEY REFERENCES assets(asset_id) ON DELETE CASCADE,
  lat         DOUBLE PRECISION NOT NULL,
  lon         DOUBLE PRECISION NOT NULL,
  radius_m    INTEGER NOT NULL,          -- 0 for a person's exact point
  source      TEXT NOT NULL,             -- person | time | suggestion
  status      TEXT NOT NULL,             -- applied | suggested
  basis       JSONB NOT NULL,            -- fix asset ids and times, clock offset used, OCR snippet, gazetteer id
  set_by      TEXT,                      -- user id, for source = person
  set_at      TIMESTAMPTZ NOT NULL
);
```

Lineage:
- **A guess** has an `artifact_lineage` row with artifact `location`, producer `location`, version and settings hash. `outcome = 'empty'` means "tried, nothing within the window", so the clip isn't due forever.
- **A person's location** records producer `person` (`src/shared/producers.py`). It is never regenerated over. Clearing it lets the machine guess again (ADR-016).

**Which location shows** (the effective location):
1. a person's;
2. then the file's;
3. then an applied guess, only when guesses are included.

Suggestions never count as a location.

The effective location is computed in SQL with a `LEFT JOIN asset_location` inside the `has_gps` / `near` filters and facets. It is not copied onto `assets`, which avoids rebuilding the `active_assets` view.

### The `location` producer (phase 3)

Settings, declared on the producer so lineage tracks them:

| Setting | Default | Meaning |
|---|---|---|
| `infer_location` | `false` | Off: nothing is guessed and existing guesses are removed. |
| `inference_minutes` | `360` | A clip takes a location from fixes up to this far before or after it. |

These live in Settings → Processing → Location. Turning Infer location off goes through the usual counted 409; a new Inference window checks guesses again (see "As built").

The producer:
- `media = ALL`, `storage = False` (it never reads originals);
- CPU pool, tier FIND, large batches;
- `applies`: the clip has no file GPS, no person location, a `taken_at`, and `infer_location` is on.

**Fixes** are clips with file GPS or a person's location. Guesses are never fixes.

**Devices** are keyed by make + model + serial number, falling back to make + model.

**Clock correction**, run per device per day before matching:
- Camera clocks are often on home time, never changed for daylight saving, or drifting.
- Candidate pairs are a camera clip and a fix from another device that look like the same scene (CLIP cosine similarity above a threshold), within ±14 h of each other.
- Each pair's time gap is one vote for the device's clock offset.
- With at least 3 pairs agreeing within 10 minutes, their median is the offset for that device and day. Otherwise the offset is 0, or the difference between `taken_at_offset_min` values when both sides record one.
- The offset used goes in `basis`.

**Matching:**
- Use the corrected camera time.
- Find the nearest fixes before and after it, from any device, within `inference_minutes`.
- **Fixes on both sides:**
  - Check the implied speed between the two fixes. Above 900 km/h, skip (a clock or data error).
  - Otherwise interpolate by time.
  - `radius_m` = half the distance between the fixes + 100 m.
- **A fix on one side only:** copy it. `radius_m` = 100 m + 4 km/h × gap. A 5-hour gap shows as "About 20 km".
- **No fix in the window:** lineage `empty`.

The UI shows the radius and the reason, e.g. "About 2 km · from phone photos 14:02–14:40".

**Re-running when neighbours change:**
- The reconciler only sees a clip's own changes. So when a clip gains, loses or moves a fix, the guesses near it and made from it are checked again (see "As built": a guess is never removed to be made again).
- Person rows are never touched this way.

### API Endpoints

- `PUT /v1/assets/locations`, editor or above. Body:
  - `asset_ids` (required, non-empty; a missing or empty list is a 400, never "all");
  - one of `{lat, lon}`, `{same_as: asset_id}` or (phase 4) `{place_id}`;
  - `replace`: `"none" | "person" | "all"`.

  If any target already has a file or person location and `replace` doesn't cover it, the answer is 409 `location_exists` with counts.
- `DELETE /v1/assets/locations`, editor or above. Body: `asset_ids`, required. It clears person locations only.
- `POST /v1/assets/locations/accept`, editor or above. Body: `asset_ids`, required. It turns suggestions into person locations.
- Asset detail adds `location: {lat, lon, radius_m, source, status, basis_summary} | null` beside the file GPS, signed-in only.
- Filters:
  - `has_gps` and `near` count the file and person locations;
  - `include_guesses=true` adds applied guesses;
  - suggestions never match.

### Privacy

- No location of any kind is sent on a public request: file, person, guess, suggestion, place name or `basis`. Same rule as GPS (PR #71).
- Location filters and location facets are refused or omitted on public requests (PR #71 covers `near` / `has_gps`; new filters must be added to the same guard).
- Place names, when they become searchable (phase 4+), are signed-in only, hidden from public search the way notes are.

### UI

The Lightbox has a **Location** row:

| Kind | Shows |
|---|---|
| From the file | Coordinates (as today) |
| A person's | Coordinates · "Set by <name>". If the file has GPS too, a second line: "From the file: …" |
| Guess | "About 2 km" · a **Guess** chip, with the reason on hover |
| Suggestion | "Maybe: <place>" · **Use this** / **No** |

Other UI:
- **Selection:** "Set location…" (coordinates, or "same place as <clip>").
- **Filter bar:** "Has location" plus an "Includes guesses" toggle.
- Copy stays terse. Times are estimates, never promises.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| The camera is on home time while travelling | The clock offset from matching phone and camera scenes corrects it. Without pairs, it uses the recorded offsets if both have them, else the times as written. |
| The phone was off for part of the day | No fix within the window: no guess. |
| A flight between two fixes | Over 900 km/h: skipped. |
| A person sets a location on a clip with file GPS | The person's location wins; the file value stays visible. |
| A person clears their location | The row goes; the machine may guess again if `infer_location` is on. |
| A neighbour gains GPS on a rescan | Neighbours' guesses are checked again: replaced only by a surer one. |
| A fix a guess was made from is trashed, with no other fix near | The guess stays, marked "source removed". |
| `infer_location` turned off | Guesses are removed. Person locations stay. |
| (0, 0) in EXIF | Treated as no GPS. |
| A public page | No location at all; location filters are refused. |
| A bulk set with no `asset_ids` | 400. |

## Code References

| Area | File | Notes |
|------|------|-------|
| EXIF parsing | `src/client/workers/exif_extract.py` | `parse_gps`, `parse_taken_at` |
| Scan writes GPS | `src/server/repository/tenant.py` `update_exif` | Overwrites file GPS on each scan |
| Filters | `src/server/models/query_filter.py` | `HasGps`, `NearLocation` |
| Facets | `src/server/api/routers/facets.py` | `gps_count` |
| Public guards | `src/server/api/routers/query.py` `guard_public_spec`, `assets.py` | |
| Producer contract | `src/producers/contract.py` | |
| Lineage | `src/server/repository/lineage.py` | |
| Similarity | `tenant.py` `find_similar` | For clock-offset pairs |
| Lightbox | `src/ui/web/src/components/Lightbox.tsx` | GPS row |

## Doc References

- `docs/cursor-api.md`: the location endpoints and filter changes
- `docs/architecture.md`: the location producer
- `docs/adr/016-operating-model.md`: human data never overwritten (principle 3); decisions in the request (principle 9)

## Build Phases

Requirements as in the ADR template: the full suite passes, tsc and vite build are clean, docs are updated, and the progress table is updated.

### Phase 1: Groundwork
- EXIF: reject (0, 0); store `taken_at_offset_min` and `gps_accuracy_m` (scan, ingest, Mac scanner).
- The `asset_location` table and model.
- The effective location in `has_gps` / `near` / facets, with `include_guesses`.
- Public guards cover any new location field and filter.

### Phase 2: A person's location
- `PUT` / `DELETE /v1/assets/locations`, with explicit ids and the 409.
- Lightbox Location row; "Set location…" on a selection.

### Phase 3: The `location` producer
- Backfill `taken_at_offset_min` and `gps_accuracy_m` from the originals for clips ingested before phase 1, in the scheduler (read-only storage), on the producer structure that replaces today's. Phase 1 extracts them only for new and changed files. (0, 0) was already cleared by the phase 1 migration.
- The `infer_location` and `inference_minutes` settings.
- Device keys, clock correction, matching, radius, the speed check.
- The re-run step when a fix changes; a person's location acts as a fix.
- The "Includes guesses" toggle in the filter bar shows by itself once guesses exist (built in phase 2: `guess_count` in the facets).

**As built** (where it differs from the design above, or settles what it left open):
- **Capture facts are a producer of their own** (`capture`, exiftool, its own pool, tier 2, reads the originals), not part of probe: probe is video only and one ffprobe pass; these are every clip's EXIF. The Python scan sends them and says so (`capture` lineage on the ingest); an ingest that doesn't (the macOS app) leaves them for the producer, which also reads that clip's `taken_at` again in the one meaning. `location` needs `capture`, so a clip is guessed only once its own time is read the one way.
- **Fixes come from the clip's library.** Matching isn't limited to a folder; it is limited to the library, as the re-run step is.
- **Rechecks, not remakes** (Robert, 2026-10-10). Data is better than no data, and a guess's confidence (`radius_m`, smaller is surer) only improves. A guess is never removed to be made again: it's marked due (`asset_location.recheck`, the reason; the producer's `made` is false while it's set) and stays until the producer saves what it finds now (`repository/locations.py` `save_guess`). A clip with no guess (tried, nothing found) is simply due again, its lineage gone. A guess is checked again only when:
  1. a fix it was made from (its `basis`) is trashed, archived or found missing, or changes: its GPS or a person's location on it moves or is cleared, or its capture time changes. Reason `basis_gone` for the first three, `basis_changed` for the rest;
  2. a new fix appears near it (every clip in sight in its library within `inference_minutes` + 14 h, the clock correction's reach): GPS from a scan, a person setting or accepting a location, or a fix restored, unarchived or found again. Reason `new_fix`;
  3. the Inference window changes. Reason `window_changed`, or `basis_changed` for a guess made from a fix now outside it.

  A stronger reason replaces a weaker one while it waits (`basis_changed` > `basis_gone` > the others). What the save does:
  - `new_fix`, `window_changed`: the new guess replaces the old only when `radius_m` is no larger; otherwise the old stays as it was ("unchanged");
  - `basis_changed`: the old one is wrong: replaced, or removed when nothing is found;
  - `basis_gone`: the new guess, however sure; with none, the old stays, marked `basis.basis_gone` ("marked"). The Lightbox's Guess details end " · source removed". Restoring the fix is a new fix, and the guess made from it replaces the marked one (as sure), unmarked.

  Triggers: a scan's `update_exif` and the capture facts read again (`file_changed`; a clip without a fix whose own time changes is `basis_changed` too: its guess was worked out for the old time), a person setting, clearing or accepting a location, and the asset repository's trash, archive and restore (`trash_many`, `archive`, `archive_folder`, `_bring_back`, `clear_trash`). A library in the trash takes all its clips, fixes and guesses alike, so it triggers nothing. Nothing else rechecks a guess, and a person's location is never touched.
- **The Inference window doesn't remake** (`remakes=False`, not in lineage). A change used to make every guess stale and redo it; now it's the producer's `regroup` (`locations.window_changed`, called with the old and new values), which marks the rechecks above and, for a wider window, makes clips where nothing was found due again. Nothing is removed and nothing is asked. The producer sends the window it worked in with each save, and a save from a window that's no longer the setting is refused ("stale": nothing written, the scheduler reads its settings again). The migration (`c4e8a1d7b3f9`) rewrote existing lineage to the new hash, so the change redid nothing.
- **A new producer version's redo** (stale lineage, no recheck reason) still replaces or removes guesses as made: that's new code, not one of the triggers.
- **Clock pairs** are each camera clip's single most alike fix from another device (one vote per clip), at CLIP cosine similarity ≥ 0.9, at most 300 of the device's clips a day. Only photos have CLIP vectors, so a camera that shoots only video gets no votes: its offset is the recorded zones' difference, or none.
- **Gaps** are between real instants when both clips record a zone, otherwise between wall clocks as written (the ADR's offset 0); the clock offset applies only to fixes from another device.
- **Radius** adds the larger GPS accuracy (`gps_accuracy_m`) of the fixes used.
- **A guess is never written over a suggestion** (phase 4), as it isn't over a person's or the file's.
- **Turning Infer location off** asks first (409 `redo_on_change`, "New settings remove location guesses from N clips"), then removes every guess and the producer's lineage; a person's location stays. It's the spec's `on_settings`, a new field of the producer contract.

### Phase 4: Place names
- GeoNames offline on the brain.
- Place search when setting a location.
- OCR place hints as suggestions (accepted only when the text appears in `asset_ocr`).

### Phase 5: Landmarks
- A separate structured vision question with "null" as the default answer.
- Suggestions only.
- `VISION_PROMPT` stays unchanged.

## Alternatives Considered

- **Same-folder-only matching.** Rejected: shoots are split across folders by device (`Phone` / `BMC`).
- **General CLIP similarity or faces as a location signal.** Rejected: too weak. CLIP is used only to pair scenes for clock correction.
- **An online reverse-geocoding service.** Rejected: nothing leaves the box.
- **Copying the effective location onto `assets`.** Rejected: it means rebuilding the `active_assets` view; the join is cheap.

## What This Does NOT Include

- A map view.
- Place names as searchable text.
- Correcting camera clocks in the stored `taken_at`. The offset is only used for matching.

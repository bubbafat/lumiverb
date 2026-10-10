# ADR-017: Inferred Location

## Status

Accepted. Decisions were made with Robert on 2026-10-09. Phases 1 and 2 built.

## Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Groundwork: better EXIF, the `asset_location` table, the effective location in filters, nothing on public pages | Built. New and changed files only: backfilling clips already ingested moves to phase 3 |
| 2 | A person's location: set it for explicit clips; Lightbox row | Built |
| 3 | The `location` producer: camera clips located from phone photos by date and time; backfill `taken_at_offset_min` and `gps_accuracy_m` for clips already ingested | Not started |
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

These live in Settings → Processing → Location. Changing either one goes through the usual counted 409 and redo.

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
- The reconciler only sees a clip's own changes. So when a clip gains or loses a fix (file GPS appears, or a person sets or clears a location), the guess and lineage rows go for clips on the same day within `inference_minutes` of it, in the same library.
- Person rows are never removed this way.
- The reconciler then sees those clips as missing and guesses again. Guesses are cheap and derived, so deleting them is safe.

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
| A neighbour gains GPS on a rescan | Neighbours' guesses are removed and remade. |
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

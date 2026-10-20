"""Capture facts (ADR-017): when the file says a clip was taken, the zone it
was taken in, and how precise its GPS is (taken_at, taken_at_offset_min,
gps_accuracy_m on assets), read from the original with exiftool.

The scan sends them with the EXIF, and says so (its lineage), so new and
changed files have them. This producer reads them for the rest: clips
scanned before ADR-017 phase 1, and clips a scanner sent without saying
(the macOS app), whose taken_at it reads again so every clip's is the
camera's wall clock (src/processing/workers/exif_extract.py parse_taken_at).
Nothing about location is worked out here."""

from src.producers import names
from src.producers.contract import ALL, PREPARE, Data, Field, Pool, ProducerSpec

# One exiftool at a time, a job's files read in one go; apart from the probes
# so a backfill of the whole library doesn't hold up new videos' probes.
EXIF = Pool("exif")

MAKES = Data(
    names.CAPTURE_TIME, "When it was taken, in UTC, and its time zone's offset when known, as the file says.",
    (Field("utc", "time"), Field("offset_min", "number")),
    stored=("assets.taken_at", "assets.taken_at_offset_min"),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ORIGINAL,),
    artifact=names.CAPTURE_TIME, version="1", media=ALL, title="Capture time and GPS accuracy", order=15,
    applies="true",
    # Made once its lineage says so: the facts themselves can be empty (a PNG says nothing).
    made=("EXISTS (SELECT 1 FROM artifact_lineage cl WHERE cl.asset_id = a.asset_id"
          f" AND cl.artifact = '{names.CAPTURE_TIME}' AND cl.producer <> '')"),
    # Its time read again, the clip's guess is made again with it.
    redo_also=(names.LOCATION_GUESS,),
    run="src.producers.capture.work:Capture", tier=PREPARE, pool=EXIF,
    batch=50, storage=True,
)

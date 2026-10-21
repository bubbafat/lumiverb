"""Live Photos: Apple's identifier of the Live Photo a clip is part of, the same in an iPhone's photo and the
few seconds of video beside it (assets.live_photo_id), read from the original with exiftool.

The scan sends it with the EXIF, and says so (its lineage), so new and changed files have it. This producer
reads it for the rest, clips scanned before, but only where it can pair anything: an Apple camera's clip
beside a file of the other kind with the same name (IMG_0255.HEIC and IMG_0255.MOV), so it reads hundreds
of files, not every iPhone photo. Pairing is the server's, by the identifier alone
(src/server/repository/live_photos.py): a video a photo shares it with is that photo's motion."""

from src.producers import names
from src.producers.capture import EXIF
from src.producers.contract import ALL, PREPARE, Data, Field, ProducerSpec
from src.shared.live_photos import stem

MAKES = Data(
    names.LIVE_PHOTO, "Which Live Photo it's part of: Apple's identifier, the same in the photo and in the few "
    "seconds of video beside it, which plays from the photo instead of showing on its own.",
    (Field("id", "text"),),
    stored=("assets.live_photo_id",),
)

# Beside a clip of the other kind with its name (ix_assets_stem finds them).
_BESIDE_ITS_OTHER_HALF = (
    "a.camera_make = 'Apple' AND EXISTS (SELECT 1 FROM assets s WHERE s.library_id = a.library_id"
    f" AND {stem('s')} = {stem('a')} AND s.media_type <> a.media_type AND s.deleted_at IS NULL)"
)

PRODUCER = ProducerSpec(
    # Its camera's make, and its path beside the paths of its library's other clips.
    makes=MAKES, reads=(names.ORIGINAL, names.METADATA), reads_others=(names.ORIGINAL,),
    artifact=names.LIVE_PHOTO, version="1", media=ALL, title="Live Photos", order=16,
    applies=_BESIDE_ITS_OTHER_HALF,
    # Made once its lineage says so: most clips have no identifier.
    made=("EXISTS (SELECT 1 FROM artifact_lineage cl WHERE cl.asset_id = a.asset_id"
          f" AND cl.artifact = '{names.LIVE_PHOTO}' AND cl.producer <> '')"),
    run="src.producers.live_photo.work:LivePhoto", tier=PREPARE, pool=EXIF,
    batch=50, storage=True,
)

"""Faces in videos (InsightFace, the photos' detector): frames from the
analysis proxy, chosen by Detail (frames.py), looked at by the faces job's
machines (the built-in one: the faces' own process, src/producers/faces/detect.py;
or a Lumiverb AI worker, ADR-022), and each person
seen saved as one face with the times they're seen (appearances.py). The
crops are cut from the frames here: the server has none (work.py)."""

from src.producers import names
from src.producers.contract import VIDEO, Data, Field, ProducerSpec, Setting
from src.producers.faces.levels import on
from src.producers.pools import FACES
from src.producers.video_faces.frames import DEFAULT, DETAILS

MAKES = Data(
    names.VIDEO_FACES, "The faces seen in a video: one for each person seen, and when. Who each is: "
    "lumiverb.people.",
    (names.BOX, Field("frame_ms", "number"), Field("seen_ms", "list", (Field("ms", "number"),)),
     Field("vector", "vector"), Field("crop", "image")),
    stored=("faces", "assets.face_count"), many=True,
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ANALYSIS_PROXY, names.SCENES, names.METADATA),
    artifact=names.VIDEO_FACES, version="1", media=VIDEO, unit="second",
    title="Faces in videos", order=101,
    # Its scenes first: Low looks at their pictures. Off finds none.
    applies=f"a.media_type = 'video' AND a.video_indexed AND {on(names.VIDEO_FACES, DEFAULT)}",
    made="a.face_count IS NOT NULL",
    settings=(
        Setting("model", "buffalo_l", "Model", kind="text", fixed="buffalo_l is the one face model installed."),
        # A level, not in the hash: videos looked at in fewer frames than now
        # are "below" (counted, looked at again when asked); at it or above,
        # current. Off finds none (applies).
        # Shown in the Faces row (part_of), so it's named for what it's the detail of.
        Setting("detail", DEFAULT, "Videos", kind="level", choices=DETAILS, remakes=False),
    ),
    # Its frames are the analysis proxy's: a file changed in place is a new clip (#37).
    redo_on_source_change=False,
    redo_note=("Faces people named, or said aren't a certain person, are kept; the rest are found again, and ones the "
               "new settings don't find go."),
    part_of=names.FACES,
    run="src.producers.video_faces.work:VideoFaces", pool=FACES,
)

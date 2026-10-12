"""Faces in videos (InsightFace, the photos' detector): frames from the
analysis proxy, chosen by Detail (frames.py), looked at on the server's GPU
by the faces' own process (src/producers/faces/detect.py), and each person
seen saved as one face with the times they're seen (appearances.py). The
crops are cut from the frames here: the server has none (work.py)."""

from src.producers.contract import VIDEO, ProducerSpec, Setting
from src.producers.faces.levels import on
from src.producers.pools import GPU
from src.producers.video_faces.frames import DEFAULT, DETAILS

PRODUCER = ProducerSpec(
    artifact="video_faces", producer="insightface-video", version="1", media=VIDEO, unit="second",
    title="Faces in videos", order=101,
    # Its scenes first: Low looks at their pictures. Off finds none.
    applies=f"a.media_type = 'video' AND a.video_indexed AND {on('video_faces', DEFAULT)}",
    made="a.face_count IS NOT NULL",
    settings=(
        Setting("model", "buffalo_l", "Model", kind="text", fixed="buffalo_l is the one face model installed."),
        # A level, not in the hash: videos looked at in fewer frames than now
        # are "below" (counted, looked at again when asked); at it or above,
        # current. Off finds none (applies).
        # Shown in the Faces row (part_of), so it's named for what it's the detail of.
        Setting("detail", DEFAULT, "Videos", kind="level", choices=DETAILS, remakes=False),
    ),
    needs=("analysis_proxy", "scenes"),
    # Its frames are the analysis proxy's: a file changed in place is a new clip (#37).
    redo_on_source_change=False,
    redo_note=("Faces people named, or said aren't a certain person, are kept; the rest are found again, and ones the "
               "new settings don't find go."),
    part_of="faces",
    kind="video_faces", flag="missing_video_faces", run="src.producers.video_faces.work:VideoFaces", pool=GPU,
)

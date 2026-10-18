"""Faces in photos (InsightFace), by the faces job's machines (ADR-022): the
built-in one on the server's GPU (src/processing/workers/faces/insightface_provider.py,
in a process of its own: detect.py) and Lumiverb's AI workers; 25 photos a
job, saved together (work.py). How many it finds
is its level (levels.py: Off / Low / Normal / High), as videos' is
(src/producers/video_faces)."""

from src.producers.contract import IMAGE, ProducerSpec, Setting
from src.producers.faces.levels import DETAILS, on
from src.producers.pools import FACES

DEFAULT = "normal"

# The face models a machine doing faces may serve (InsightFace's model packs): the producer's is in use.
MODELS = frozenset({"buffalo_l"})

# Proposed (PR: "merge close face clusters"): leaf selection splits one person's
# sub-modes (years, glasses, light) into groups; ones closer than this merge. On
# LFW with buffalo_l, 0.30-0.45 reunite people without mixing any; at 0.50
# Venus and Serena Williams become one group. 0.40 keeps that margin.
MERGE_DISTANCE = 0.40

PRODUCER = ProducerSpec(
    artifact="faces", producer="insightface", version="1", media=IMAGE, title="Faces", order=100,
    applies=f"a.media_type = 'image' AND {on('faces', DEFAULT)}",
    made="a.face_count IS NOT NULL",
    settings=(
        Setting("model", "buffalo_l", "Model", kind="text", fixed="buffalo_l is the one face model installed."),
        Setting("det_size", 640, "Detection size", unit="px", advanced=True,
                fixed="The detector's input, 640 × 640, as buffalo_l was trained."),
        # A level, not in the hash: photos looked at with stricter gates than
        # now are "below" (counted, looked at again when asked); at it or
        # above, current. Off finds none (applies). It replaces the gates
        # people once set one by one (confidence, size, sharpness).
        Setting("detail", DEFAULT, "Photos", kind="level", choices=DETAILS, remakes=False),
        # How faces no one has named are grouped on the People page (not how they're found:
        # changing these works the groups out again, and finds no face again).
        Setting("merge_close_clusters", True, "Merge close groups", kind="bool", remakes=False),
        # Cosine distance between L2-normalized ArcFace embeddings; see _cluster_face_embeddings.
        Setting("merge_distance", MERGE_DISTANCE, "Merge distance (cosine)", kind="float",
                minimum=0.05, maximum=0.6, advanced=True, remakes=False),
    ),
    uniform=True, needs=("proxy",),
    redo_note=("Faces people named, or said aren't a certain person, are kept; the rest are found again, and ones the "
               "new settings don't find go."),
    regroup="src.server.repository.faces:regroup_faces",
    # The faces job's machines (Settings › AI); on the server's GPU, the built-in one takes turns with CLIP.
    kind="faces", flag="missing_faces", run="src.producers.faces.work:Faces", pool=FACES, batch=25,
)

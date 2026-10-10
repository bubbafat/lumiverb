"""Faces in photos (InsightFace), on the brain's GPU, 25 photos a batch
(src/processing/workers/faces/insightface_provider.py, run by src/client/cli/repair.py)."""

from src.producers.contract import IMAGE, ProducerSpec, Setting

# Proposed (PR: "merge close face clusters"): leaf selection splits one person's
# sub-modes (years, glasses, light) into groups; ones closer than this merge. On
# LFW with buffalo_l, 0.30-0.45 reunite people without mixing any; at 0.50
# Venus and Serena Williams become one group. 0.40 keeps that margin.
MERGE_DISTANCE = 0.40

PRODUCER = ProducerSpec(
    artifact="faces", producer="insightface", version="1", media=IMAGE, title="Faces", order=100,
    applies="a.media_type = 'image'",
    made="a.face_count IS NOT NULL",
    settings=(
        Setting("model", "buffalo_l", "Model", kind="text", fixed="buffalo_l is the one face model installed."),
        Setting("det_size", 640, "Detection size", unit="px", advanced=True,
                fixed="The detector's input, 640 × 640, as buffalo_l was trained."),
        # Faces are found in the proxies (PROXY_CACHE_EDGE): a larger size looks at the same image,
        # and under the detector's 640 it would only be scaled up again.
        Setting("max_detect_edge", 1280, "Image size looked at", minimum=640, maximum=1280, unit="px",
                advanced=True),
        # Under 0.3 the detector hands back so many boxes that detection slows a lot.
        Setting("min_confidence", 0.5, "Least confidence", kind="float", minimum=0.3, maximum=0.99),
        Setting("min_area_fraction", 0.003, "Smallest face (share of the photo)", kind="float", minimum=0,
                maximum=0.5, advanced=True),
        # Measured in the image looked at, as the sharpness is.
        Setting("min_face_pixels", 40, "Smallest face, in the image looked at", minimum=8, maximum=512, unit="px",
                advanced=True),
        Setting("min_relative_size", 0.15, "Smallest beside the largest", kind="float", minimum=0, maximum=1,
                advanced=True),
        # How much detail the face has (the variance of its Laplacian); blurred faces score under 15.
        Setting("min_sharpness", 15.0, "Least sharpness (detail)", kind="float", minimum=0, maximum=200,
                advanced=True),
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
    regroup="src.server.repository.tenant:_mark_clusters_dirty",
    kind="faces", flag="missing_faces", run="src.server.scheduler.runners:faces", pool="gpu", batch=25,
)

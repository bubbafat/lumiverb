"""Faces in photos (InsightFace), on the brain's GPU, 25 photos a batch
(src/client/workers/faces/insightface_provider.py, run by src/client/cli/repair.py)."""

from src.producers.contract import IMAGE, ProducerSpec, Setting

PRODUCER = ProducerSpec(
    artifact="faces", producer="insightface", version="1", media=IMAGE, title="Faces", order=100,
    applies="a.media_type = 'image'",
    made="a.face_count IS NOT NULL",
    settings=(
        Setting("model", "buffalo_l", "Model", kind="text", fixed="buffalo_l is the one face model installed."),
        Setting("det_size", 640, "Detection size", unit="px", advanced=True,
                fixed="The detector's input, 640 × 640, as buffalo_l was trained."),
        # Faces are found in the proxies (PROXY_CACHE_EDGE): a larger size looks at the same image.
        Setting("max_detect_edge", 1280, "Image size", minimum=320, maximum=1280, unit="px", advanced=True),
        Setting("min_confidence", 0.5, "Least confidence", kind="float", minimum=0.05, maximum=0.99),
        Setting("min_area_fraction", 0.003, "Smallest face (share of the photo)", kind="float", minimum=0,
                maximum=0.5, advanced=True),
        Setting("min_face_pixels", 40, "Smallest face", minimum=8, maximum=512, unit="px", advanced=True),
        Setting("min_relative_size", 0.15, "Smallest beside the largest", kind="float", minimum=0, maximum=1,
                advanced=True),
        Setting("min_sharpness", 15.0, "Least sharpness", kind="float", minimum=0, maximum=1000, advanced=True),
    ),
    uniform=True, needs=("proxy",),
    kind="faces", flag="missing_faces", run="src.server.scheduler.runners:faces", pool="gpu", batch=25,
)

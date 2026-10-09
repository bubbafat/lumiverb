"""Faces in photos (InsightFace), on the brain's GPU, 25 photos a batch."""

from src.producers.contract import IMAGE, ProducerSpec

PRODUCER = ProducerSpec(
    artifact="faces", producer="insightface", version="1", media=IMAGE, title="Faces", order=100,
    applies="a.media_type = 'image'",
    made="a.face_count IS NOT NULL",
    defaults={"model": "buffalo_l", "det_size": 640, "max_detect_edge": 1280, "min_confidence": 0.5,
              "min_area_fraction": 0.003, "min_face_pixels": 40, "min_relative_size": 0.15,
              "min_sharpness": 15.0},
    uniform=True, needs=("proxy",),
    kind="faces", flag="missing_faces", run="src.server.scheduler.runners:faces", pool="gpu", batch=25,
)

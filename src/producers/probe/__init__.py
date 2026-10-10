"""Video facts: one ffprobe pass over a video (src/processing/video/probe.py),
stored as its facet (PUT /v1/assets/{id}/video-facet)."""

from src.producers.contract import SEE, VIDEO, ProducerSpec
from src.producers.pools import PROBES

PRODUCER = ProducerSpec(
    artifact="probe", producer="ffprobe", version="1", media=VIDEO, title="Video facts (probe)", order=10,
    applies="a.media_type = 'video'",
    made="EXISTS (SELECT 1 FROM video_facets vf WHERE vf.asset_id = a.asset_id)",
    kind="probe", flag="missing_probe", run="src.producers.probe.work:Probe", tier=SEE, pool=PROBES, storage=True,
)

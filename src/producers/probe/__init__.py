"""Video facts: one ffprobe pass over a video (src/processing/video/probe.py),
stored as its facet (PUT /v1/assets/{id}/video-facet)."""

from src.producers import names
from src.producers.contract import SEE, VIDEO, Data, Field, ProducerSpec
from src.producers.pools import PROBES

MAKES = Data(
    names.VIDEO_FORMAT, "A video's container, codecs, size, rotation, frame rate, timecode and audio, as "
    "ffprobe reads them.",
    (Field("duration_sec", "number"), Field("container", "text"), Field("video_codec", "text"),
     Field("width", "number"), Field("height", "number"), Field("rotation", "number"),
     Field("frame_rate", "number"), Field("start_timecode", "text"), Field("drop_frame", "yes or no"),
     Field("audio_codec", "text"), Field("audio_channels", "number"), Field("audio_sample_rate", "number")),
    stored=("video_facets",),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ORIGINAL,),
    artifact=names.VIDEO_FORMAT, version="1", media=VIDEO, title="Video facts (probe)", order=10,
    applies="a.media_type = 'video'",
    made="EXISTS (SELECT 1 FROM video_facets vf WHERE vf.asset_id = a.asset_id)",
    run="src.producers.probe.work:Probe", tier=SEE, pool=PROBES, storage=True,
)

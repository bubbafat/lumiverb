"""Video previews, made when a file is scanned (src/processing/scan.py)."""

from src.producers import names
from src.producers.contract import QUALITY, VIDEO, X264_CRF, Data, Field, ProducerSpec, Setting
from src.producers.proxy import MADE_AT_SCAN

MAKES = Data(
    names.VIDEO_PREVIEW, "Its short preview video, the first seconds: moving, where lumiverb.preview is a "
    "still.",
    (Field("video", "video"),),
    stored=("assets.video_preview_key",),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ORIGINAL,),
    artifact=names.VIDEO_PREVIEW, version="1", media=VIDEO, title="Video previews", order=30,
    applies="a.media_type = 'video'",
    made="a.video_preview_key IS NOT NULL",
    settings=(
        Setting("seconds", 10, "Length", minimum=1, maximum=60, unit="s", fixed=MADE_AT_SCAN),
        Setting("max_height", 720, "Height", minimum=144, maximum=2160, unit="px", fixed=MADE_AT_SCAN),
        # A notch: x264's CRF, as the analysis proxy's; the scan makes new files' previews with it.
        Setting("quality", "better", "Quality", kind="notch", choices=QUALITY, means=X264_CRF),
        Setting("audio_kbps", 128, "Audio", minimum=32, maximum=320, unit="kbps", advanced=True, fixed=MADE_AT_SCAN),
    ),
    made_at_scan=True,
)

"""Video previews, made when a file is scanned (src/processing/scan.py)."""

from src.producers.contract import VIDEO, ProducerSpec, Setting
from src.producers.proxy import MADE_AT_SCAN

PRODUCER = ProducerSpec(
    artifact="video_preview", producer="preview", version="1", media=VIDEO, title="Video previews", order=30,
    applies="a.media_type = 'video'",
    made="a.video_preview_key IS NOT NULL",
    settings=(
        Setting("seconds", 10, "Length", minimum=1, maximum=60, unit="s", fixed=MADE_AT_SCAN),
        Setting("max_height", 720, "Height", minimum=144, maximum=2160, unit="px", fixed=MADE_AT_SCAN),
        Setting("crf", 28, "Quality (CRF, lower is better)", minimum=18, maximum=40, advanced=True,
                fixed=MADE_AT_SCAN),
        Setting("audio_kbps", 128, "Audio", minimum=32, maximum=320, unit="kbps", advanced=True, fixed=MADE_AT_SCAN),
    ),
    cant_redo="Video previews are made when a file is scanned: lumiverb scan --force makes them again.",
)

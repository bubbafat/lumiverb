"""Video previews, made when a file is scanned (src/client/cli/scan.py)."""

from src.producers.contract import VIDEO, ProducerSpec

PRODUCER = ProducerSpec(
    artifact="video_preview", producer="preview", version="1", media=VIDEO, title="Video previews", order=30,
    applies="a.media_type = 'video'",
    made="a.video_preview_key IS NOT NULL",
    defaults={"seconds": 10, "max_height": 720, "crf": 28, "audio_kbps": 128},
    cant_redo="Video previews are made when a file is scanned: lumiverb scan --force makes them again.",
)

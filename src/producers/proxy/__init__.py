"""Proxies and thumbnails, made when a file is scanned (src/processing/scan.py)."""

from src.producers import names
from src.producers.contract import ALL, QUALITY, Data, Field, ProducerSpec, Setting

MADE_AT_SCAN = "Made at the scan with this; changing it isn't offered yet."
# What each notch of a preview's quality stands for (ADR-018 decision 6): the JPEG the scan
# makes from the original, and the WebP it sends and the server keeps, the thumbnail's too
# (src/server/preview_image.py). Better is what every one was made with before notches.
WEBP = {"good": {"jpeg_quality": 65, "webp_quality": 70},
        "better": {"jpeg_quality": 75, "webp_quality": 80},
        "best": {"jpeg_quality": 90, "webp_quality": 90}}

MAKES = Data(
    names.PREVIEW, "Its preview image and thumbnail: a still, for a video too.",
    (Field("preview", "image"), Field("thumbnail", "image")),
    stored=("assets.proxy_key", "assets.thumbnail_key"),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ORIGINAL,),
    artifact="proxy", producer="proxy", version="1", media=ALL, title="Proxies and thumbnails", order=20,
    applies="true",
    made="a.proxy_key IS NOT NULL",
    settings=(
        Setting("long_edge", 2048, "Longest edge", minimum=512, maximum=4096, unit="px", fixed=MADE_AT_SCAN),
        # A notch: the scan makes new files' previews with it (the account's, as the reader is told).
        Setting("quality", "better", "Quality", kind="notch", choices=QUALITY, means=WEBP),
        Setting("thumbnail_edge", 512, "Thumbnail edge", minimum=128, maximum=1024, unit="px", fixed=MADE_AT_SCAN),
    ),
    made_at_scan=True,
)

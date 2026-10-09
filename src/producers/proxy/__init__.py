"""Proxies and thumbnails, made when a file is scanned (src/client/cli/scan.py)."""

from src.producers.contract import ALL, ProducerSpec, Setting

MADE_AT_SCAN = "Made at the scan with these settings; changing them isn't wired yet."

PRODUCER = ProducerSpec(
    artifact="proxy", producer="proxy", version="1", media=ALL, title="Proxies and thumbnails", order=20,
    applies="true",
    made="a.proxy_key IS NOT NULL",
    settings=(
        Setting("long_edge", 2048, "Longest edge", minimum=512, maximum=4096, unit="px", fixed=MADE_AT_SCAN),
        Setting("jpeg_quality", 75, "JPEG quality", minimum=1, maximum=100, advanced=True, fixed=MADE_AT_SCAN),
        Setting("webp_quality", 80, "WebP quality", minimum=1, maximum=100, advanced=True, fixed=MADE_AT_SCAN),
        Setting("thumbnail_edge", 512, "Thumbnail edge", minimum=128, maximum=1024, unit="px", fixed=MADE_AT_SCAN),
    ),
    cant_redo="Proxies and thumbnails are made when a file is scanned: lumiverb scan --force makes them again.",
)

"""Proxies and thumbnails, made when a file is scanned (src/client/cli/scan.py)."""

from src.producers.contract import ALL, ProducerSpec

PRODUCER = ProducerSpec(
    artifact="proxy", producer="proxy", version="1", media=ALL, title="Proxies and thumbnails", order=20,
    applies="true",
    made="a.proxy_key IS NOT NULL",
    defaults={"long_edge": 2048, "jpeg_quality": 75, "webp_quality": 80, "thumbnail_edge": 512},
    cant_redo="Proxies and thumbnails are made when a file is scanned: lumiverb scan --force makes them again.",
)

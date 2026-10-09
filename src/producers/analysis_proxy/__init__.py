"""Analysis copies: a small video and its audio that transcripts and scenes
are made from (src/client/video/analysis_proxy.py)."""

from src.producers.contract import PREPARE, VIDEO, ProducerSpec, Setting

PRODUCER = ProducerSpec(
    artifact="analysis_proxy", producer="analysis-proxy", version="1", media=VIDEO, unit="second", title="Analysis proxies",
    order=40,
    applies="a.media_type = 'video'",
    made="a.analysis_proxy_key IS NOT NULL",
    settings=(
        Setting("max_edge", 960, "Longest edge", minimum=320, maximum=1920, unit="px"),
        Setting("fps_max", 30, "Most frames a second", minimum=1, maximum=60, advanced=True),
        Setting("crf", 28, "Quality (CRF, lower is better)", minimum=18, maximum=40, advanced=True),
        Setting("audio_kbps_per_channel", 48, "Audio per channel", minimum=16, maximum=128, unit="kbps",
                advanced=True),
    ),
    redo_on_source_change=False,
    kind="render", flag="missing_analysis_proxy", run="src.server.scheduler.runners:render", tier=PREPARE, pool="render", storage=True,
)

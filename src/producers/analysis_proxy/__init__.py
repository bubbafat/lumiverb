"""Analysis copies: a small video and its audio that transcripts and scenes
are made from (src/client/video/analysis_proxy.py)."""

from src.producers.contract import PREPARE, VIDEO, ProducerSpec

PRODUCER = ProducerSpec(
    artifact="analysis_proxy", producer="analysis-proxy", version="1", media=VIDEO, title="Analysis proxies",
    order=40,
    applies="a.media_type = 'video'",
    made="a.analysis_proxy_key IS NOT NULL",
    defaults={"max_edge": 960, "fps_max": 30, "crf": 28, "audio_kbps_per_channel": 48},
    redo_on_source_change=False,
    kind="render", flag="missing_analysis_proxy", run="src.server.scheduler.runners:render", tier=PREPARE, pool="render", storage=True,
)

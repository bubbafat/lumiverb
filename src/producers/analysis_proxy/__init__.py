"""Analysis copies: a small video and its audio that transcripts and scenes
are made from (src/processing/video/analysis_proxy.py)."""

from src.producers.contract import PREPARE, VIDEO, ProducerSpec, Setting
from src.producers.pools import RENDERS

# Its quality's notches, lowest first: smaller uploads from a device, or sharper playback (the web player streams it).
QUALITY = ("good", "better", "best")
CRF = {"good": {"crf": 32}, "better": {"crf": 28}, "best": {"crf": 23}}

# 2: a keyframe every 2 s of video and nowhere else (analysis_proxy.KEYFRAME_SEC).
# 1 let the encoder add them at changes of picture, and spaced them by frames.
PRODUCER = ProducerSpec(
    artifact="analysis_proxy", producer="analysis-proxy", version="2", media=VIDEO, unit="second", title="Analysis proxies",
    order=40,
    applies="a.media_type = 'video'",
    made="a.analysis_proxy_key IS NOT NULL",
    settings=(
        Setting("max_edge", 960, "Longest edge", minimum=320, maximum=1920, unit="px"),
        Setting("fps_max", 30, "Most frames a second", minimum=1, maximum=60, advanced=True),
        # A notch (ADR-018 decision 6): x264's CRF, NVENC's CQ two more (analysis_proxy._video_codec_args).
        # Better is CRF 28, what every one was made with before notches: none is made another way for them.
        Setting("quality", "better", "Quality", kind="notch", choices=QUALITY, means=CRF),
        Setting("audio_kbps_per_channel", 48, "Audio per channel", minimum=16, maximum=128, unit="kbps",
                advanced=True),
    ),
    redo_on_source_change=False,
    kind="render", flag="missing_analysis_proxy", run="src.producers.analysis_proxy.work:Render", tier=PREPARE,
    pool=RENDERS, storage=True,
)

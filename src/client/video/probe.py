"""One ffprobe pass per clip: the technical facts an editor export needs.

parse_ffprobe() is pure (ffprobe JSON in, VideoFacet out) so it can be
tested without media; probe_video() runs ffprobe on a file.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

# Nominal rates above this are container timebases (e.g. 90000/1 from
# some variable-frame-rate files), not frame rates.
MAX_PLAUSIBLE_FPS = 240


@dataclass(frozen=True)
class VideoFacet:
    duration_sec: float | None
    container: str | None
    video_codec: str | None
    width: int | None  # display width: rotation applied
    height: int | None
    rotation: int  # degrees clockwise to display upright: 0, 90, 180 or 270
    frame_rate_num: int | None
    frame_rate_den: int | None
    start_timecode: str | None  # "HH:MM:SS:FF", or "HH:MM:SS;FF" for drop-frame
    drop_frame: bool | None  # None when the clip has no timecode
    audio_codec: str | None
    audio_channels: int | None
    audio_sample_rate: int | None

    @property
    def frame_rate(self) -> float | None:
        if not self.frame_rate_num or not self.frame_rate_den:
            return None
        return self.frame_rate_num / self.frame_rate_den

    def to_dict(self) -> dict:
        return asdict(self)


def _rate(value: str | None) -> Fraction | None:
    if not value or "/" not in value:
        return None
    num, _, den = value.partition("/")
    try:
        n, d = int(num), int(den)
    except ValueError:
        return None
    if n <= 0 or d <= 0:
        return None
    return Fraction(n, d)


def _frame_rate(stream: dict) -> Fraction | None:
    nominal = _rate(stream.get("r_frame_rate"))
    if nominal is not None and nominal <= MAX_PLAUSIBLE_FPS:
        return nominal
    return _rate(stream.get("avg_frame_rate"))


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            # The display matrix gives counter-clockwise degrees.
            return int(-float(side["rotation"])) % 360
    rotate = (stream.get("tags") or {}).get("rotate")
    if rotate is not None:
        try:
            return int(float(rotate)) % 360
        except ValueError:
            return 0
    return 0


def _timecode(data: dict, video: dict | None) -> str | None:
    candidates = []
    if video is not None:
        candidates.append(video.get("tags") or {})
    candidates += [
        s.get("tags") or {}
        for s in data.get("streams", [])
        if s.get("codec_tag_string") == "tmcd"
    ]
    candidates.append(data.get("format", {}).get("tags") or {})
    for tags in candidates:
        if tags.get("timecode"):
            return str(tags["timecode"])
    return None


def _float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_ffprobe(data: dict) -> VideoFacet:
    """VideoFacet from `ffprobe -show_streams -show_format -of json` output."""
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})

    duration = _float(fmt.get("duration"))
    if duration is None and video is not None:
        duration = _float(video.get("duration"))

    rotation = _rotation(video) if video else 0
    width = _int(video.get("width")) if video else None
    height = _int(video.get("height")) if video else None
    if rotation in (90, 270):
        width, height = height, width

    rate = _frame_rate(video) if video else None
    timecode = _timecode(data, video)
    container = (fmt.get("format_name") or "").split(",")[0] or None

    return VideoFacet(
        duration_sec=duration,
        container=container,
        video_codec=video.get("codec_name") if video else None,
        width=width,
        height=height,
        rotation=rotation,
        frame_rate_num=rate.numerator if rate else None,
        frame_rate_den=rate.denominator if rate else None,
        start_timecode=timecode,
        drop_frame=(";" in timecode) if timecode else None,
        audio_codec=audio.get("codec_name") if audio else None,
        audio_channels=_int(audio.get("channels")) if audio else None,
        audio_sample_rate=_int(audio.get("sample_rate")) if audio else None,
    )


def probe_video(path: Path) -> VideoFacet:
    """Run ffprobe on a source file. Raises CalledProcessError if ffprobe fails."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_ffprobe(json.loads(result.stdout))

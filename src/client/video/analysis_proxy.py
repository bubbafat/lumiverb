"""Analysis proxies: full-length, low-resolution copies of videos with audio.

Transcription, scene detection and scene vision read these instead of the
originals, so enrichment continues while the storage holding the originals
sleeps (ADR-016 phase 2). They are for analysis, not editing: Resolve's
proxies stay outside the DAM.

The proxy is upright (rotation applied), at most `max_edge` pixels on its
long side, at most 30 fps, H.264 with the first audio track as stereo AAC,
and starts at 0 like the original, so times found in it are times in the
original.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Rendering a long recording takes a while; give up only when something's stuck.
MIN_TIMEOUT_SEC = 600.0
TIMEOUT_PER_SOURCE_SEC = 3.0
UNKNOWN_DURATION_TIMEOUT_SEC = 6 * 3600.0


class RenderError(Exception):
    """ffmpeg couldn't make the proxy."""


@dataclass(frozen=True)
class AnalysisProxySettings:
    max_edge: int = 960
    fps_max: int = 30
    # libx264 matched NVENC's speed on this box (decoding is the bottleneck)
    # with smaller files, and leaves the GPU to Whisper and CLIP.
    encoder: str = "libx264"
    crf: int = 28
    audio_bitrate: str = "128k"

    @classmethod
    def from_config(cls) -> AnalysisProxySettings:
        from src.client.cli.config import load_config

        cfg = load_config()
        return cls(max_edge=cfg.analysis_proxy_max_edge, encoder=cfg.analysis_proxy_encoder)


def _video_codec_args(settings: AnalysisProxySettings) -> list[str]:
    if settings.encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(settings.crf + 2), "-b:v", "0"]
    return ["-c:v", settings.encoder, "-preset", "veryfast", "-crf", str(settings.crf)]


def build_command(source: Path, dest: Path, settings: AnalysisProxySettings | None = None) -> list[str]:
    s = settings or AnalysisProxySettings()
    edge = s.max_edge
    scale = (
        f"scale=w='min(iw,{edge})':h='min(ih,{edge})'"
        f":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", scale, "-fpsmax", str(s.fps_max), "-pix_fmt", "yuv420p",
        *_video_codec_args(s), "-g", str(s.fps_max * 2),
        "-c:a", "aac", "-b:a", s.audio_bitrate, "-ac", "2",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-movflags", "+faststart", "-f", "mp4",
        str(dest),
    ]


def render_timeout(duration_sec: float | None) -> float:
    if not duration_sec:
        return UNKNOWN_DURATION_TIMEOUT_SEC
    return max(MIN_TIMEOUT_SEC, duration_sec * TIMEOUT_PER_SOURCE_SEC)


def render_analysis_proxy(
    source: Path,
    dest: Path,
    settings: AnalysisProxySettings | None = None,
    *,
    timeout: float | None = None,
) -> None:
    """Render `source` into `dest`, or raise RenderError and leave nothing."""
    part = dest.with_name(dest.name + ".part")
    cmd = build_command(source, part, settings)
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout or UNKNOWN_DURATION_TIMEOUT_SEC, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        part.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg timed out after {exc.timeout:.0f}s on {source.name}") from exc
    if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        stderr = result.stderr.decode(errors="replace").strip()[-500:]
        raise RenderError(f"ffmpeg failed on {source.name}: {stderr or f'exit {result.returncode}'}")
    part.replace(dest)

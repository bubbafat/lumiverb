"""Analysis proxies: full-length, low-resolution copies of videos with audio.

Transcription, scene detection and scene vision read these instead of the
originals, so enrichment continues while the storage holding the originals
sleeps (ADR-016 phase 2). They are for analysis, not editing: Resolve's
proxies stay outside the DAM.

The proxy is upright (rotation applied), at most `max_edge` pixels on its
long side, at most 30 fps, H.264, and keeps every audio track ffmpeg can
decode, in order, each as AAC at 48 kHz, at most stereo, at a low bitrate.
With several tracks a stereo mix of them comes first (handler "Lumiverb mix"):
browsers play only the first track, and a lav may be on any. It starts at 0
like the original, so times found in it are times in the original.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.client.video.audio import MIX_HANDLER, AudioTrack, audio_tracks, mix_filter

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
    # Speech, not music: Whisper hears 16 kHz mono.
    audio_kbps_per_channel: int = 48

    @classmethod
    def from_config(cls) -> AnalysisProxySettings:
        from src.client.cli.config import load_config

        cfg = load_config()
        return cls(max_edge=cfg.analysis_proxy_max_edge, encoder=cfg.analysis_proxy_encoder)


def _video_codec_args(settings: AnalysisProxySettings) -> list[str]:
    if settings.encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(settings.crf + 2), "-b:v", "0"]
    return ["-c:v", settings.encoder, "-preset", "veryfast", "-crf", str(settings.crf)]


def _audio_args(tracks: list[AudioTrack], settings: AnalysisProxySettings) -> list[str]:
    """A mix first when there are several tracks, then every track in order, each at most stereo."""
    if not tracks:
        return []
    kbps = settings.audio_kbps_per_channel
    graph: list[str] = []
    maps: list[str] = []
    per_track: list[str] = []
    out = 0
    if len(tracks) > 1:
        graph = ["-filter_complex", mix_filter(tracks, "mix")]
        maps += ["-map", "[mix]"]
        per_track += ["-ac:a:0", "2", "-b:a:0", f"{kbps * 2}k", "-metadata:s:a:0", f"handler_name={MIX_HANDLER}"]
        out = 1
    for t in tracks:
        ch = min(t.channels, 2)
        maps += ["-map", f"0:a:{t.index}"]
        per_track += [f"-ac:a:{out}", str(ch), f"-b:a:{out}", f"{kbps * ch}k"]
        out += 1
    return [*graph, *maps, "-c:a", "aac", "-ar", "48000", *per_track]


def build_command(
    source: Path,
    dest: Path,
    settings: AnalysisProxySettings | None = None,
    tracks: list[AudioTrack] = (),
) -> list[str]:
    """`tracks`: the source's decodable audio tracks (audio_tracks)."""
    s = settings or AnalysisProxySettings()
    edge = s.max_edge
    scale = (
        f"scale=w='min(iw,{edge})':h='min(ih,{edge})'"
        f":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source),
        "-map", "0:V:0",
        "-vf", scale, "-fpsmax", str(s.fps_max), "-pix_fmt", "yuv420p",
        *_video_codec_args(s), "-g", str(s.fps_max * 2),
        *_audio_args(list(tracks), s),
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-movflags", "+faststart", "-f", "mp4",
        str(dest),
    ]


def render_timeout(duration_sec: float | None) -> float:
    if not duration_sec:
        return UNKNOWN_DURATION_TIMEOUT_SEC
    return max(MIN_TIMEOUT_SEC, duration_sec * TIMEOUT_PER_SOURCE_SEC)


def _run(source: Path, part: Path, settings: AnalysisProxySettings | None, tracks: list[AudioTrack],
         timeout: float | None) -> None:
    cmd = build_command(source, part, settings, tracks)
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout or UNKNOWN_DURATION_TIMEOUT_SEC, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        part.unlink(missing_ok=True)
        raise _TimeoutError(f"ffmpeg timed out after {exc.timeout:.0f}s on {source.name}") from exc
    if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        stderr = result.stderr.decode(errors="replace").strip()[-500:]
        raise RenderError(f"ffmpeg failed on {source.name}: {stderr or f'exit {result.returncode}'}")


class _TimeoutError(RenderError):
    pass


def render_analysis_proxy(
    source: Path,
    dest: Path,
    settings: AnalysisProxySettings | None = None,
    *,
    timeout: float | None = None,
) -> None:
    """Render `source` into `dest`, or raise RenderError and leave nothing.

    If a render with several audio tracks fails, it tries once more with
    only the first: one odd track shouldn't cost the clip its transcript.
    """
    part = dest.with_name(dest.name + ".part")
    try:
        tracks = audio_tracks(source)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        # Never render without audio because a read failed: no transcript would follow.
        raise RenderError(f"ffprobe couldn't read {source.name}: {exc}") from exc
    try:
        _run(source, part, settings, tracks, timeout)
    except _TimeoutError:
        raise
    except RenderError as exc:
        if len(tracks) <= 1:
            raise
        logger.warning("%s; trying again with its first audio track only", exc)
        _run(source, part, settings, tracks[:1], timeout)
    part.replace(dest)

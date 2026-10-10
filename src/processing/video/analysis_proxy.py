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

Decoding a 4K HEVC original is most of a render. Where ffmpeg and the GPU
can, the GPU decodes (Vulkan by default): about four times faster on the
brain, with the same pictures, since decoding is exact. ffmpeg itself
decodes on the CPU what the GPU can't (ProRes, 4:2:2), and a render whose
GPU decoding fails runs again on the CPU. One render at a time decodes on
the GPU, and only while it has room beside the models using it; the rest
decode on the CPU meanwhile.
"""

from __future__ import annotations

import functools
import logging
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from src.processing.video.audio import MIX_HANDLER, AudioTrack, audio_tracks, mix_filter

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
    # "auto" (Vulkan when it works here), "cpu", or an ffmpeg hwaccel name.
    decoder: str = "auto"
    # Renders that decode on the GPU at once (the rest on the CPU meanwhile).
    gpu_decodes: int = 1

    @classmethod
    def for_producer(cls, settings: Mapping[str, Any], encoder: str, decoder: str = "auto",
                     gpu_decodes: int = 1) -> AnalysisProxySettings:
        """The account's settings (GET /v1/producers), rendered with this
        machine's encoder and decoder. Settings this version doesn't know are left out."""
        known = {f.name for f in fields(cls)} - set(MACHINE_CHOICES)
        return cls(**{k: v for k, v in settings.items() if k in known}, encoder=encoder, decoder=decoder,
                   gpu_decodes=gpu_decodes)

    def output(self) -> dict[str, Any]:
        """What lineage hashes: everything but this machine's way of
        rendering (encoder, decoder, GPU decodes), like an endpoint."""
        return {k: v for k, v in asdict(self).items() if k not in MACHINE_CHOICES}


MACHINE_CHOICES = ("encoder", "decoder", "gpu_decodes")


@functools.cache
def _vulkan_decodes() -> bool:
    """Whether this ffmpeg can open a Vulkan device here. A device without
    video decoding (a software driver) is fine: ffmpeg then decodes on the CPU."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-init_hw_device", "vulkan",
             "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.04", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


# How many renders decode on the GPU at once is this machine's choice
# (gpu_decodes in its config, default 1): one nearly saturates the brain's
# video decoder (a 3080's NVDEC at 100% with three, each a third as fast),
# and each holds a few hundred MB of video memory that the vision model and
# Whisper need more; three at once ran the vision model out of memory. The
# others decode on the CPU meanwhile.
@functools.cache
def _gpu_slots(count: int) -> threading.BoundedSemaphore:
    return threading.BoundedSemaphore(count)


# What a GPU decode leaves free for the models beside it to grow into.
GPU_DECODE_MIN_FREE_MB = 1536


def _gpu_has_room() -> bool:
    """Whether the GPU has room for a decode beside the models using it: free
    memory on the first NVIDIA GPU (the one ffmpeg picks). Without nvidia-smi,
    or an answer from it, there's nothing to go by: try."""
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=10, check=False)
    except (subprocess.SubprocessError, OSError):
        return True
    lines = result.stdout.split() if result.returncode == 0 else []
    try:
        return int(lines[0]) >= GPU_DECODE_MIN_FREE_MB
    except (IndexError, ValueError):
        return True


def gpu_decoder(choice: str) -> str | None:
    """The ffmpeg hwaccel to decode with, or None for the CPU."""
    if choice in ("", "cpu"):
        return None
    if choice == "auto":
        return "vulkan" if _vulkan_decodes() else None
    return choice


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
    hwaccel: str | None = None,
) -> list[str]:
    """`tracks`: the source's decodable audio tracks (audio_tracks);
    `hwaccel`: decode on the GPU with it (frames come back for the CPU's filters)."""
    s = settings or AnalysisProxySettings()
    edge = s.max_edge
    scale = (
        f"scale=w='min(iw,{edge})':h='min(ih,{edge})'"
        f":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *(["-hwaccel", hwaccel] if hwaccel else []),
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
         timeout: float | None, hwaccel: str | None = None) -> None:
    cmd = build_command(source, part, settings, tracks, hwaccel)
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

    If decoding on the GPU fails, it runs again on the CPU. If a render
    with several audio tracks fails, it tries once more with only the first:
    one odd track shouldn't cost the clip its transcript.
    """
    part = dest.with_name(dest.name + ".part")
    try:
        tracks = audio_tracks(source)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        # Never render without audio because a read failed: no transcript would follow.
        raise RenderError(f"ffprobe couldn't read {source.name}: {exc}") from exc
    s = settings or AnalysisProxySettings()
    hwaccel = gpu_decoder(s.decoder) if s.gpu_decodes > 0 else None
    slots = _gpu_slots(s.gpu_decodes) if hwaccel else None

    def attempt(with_tracks: list[AudioTrack]) -> None:
        if slots is not None and slots.acquire(blocking=False):
            try:
                if _gpu_has_room():
                    _run(source, part, settings, with_tracks, timeout, hwaccel)
                    return
            except RenderError as exc:
                logger.info("%s; decoding on the CPU instead", exc)
            finally:
                slots.release()
        _run(source, part, settings, with_tracks, timeout)

    try:
        attempt(tracks)
    except _TimeoutError:
        raise
    except RenderError as exc:
        if len(tracks) <= 1:
            raise
        logger.warning("%s; trying again with its first audio track only", exc)
        attempt(tracks[:1])
    part.replace(dest)

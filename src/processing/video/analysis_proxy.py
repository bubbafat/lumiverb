"""Analysis proxies: full-length, low-resolution copies of videos with audio.

Transcription, scene detection and scene vision read these instead of the
originals, so enrichment continues while the storage holding the originals
sleeps (ADR-016 phase 2). They are for analysis, not editing: Resolve's
proxies stay outside the DAM.

The proxy is upright (rotation applied), at most `max_edge` pixels on its
long side, at most 30 fps, H.264 with a keyframe every KEYFRAME_SEC of video
and nowhere else, and keeps every audio track ffmpeg can
decode, in order, each as AAC at 48 kHz, at most stereo, at a low bitrate.
With several tracks a stereo mix of them comes first (handler "Lumiverb mix"):
browsers play only the first track, and a lav may be on any. It starts at 0
like the original, so times found in it are times in the original.

Decoding a 4K HEVC original is most of a render. Where ffmpeg and the
machine can, its hardware decodes (ffmpeg_run.decoded_by: Vulkan on a
Linux GPU, about four times faster on prod; VideoToolbox on a Mac, a fifth
of the CPU's work on a 5.3K GoPro clip), with the same pictures, since
decoding is exact. ffmpeg itself decodes on the CPU what the hardware
can't (ProRes, 4:2:2), and a render whose hardware decoding fails runs
again on the CPU. On a GPU one render at a time decodes, and only while it
has room beside the models using it; the rest decode on the CPU meanwhile.
A Mac's media engine takes every render.

A render is stopped only when ffmpeg stops making progress, never for
being slow (ffmpeg_run.run).
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

from src.processing.video import ffmpeg_run
from src.processing.video.audio import MIX_HANDLER, AudioTrack, audio_tracks, mix_and_tracks_filter

logger = logging.getLogger(__name__)


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
    # "auto" (Vulkan, else VideoToolbox, when it works here), "cpu", or an ffmpeg hwaccel name.
    decoder: str = "auto"
    # Renders that decode on the GPU at once (the rest on the CPU meanwhile;
    # 0: none). A Mac's media engine takes every render.
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


# How many renders decode on the GPU at once is this machine's choice
# (gpu_decodes in its config, default 1): one nearly saturates prod's
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
    return ffmpeg_run.decoded_by(choice)


# A keyframe every this many seconds of video, and no others, whichever
# encoder makes it. Scenes are found from the keyframes alone, with
# thresholds measured on keyframes this far apart (src/producers/scenes/
# detail.py), so where they fall can't be the encoder's choice: x264 adds one
# at every big change of picture (50 in a minute of a fast clip, as close as
# 0.23 s), and -g counts frames, so 2.5 s apart at 24 fps.
KEYFRAME_SEC = 2


def _video_codec_args(settings: AnalysisProxySettings) -> list[str]:
    """The encoder, its quality, and no keyframes of its own choosing (_keyframes places them)."""
    if settings.encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(settings.crf + 2), "-b:v", "0",
                "-no-scenecut", "1", "-forced-idr", "1"]
    return ["-c:v", settings.encoder, "-preset", "veryfast", "-crf", str(settings.crf), "-sc_threshold", "0"]


def _keyframes(settings: AnalysisProxySettings) -> list[str]:
    """A keyframe at 0 and every KEYFRAME_SEC of video time after, at any frame rate."""
    return ["-force_key_frames", f"expr:gte(t,n_forced*{KEYFRAME_SEC})",
            "-g", str(settings.fps_max * KEYFRAME_SEC)]


def _audio_args(tracks: list[AudioTrack], settings: AnalysisProxySettings) -> list[str]:
    """A mix first when there are several tracks, then every track in order, each at most stereo.

    With several, every audio output comes from one filtergraph
    (mix_and_tracks_filter): mapping the tracks straight from the input
    beside a graph mixing them stalls ffmpeg 9 now and then."""
    if not tracks:
        return []
    kbps = settings.audio_kbps_per_channel
    graph: list[str] = []
    maps: list[str] = []
    per_track: list[str] = []
    out = 0
    if len(tracks) > 1:
        filtergraph, track_outs = mix_and_tracks_filter(tracks, "mix")
        graph = ["-filter_complex", filtergraph]
        maps += ["-map", "[mix]"]
        per_track += ["-ac:a:0", "2", "-b:a:0", f"{kbps * 2}k", "-metadata:s:a:0", f"handler_name={MIX_HANDLER}"]
        out = 1
    else:
        track_outs = [f"0:a:{tracks[0].index}"]
    for t, source in zip(tracks, track_outs):
        ch = min(t.channels, 2)
        maps += ["-map", source]
        per_track += [f"-ac:a:{out}", str(ch), f"-b:a:{out}", f"{kbps * ch}k"]
        out += 1
    return [*graph, *maps, "-c:a", "aac", "-ar", "48000", *per_track]


def source_pix_fmt(source: Path) -> str | None:
    """The pixel format the CPU decodes the source's video into (None when
    ffprobe can't say)."""
    try:
        result = subprocess.run(
            # Values alone: csv adds a field for side data (a rotated phone clip's).
            ["ffprobe", "-v", "error", "-select_streams", "V:0", "-show_entries", "stream=pix_fmt",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    found = result.stdout.split()
    if result.returncode != 0 or not found:
        return None
    return found[0]


def build_command(
    source: Path,
    dest: Path,
    settings: AnalysisProxySettings | None = None,
    tracks: list[AudioTrack] = (),
    hwaccel: str | None = None,
    pix_fmt: str | None = None,
) -> list[str]:
    """`tracks`: the source's decodable audio tracks (audio_tracks);
    `hwaccel`: decode on the GPU with it (frames come back for the CPU's
    filters); `pix_fmt`: the source's (source_pix_fmt), which those frames
    are first put back into. Hardware hands frames back in its own layout
    (a 10-bit clip as p010, not yuv420p10le), and a clip already within
    max_edge, so not resized, was made 8-bit along another path: the
    pictures differed from the CPU's (a 640x360 10-bit HEVC clip, on Vulkan
    and VideoToolbox alike). Repacked, the rest sees the same frames."""
    s = settings or AnalysisProxySettings()
    edge = s.max_edge
    scale = (
        f"scale=w='min(iw,{edge})':h='min(ih,{edge})'"
        f":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    if hwaccel and pix_fmt:
        scale = f"format={pix_fmt},{scale}"
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *(["-hwaccel", hwaccel] if hwaccel else []),
        "-i", str(source),
        "-map", "0:V:0",
        "-vf", scale, "-fpsmax", str(s.fps_max), "-pix_fmt", "yuv420p",
        *_video_codec_args(s), *_keyframes(s),
        *_audio_args(list(tracks), s),
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-movflags", "+faststart", "-f", "mp4",
        str(dest),
    ]


def _run(source: Path, part: Path, settings: AnalysisProxySettings | None, tracks: list[AudioTrack],
         stall_after: float, hwaccel: str | None = None) -> None:
    cmd = build_command(source, part, settings, tracks, hwaccel, source_pix_fmt(source) if hwaccel else None)
    try:
        result = ffmpeg_run.run(cmd, stall_after=stall_after)
    except ffmpeg_run.StalledError as exc:
        part.unlink(missing_ok=True)
        raise _StuckError(f"{exc} on {source.name}") from exc
    if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        stderr = result.stderr.strip()[-500:]
        raise RenderError(f"ffmpeg failed on {source.name}: {stderr or f'exit {result.returncode}'}")


class _StuckError(RenderError):
    pass


def render_analysis_proxy(
    source: Path,
    dest: Path,
    settings: AnalysisProxySettings | None = None,
    *,
    stall_after: float = ffmpeg_run.STALL_SEC,
) -> None:
    """Render `source` into `dest`, or raise RenderError and leave nothing.

    If hardware decoding fails, it runs again on the CPU. If a render with
    several audio tracks fails, it tries once more with only the first: one
    odd track shouldn't cost the clip its transcript. ffmpeg is stopped
    only when it made no progress for `stall_after` seconds.
    """
    part = dest.with_name(dest.name + ".part")
    try:
        tracks = audio_tracks(source)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        # Never render without audio because a read failed: no transcript would follow.
        raise RenderError(f"ffprobe couldn't read {source.name}: {exc}") from exc
    s = settings or AnalysisProxySettings()
    hwaccel = gpu_decoder(s.decoder) if s.gpu_decodes > 0 else None
    shared = hwaccel in ffmpeg_run.SHARED_MEMORY
    slots = _gpu_slots(s.gpu_decodes) if hwaccel and not shared else None

    def attempt(with_tracks: list[AudioTrack]) -> None:
        if hwaccel and (shared or slots.acquire(blocking=False)):
            try:
                if shared or _gpu_has_room():
                    _run(source, part, settings, with_tracks, stall_after, hwaccel)
                    return
            except RenderError as exc:
                logger.info("%s; decoding on the CPU instead", exc)
            finally:
                if slots is not None:
                    slots.release()
        _run(source, part, settings, with_tracks, stall_after)

    try:
        attempt(tracks)
    except _StuckError:
        raise
    except RenderError as exc:
        if len(tracks) <= 1:
            raise
        logger.warning("%s; trying again with its first audio track only", exc)
        attempt(tracks[:1])
    part.replace(dest)

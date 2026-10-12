"""A video's audio tracks, and the audio speech recognition hears.

Cameras often record a lav on its own track, so analysis keeps every track.
With several, the analysis proxy carries a mix of them first (what browsers
play, and what transcription hears), then each track as it was.
"""

from __future__ import annotations

import functools
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.processing.video import ffmpeg_run

logger = logging.getLogger(__name__)

# handler_name of the analysis proxy's mixed first track.
MIX_HANDLER = "Lumiverb mix"
# Codecs ffprobe names for audio it doesn't know at all.
_UNDECODABLE = {"", "none", "unknown"}
# Lays a track out from the clip's start: silence where it starts late or skips.
_FROM_START = "aresample=async=1:first_pts=0"


@dataclass(frozen=True)
class AudioTrack:
    index: int  # among the file's audio streams: ffmpeg's 0:a:<index>
    channels: int
    codec: str
    handler: str = ""


@functools.cache
def decodable_audio() -> frozenset[str] | None:
    """The audio codecs this ffmpeg can decode, by the names ffprobe gives
    them (`ffmpeg -codecs`, whose first flag is D); None when it can't say.

    A codec ffmpeg knows isn't one it can decode: ffmpeg 9 names iPhone
    spatial audio (apple_apac) and has no decoder for it, so a track of it
    in a render fails the render."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-codecs"], capture_output=True, text=True,
                             timeout=30, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    found = set()
    for line in out.splitlines():
        flags, _, rest = line.strip().partition(" ")
        if len(flags) == 6 and flags[0] == "D" and flags[2] == "A" and rest.split():
            found.add(rest.split()[0])
    return frozenset(found) or None


def audio_tracks(path: Path, *, timeout: float = 60.0) -> list[AudioTrack]:
    """The audio tracks ffmpeg can decode, in order. Raises if ffprobe can't read the file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", *ffmpeg_run.input_args(), "-select_streams", "a",
         "-show_entries", "stream=codec_name,channels:stream_tags=handler_name", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=timeout, check=True,
    ).stdout
    decodable = decodable_audio()
    tracks = []
    for i, s in enumerate(json.loads(out or "{}").get("streams", [])):
        codec, channels = s.get("codec_name") or "", int(s.get("channels") or 0)
        if codec in _UNDECODABLE or channels <= 0 or (decodable is not None and codec not in decodable):
            logger.info("%s: leaving out audio track %d (%s, %d channels): ffmpeg can't decode it",
                        path.name, i, codec or "unknown codec", channels)
            continue
        tracks.append(AudioTrack(i, channels, codec, (s.get("tags") or {}).get("handler_name", "")))
    return tracks


def _amix(count: int, label: str) -> str:
    """Mixes stereo inputs [m0]..[m<count-1>] into `[label]`, each at its own level.

    amix's default divides by the number of tracks, so one live lav among
    eight would come out 18 dB down; the limiter stops loud ones clipping.
    """
    inputs = "".join(f"[m{n}]" for n in range(count))
    return f"{inputs}amix=inputs={count}:normalize=0,alimiter=limit=0.95:level=false[{label}]"


def mix_filter(tracks: list[AudioTrack], label: str) -> str:
    """A filtergraph mixing `tracks` into stereo `[label]` (_amix).

    amix takes samples in order, so each track is first laid out from the
    clip's start, with silence where it starts late or skips.
    """
    parts = [f"[0:a:{t.index}]{_FROM_START},aformat=channel_layouts=stereo[m{n}]" for n, t in enumerate(tracks)]
    return ";".join([*parts, _amix(len(tracks), label)])


def mix_and_tracks_filter(tracks: list[AudioTrack], label: str) -> tuple[str, list[str]]:
    """A filtergraph giving the mix of `tracks` as `[label]` and each track
    again on its own: the graph and those tracks' labels, in order.

    Every audio output comes from the one graph, each track laid out from the
    clip's start once and split between the mix and its own output. Taking
    the tracks straight from the input alongside a graph mixing them (with
    the video in the same file) stalls ffmpeg 9 now and then: about half the
    time under load, a late-starting track's packets queue while the mix
    waits for them, and ffmpeg sits idle until killed.
    """
    parts: list[str] = []
    outs: list[str] = []
    for n, t in enumerate(tracks):
        parts.append(f"[0:a:{t.index}]{_FROM_START},asplit=2[s{n}][t{n}]")
        parts.append(f"[s{n}]aformat=channel_layouts=stereo[m{n}]")
        outs.append(f"[t{n}]")
    return ";".join([*parts, _amix(len(tracks), label)]), outs


def speech_wav_command(source: Path, wav: Path, tracks: list[AudioTrack]) -> list[str]:
    """ffmpeg command for a 16 kHz mono WAV of everything said on any track, timed as in the clip."""
    if tracks[0].handler == MIX_HANDLER or len(tracks) == 1:
        audio = ["-map", f"0:a:{tracks[0].index}", "-af", _FROM_START]
    else:
        audio = ["-filter_complex", mix_filter(tracks, "speech"), "-map", "[speech]"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *ffmpeg_run.input_args(), "-i", str(source), *audio, "-ar", "16000", "-ac", "1", "-f", "wav", str(wav),
    ]

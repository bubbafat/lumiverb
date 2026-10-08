"""A video's audio tracks, and the audio speech recognition hears.

Cameras often record a lav on its own track, so analysis keeps every track.
With several, the analysis proxy carries a mix of them first (what browsers
play, and what transcription hears), then each track as it was.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# handler_name of the analysis proxy's mixed first track.
MIX_HANDLER = "Lumiverb mix"
# Codecs ffprobe names for audio it can't decode (e.g. iPhone spatial audio).
_UNDECODABLE = {"", "none", "unknown"}
# Lays a track out from the clip's start: silence where it starts late or skips.
_FROM_START = "aresample=async=1:first_pts=0"


@dataclass(frozen=True)
class AudioTrack:
    index: int  # among the file's audio streams: ffmpeg's 0:a:<index>
    channels: int
    codec: str
    handler: str = ""


def audio_tracks(path: Path, *, timeout: float = 60.0) -> list[AudioTrack]:
    """The audio tracks ffmpeg can decode, in order. Raises if ffprobe can't read the file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name,channels:stream_tags=handler_name", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=timeout, check=True,
    ).stdout
    tracks = []
    for i, s in enumerate(json.loads(out or "{}").get("streams", [])):
        codec, channels = s.get("codec_name") or "", int(s.get("channels") or 0)
        if codec in _UNDECODABLE or channels <= 0:
            logger.info("%s: leaving out audio track %d (%s, %d channels): ffmpeg can't decode it",
                        path.name, i, codec or "unknown codec", channels)
            continue
        tracks.append(AudioTrack(i, channels, codec, (s.get("tags") or {}).get("handler_name", "")))
    return tracks


def mix_filter(tracks: list[AudioTrack], label: str) -> str:
    """A filtergraph mixing `tracks` into stereo `[label]`, each at its own level.

    amix's default divides by the number of tracks, so one live lav among
    eight would come out 18 dB down; the limiter stops loud ones clipping.
    amix also takes samples in order, so each track is first laid out from
    the clip's start, with silence where it starts late or skips.
    """
    parts = [f"[0:a:{t.index}]{_FROM_START},aformat=channel_layouts=stereo[m{n}]" for n, t in enumerate(tracks)]
    inputs = "".join(f"[m{n}]" for n in range(len(tracks)))
    parts.append(f"{inputs}amix=inputs={len(tracks)}:normalize=0,alimiter=limit=0.95:level=false[{label}]")
    return ";".join(parts)


def speech_wav_command(source: Path, wav: Path, tracks: list[AudioTrack]) -> list[str]:
    """ffmpeg command for a 16 kHz mono WAV of everything said on any track, timed as in the clip."""
    if tracks[0].handler == MIX_HANDLER or len(tracks) == 1:
        audio = ["-map", f"0:a:{tracks[0].index}", "-af", _FROM_START]
    else:
        audio = ["-filter_complex", mix_filter(tracks, "speech"), "-map", "[speech]"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), *audio, "-ar", "16000", "-ac", "1", "-f", "wav", str(wav),
    ]

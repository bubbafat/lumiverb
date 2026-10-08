"""A video's audio tracks, and the audio speech recognition hears.

Cameras often record a lav on its own track, so analysis keeps every track:
the analysis proxy carries them all, and transcription hears them mixed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def audio_channels(path: Path, *, timeout: float = 60.0) -> list[int]:
    """Each audio track's channel count, in order. Raises if ffprobe can't read the file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=channels",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=timeout, check=True,
    ).stdout
    return [int(s.get("channels") or 2) for s in json.loads(out or "{}").get("streams", [])]


def speech_wav_command(source: Path, wav: Path, channels: list[int]) -> list[str]:
    """ffmpeg command for a 16 kHz mono WAV with every audio track mixed in."""
    if len(channels) > 1:
        inputs = "".join(f"[0:a:{i}]" for i in range(len(channels)))
        audio = ["-filter_complex", f"{inputs}amix=inputs={len(channels)}:duration=longest[speech]",
                 "-map", "[speech]"]
    else:
        audio = ["-map", "0:a:0"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), *audio, "-ar", "16000", "-ac", "1", "-f", "wav", str(wav),
    ]

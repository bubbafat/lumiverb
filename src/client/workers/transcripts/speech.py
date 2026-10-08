"""The speech in a clip, found on the worker before any machine hears it.

The transcript producer's settings say which silences are skipped (VAD:
vad_min_silence_ms, src/shared/producers.py). The worker finds the speech
itself with faster-whisper's VAD, as faster-whisper's vad_filter does, and
sends only the speech to whichever machine transcribes it, the built-in
Whisper or a server. The times it says are the speech's; they're mapped
back onto the clip as faster-whisper maps them. So whichever machine made
a transcript, it was made with the recorded settings.
"""

from __future__ import annotations

import bisect
import json
import logging
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from src.client.workers.transcripts import child
from src.client.workers.transcripts.base import Segment

logger = logging.getLogger(__name__)

RATE = child.RATE
# Finding the speech in an hour of audio takes about a minute on the CPU.
FIND_TIMEOUT_SEC = 1800


class SpeechError(Exception):
    """The speech couldn't be found (the audio couldn't be read, or the VAD failed)."""


def find_speech(wav: Path, speech_wav: Path, min_silence_ms: int, timeout: float = FIND_TIMEOUT_SEC) -> list[dict]:
    """The speech in wav ({start, end} in samples; [] when nothing is said),
    written to speech_wav when there is some. In a subprocess (child.py)."""
    try:
        proc = subprocess.run([sys.executable, child.__file__, "find-speech", str(wav), str(speech_wav),
                               str(int(min_silence_ms))], capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as e:
        raise SpeechError(f"Finding the speech failed: {e}") from e
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except ValueError:
        out = {}
    if proc.returncode != 0 or "chunks" not in out:
        raise SpeechError(out.get("error") or (proc.stderr or "").strip()[-500:] or "Finding the speech failed.")
    return out["chunks"]


class SpeechMap:
    """Times in the speech alone, back onto the clip (faster_whisper.vad.SpeechTimestampsMap)."""

    def __init__(self, chunks: list[dict], rate: int = RATE, precision: int = 2) -> None:
        self._rate = rate
        self._precision = precision
        self._ends: list[int] = []  # each chunk's end, in the speech
        self._silence: list[float] = []  # seconds skipped before each chunk
        previous_end = silent = 0
        for c in chunks:
            silent += c["start"] - previous_end
            previous_end = c["end"]
            self._ends.append(c["end"] - silent)
            self._silence.append(silent / rate)

    def _chunk(self, time: float, is_end: bool) -> int:
        sample = int(time * self._rate)
        if is_end and sample in self._ends:
            return self._ends.index(sample)
        return min(bisect.bisect(self._ends, sample), len(self._ends) - 1)

    def original(self, time: float, *, is_end: bool = False) -> float:
        return round(self._silence[self._chunk(time, is_end)] + time, self._precision)


def restore(segments: Iterable[Segment], chunks: list[dict]) -> list[Segment]:
    """Segments timed in the speech, timed in the clip."""
    to_clip = SpeechMap(chunks)
    return [Segment(to_clip.original(s.start), to_clip.original(s.end, is_end=True), s.text) for s in segments]


def _stamp(seconds: float) -> str:
    return (f"{int(seconds // 3600):02d}:{int((seconds % 3600) // 60):02d}:{int(seconds % 60):02d},"
            f"{int((seconds % 1) * 1000):03d}")


def to_srt(segments: Iterable[Segment]) -> str:
    """SRT as the worker has always written it: numbered by segment, those with nothing said left out."""
    parts = []
    for i, seg in enumerate(segments, 1):
        text = seg.text.strip()
        if text:
            parts.append(f"{i}\n{_stamp(seg.start)} --> {_stamp(seg.end)}\n{text}\n")
    return "\n".join(parts)

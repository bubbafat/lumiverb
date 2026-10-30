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
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from src.processing.workers.transcripts import child
from src.processing.workers.transcripts.base import Segment

logger = logging.getLogger(__name__)

RATE = child.RATE
# Finding the speech in an hour of audio takes about a minute on the CPU.
FIND_TIMEOUT_SEC = 1800


def speech_seconds(wav: Path) -> float:
    """How long a WAV is, in seconds (0 when it can't be read)."""
    import wave

    try:
        with wave.open(str(wav)) as w:
            return w.getnframes() / float(w.getframerate() or RATE)
    except (OSError, EOFError, wave.Error):
        return 0.0


def time_allowed(seconds: float) -> float:
    """How long a machine gets to transcribe that much speech before it's
    taken as stuck on the clip: ten minutes, and four times the speech
    (a CPU server can be slower than real time)."""
    return 600.0 + 4.0 * seconds


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


# What Whisper writes for a stretch it can't make out (the end of its training's videos and their subtitles), as
# normalized() says them: left out where the machine itself thought it most likely wasn't speech (NOT_SPEECH).
# Robert's videos, Oct 30: 405 transcripts were three words or fewer, mostly these; every one of 31 such lines
# tried again was at 0.38 or more, where the words of what's really said were at 0.31 (the median).
STOCK = frozenset({
    "you", "i", "bye", "bye bye", "thank you", "thanks", "thank you very much", "thank you so much",
    "thank you for watching", "thanks for watching", "thank you for watching see you next time",
    "please subscribe", "subtitles by the amaraorg community", "soustitres réalisés par la communauté damaraorg",
    "ご視聴ありがとうございました", "감사합니다", "고맙습니다", "시청해주셔서 감사합니다",
})
NOT_SPEECH = 0.3
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")
_NOT_A_WORD = re.compile(r"[^\w\s]+")


def normalized(text: str) -> str:
    """Text as compared: lower case, no punctuation, spaces single."""
    return " ".join(_NOT_A_WORD.sub("", text.lower()).split())


def _once(text: str) -> str:
    """A segment's text with a sentence said again and again in a row said once ("Thank you. Thank you. …")."""
    sentences = _SENTENCE_END.split(text)
    kept = [s for i, s in enumerate(sentences) if i == 0 or normalized(s) != normalized(sentences[i - 1])]
    if len(kept) == len(sentences):
        return text
    return (" " if text.startswith(" ") else "") + " ".join(s.strip() for s in kept)


def cleaned(segments: Iterable[Segment]) -> list[Segment]:
    """What a machine heard, without what Whisper makes up: a line said again right after itself (a loop) kept
    once, a sentence said over and over in one line once, and a stock line (STOCK) where the machine thought it
    wasn't speech (NOT_SPEECH) left out. A machine that didn't say how likely that was keeps them."""
    out: list[Segment] = []
    for s in segments:
        text = _once(s.text)
        said = normalized(text)
        if said in STOCK and s.no_speech is not None and s.no_speech >= NOT_SPEECH:
            continue
        if said and out and said == normalized(out[-1].text):
            continue
        out.append(Segment(s.start, s.end, text, s.no_speech))
    return out


def _stamp(seconds: float) -> str:
    # Whole milliseconds first: 2.8 s is 02,800 (the float parts gave 02,799).
    ms = round(seconds * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(segments: Iterable[Segment]) -> str:
    """SRT as the worker has always written it: numbered by segment, those with nothing said left out."""
    parts = []
    for i, seg in enumerate(segments, 1):
        text = seg.text.strip()
        if text:
            parts.append(f"{i}\n{_stamp(seg.start)} --> {_stamp(seg.end)}\n{text}\n")
    return "\n".join(parts)

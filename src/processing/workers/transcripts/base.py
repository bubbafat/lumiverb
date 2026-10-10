"""What a machine doing transcripts hears and says (Settings → AI)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    """Something said, start and end in seconds."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Heard:
    """What a machine heard in the speech it was sent: times are the speech's, not the clip's."""

    segments: list[Segment] = field(default_factory=list)
    language: str = ""


class TranscriptError(Exception):
    """A machine couldn't transcribe the speech.

    endpoint_fault: the machine was at fault (unreachable, refused the key,
    the model gone or not loading, out of memory, not speaking the API), not
    the clip: its work goes to another machine, and with none left the job
    waits without charging the clip. machine: the one that served it.
    """

    def __init__(self, message: str, *, endpoint_fault: bool) -> None:
        super().__init__(message)
        self.endpoint_fault = endpoint_fault
        self.machine = None


class Transcriber(ABC):
    @abstractmethod
    def transcribe(self, speech_wav: Path) -> Heard:
        """What was said in speech_wav (16 kHz mono WAV of speech alone). Raises TranscriptError."""

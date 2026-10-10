"""The built-in Whisper: faster-whisper on the worker's own computer, one
subprocess per clip (child.py), like the transcription it replaces."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from src.processing.workers.transcripts import child
from src.processing.workers.transcripts.base import Heard, Segment, Transcriber, TranscriptError
from src.processing.workers.transcripts.speech import speech_seconds, time_allowed

# Failures mid-transcription that are the computer's, not the clip's.
_MACHINE_TROUBLE = ("out of memory", "cuda", "cublas", "cudnn", "no space left")


def unavailable() -> str:
    """Why this worker can't run Whisper itself; "" when it can."""
    if importlib.util.find_spec("faster_whisper") is None:
        return "faster-whisper isn't installed with this worker (uv sync --extra workers)."
    return ""


class BuiltInWhisper(Transcriber):
    def __init__(self, model: str, *, device: str = "auto", timeout: float | None = None) -> None:
        self._model = model
        self._device = device
        self._timeout = timeout

    def transcribe(self, speech_wav: Path) -> Heard:
        allowed = self._timeout or time_allowed(speech_seconds(speech_wav))
        try:
            proc = subprocess.run([sys.executable, child.__file__, "transcribe", str(speech_wav), self._model,
                                   self._device], capture_output=True, text=True, timeout=allowed)
        except subprocess.TimeoutExpired as e:
            raise TranscriptError(f"Whisper didn't finish within {int(allowed)} s.", endpoint_fault=False) from e
        except OSError as e:
            raise TranscriptError(f"Whisper couldn't start: {e}", endpoint_fault=True) from e
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
        except ValueError:
            out = None
        if not isinstance(out, dict):
            # It died without a word (killed for memory, a crash): this clip's doing as far as
            # anyone can tell, so it's charged once a check finds the machine fine, and
            # tried again later, rather than stopping transcription every time it comes up.
            why = (proc.stderr or "").strip()[-300:] or f"exit {proc.returncode}"
            raise TranscriptError(f"Whisper stopped: {why}", endpoint_fault=False)
        if proc.returncode != 0 or "error" in out:
            error = str(out.get("error") or "Whisper failed.")
            machine = out.get("stage") == "load" or any(t in error.lower() for t in _MACHINE_TROUBLE)
            raise TranscriptError(error, endpoint_fault=machine)
        return Heard([Segment(float(s["start"]), float(s["end"]), str(s.get("text") or ""))
                      for s in out.get("segments") or []], str(out.get("language") or ""))

"""Transcription only while a machine doing transcripts can (Settings → AI).

The transcripts job's machines: the built-in Whisper (the worker's own
computer) and transcription servers. When none can be used, transcription
waits without charging any clip (src/client/cli/job_guard.py). Whichever
machine hears a clip, the worker finds its speech first, so it also needs
faster-whisper here (src/client/workers/transcripts/speech.py).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.client.cli.ai_pool import Machine, PooledTranscriber
from src.client.cli.job_guard import JobGuard

if TYPE_CHECKING:
    from src.client.cli.failure_report import FailureReport
    from src.client.workers.transcripts.base import Transcriber


class TranscriptGuard(JobGuard):
    def __init__(self, client: Any, failures: FailureReport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(client, "transcripts", failures, clock)

    def check(self) -> bool:
        from src.client.workers.transcripts.local import unavailable

        # Checked and reported either way: without faster-whisper here, Settings
        # shows the built-in Whisper offline, saying why.
        ok = super().check()
        why = unavailable()
        if why:
            # Every machine hears only the speech, and it's found here.
            self.error = f"This worker can't find the speech in clips: {why}"
            return False
        return ok

    def transcriber(self) -> Transcriber:
        """Transcribes speech on whichever online machine is free, by the name it knows the model by."""
        return PooledTranscriber(self.pool, _transcriber_for)


def _transcriber_for(machine: Machine) -> Transcriber:
    from src.client.workers.transcripts.local import BuiltInWhisper
    from src.client.workers.transcripts.openai_compatible import OpenAITranscriber

    if machine.built_in:
        return BuiltInWhisper(machine.serves)
    return OpenAITranscriber(machine.api_url, machine.api_key, machine.serves)

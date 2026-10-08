"""A transcription server (Settings → AI): OpenAI's POST /audio/transcriptions,
as speaches and LocalAI serve it.

It's sent the speech alone (the worker found it: speech.py), as a 16 kHz
mono WAV, with the job's model by the name the server lists it as, and
asked for verbose_json: segments with their times, and the language. It
isn't asked to skip silences of its own (none are left to skip).
"""

from __future__ import annotations

from pathlib import Path

import requests

from src.client.workers.captions.base import ENDPOINT_FAULT_STATUSES
from src.client.workers.transcripts.base import Heard, Segment, Transcriber, TranscriptError

CONNECT_TIMEOUT_SEC = 10
# A long clip takes minutes; this is for one that never ends.
READ_TIMEOUT_SEC = 3600


class OpenAITranscriber(Transcriber):
    def __init__(self, api_url: str, api_key: str | None, model: str, *, timeout: float = READ_TIMEOUT_SEC) -> None:
        self._url = f"{api_url.rstrip('/')}/audio/transcriptions"
        self._key = api_key
        self._model = model
        self._timeout = timeout

    def transcribe(self, speech_wav: Path) -> Heard:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            with open(speech_wav, "rb") as audio:
                resp = requests.post(self._url, headers=headers, timeout=(CONNECT_TIMEOUT_SEC, self._timeout),
                                     files={"file": ("speech.wav", audio, "audio/wav")},
                                     data={"model": self._model, "response_format": "verbose_json",
                                           "timestamp_granularities[]": "segment"})
        except requests.ConnectionError as e:
            raise TranscriptError(f"Couldn't reach {self._url}: {type(e).__name__}.", endpoint_fault=True) from e
        except requests.Timeout as e:
            raise TranscriptError(f"{self._url} didn't answer in time.", endpoint_fault=True) from e
        if resp.status_code >= 400:
            said = (resp.text or "").strip()[:300]
            if resp.status_code in (401, 403):
                why = "refused the key"
            elif resp.status_code == 404:
                why = f"has no {self._model} or no transcription API there"
            else:
                why = f"answered {resp.status_code}"
            fault = resp.status_code in ENDPOINT_FAULT_STATUSES or resp.status_code >= 500
            raise TranscriptError(f"{self._url} {why}" + (f": {said}" if said else "."), endpoint_fault=fault)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise TranscriptError(f"{self._url} didn't answer in OpenAI's JSON.", endpoint_fault=True)
        segments = data.get("segments")
        if not isinstance(segments, list):
            if str(data.get("text") or "").strip():
                # Text without times can't make a transcript: the server doesn't do verbose_json.
                raise TranscriptError(f"{self._url} said what it heard without times (it needs verbose_json).",
                                      endpoint_fault=True)
            segments = []
        try:
            heard = [Segment(float(s["start"]), float(s["end"]), str(s.get("text") or "")) for s in segments]
        except (KeyError, TypeError, ValueError) as e:
            raise TranscriptError(f"{self._url} sent segments without times.", endpoint_fault=True) from e
        return Heard(heard, str(data.get("language") or ""))

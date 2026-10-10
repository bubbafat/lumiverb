"""A transcription server (Settings → AI): OpenAI's POST /audio/transcriptions,
as speaches and LocalAI serve it.

It's sent the speech alone (the worker found it: speech.py), as a 16 kHz
mono WAV, with the job's model by the name the server lists it as, and
asked for verbose_json: segments with their times, and the language. It's
asked not to skip silences of its own (vad_filter=false: none are left to
skip) and to time its segments (without_timestamps=false: speaches
otherwise answers in pieces of up to 30 s). Servers that don't know those
fields ignore them; current speaches skips short silences whatever it's told.
"""

from __future__ import annotations

from pathlib import Path

import requests

from src.processing.workers.captions.base import ENDPOINT_FAULT_STATUSES
from src.processing.workers.transcripts.base import Heard, Segment, Transcriber, TranscriptError
from src.processing.workers.transcripts.speech import speech_seconds, time_allowed

CONNECT_TIMEOUT_SEC = 10
# Segments may end a little past the speech; far past it, the times aren't seconds.
END_SLACK_SEC = 30.0


class OpenAITranscriber(Transcriber):
    def __init__(self, api_url: str, api_key: str | None, model: str, *, timeout: float | None = None) -> None:
        self._url = f"{api_url.rstrip('/')}/audio/transcriptions"
        self._key = api_key
        self._model = model
        self._timeout = timeout

    def transcribe(self, speech_wav: Path) -> Heard:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        seconds = speech_seconds(speech_wav)
        allowed = self._timeout or time_allowed(seconds)
        try:
            with open(speech_wav, "rb") as audio:
                resp = requests.post(self._url, headers=headers, timeout=(CONNECT_TIMEOUT_SEC, allowed),
                                     files={"file": ("speech.wav", audio, "audio/wav")},
                                     data={"model": self._model, "response_format": "verbose_json",
                                           "timestamp_granularities[]": "segment",
                                           "vad_filter": "false", "without_timestamps": "false"})
        except requests.ConnectTimeout as e:
            raise TranscriptError(f"Couldn't reach {self._url}: it didn't answer.", endpoint_fault=True) from e
        except requests.ConnectionError as e:
            raise TranscriptError(f"Couldn't reach {self._url}: {type(e).__name__}.", endpoint_fault=True) from e
        except requests.Timeout as e:
            # Reached, but no answer in four times the speech: this clip, most likely
            # (charged once a check finds the machine fine).
            raise TranscriptError(f"{self._url} didn't finish {seconds:.0f} s of speech within {allowed:.0f} s.",
                                  endpoint_fault=False) from e
        except requests.RequestException as e:
            raise TranscriptError(f"{self._url} broke off: {type(e).__name__}.", endpoint_fault=True) from e
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
        if not isinstance(segments, list) or not segments:
            if str(data.get("text") or "").strip():
                # Text without times can't make a transcript: the server doesn't do verbose_json.
                raise TranscriptError(f"{self._url} said what it heard without times (it needs verbose_json).",
                                      endpoint_fault=True)
            segments = []
        try:
            heard = [Segment(float(s["start"]), float(s["end"]), str(s.get("text") or "")) for s in segments]
        except (KeyError, TypeError, ValueError) as e:
            raise TranscriptError(f"{self._url} sent segments without times.", endpoint_fault=True) from e
        if seconds and any(s.end > seconds + END_SLACK_SEC for s in heard):
            raise TranscriptError(f"{self._url} timed segments past the {seconds:.0f} s it was sent "
                                  "(its times aren't seconds).", endpoint_fault=True)
        return Heard(heard, str(data.get("language") or ""))

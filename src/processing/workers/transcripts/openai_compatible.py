"""A transcription server (Settings → AI): OpenAI's POST /audio/transcriptions,
as speaches and LocalAI serve it.

It's sent the speech alone (the worker found it: speech.py), as a 16 kHz
mono WAV, with the job's model by the name the server lists it as, and
asked for verbose_json: segments with their times, and the language. It's
asked not to skip silences of its own (vad_filter=false: none are left to
skip) and to time its segments (without_timestamps=false: speaches
otherwise answers in pieces of up to 30 s). Servers that don't know those
fields ignore them; current speaches skips short silences whatever it's told.
With a Language chosen, it's asked to hear that language: OpenAI's API
doesn't say how sure it was of another, as the built-in's does.

Its address is checked at each request and no redirect is followed
(src/shared/ai_requests.py). What it says when it fails is the log's: a
failure says what went wrong in Lumiverb's words, as it reaches every
signed-in person (the machine's status, why work waits, a clip's failure).
"""

from __future__ import annotations

from pathlib import Path

import requests

from src.processing.workers.captions.base import ENDPOINT_FAULT_STATUSES
from src.processing.workers.transcripts.base import (
    Heard,
    Segment,
    Transcriber,
    TranscriptError,
    chance,
)
from src.processing.workers.transcripts.speech import speech_seconds, time_allowed
from src.shared import ai_requests

CONNECT_TIMEOUT_SEC = 10
# Segments may end a little past the speech; far past it, the times aren't seconds.
END_SLACK_SEC = 30.0


class OpenAITranscriber(Transcriber):
    def __init__(self, api_url: str, api_key: str | None, model: str, *, fingerprint: str = "",
                 timeout: float | None = None) -> None:
        """fingerprint: the certificate an https server was added with (src/shared/ai_requests.py)."""
        self._url = f"{api_url.rstrip('/')}/audio/transcriptions"
        self._key = api_key
        self._model = model
        self._fingerprint = fingerprint
        self._timeout = timeout

    def transcribe(self, speech_wav: Path, language: str = "") -> Heard:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        seconds = speech_seconds(speech_wav)
        allowed = self._timeout or time_allowed(seconds)
        try:
            with open(speech_wav, "rb") as audio:
                resp = ai_requests.post(self._url, headers=headers, timeout=(CONNECT_TIMEOUT_SEC, allowed),
                                        fingerprint=self._fingerprint,
                                        files={"file": ("speech.wav", audio, "audio/wav")},
                                        data={"model": self._model, "response_format": "verbose_json",
                                              "timestamp_granularities[]": "segment",
                                              "vad_filter": "false", "without_timestamps": "false",
                                              **({"language": language} if language else {})})
        except (ai_requests.AddressRefused, ai_requests.NotItsCertificate, ai_requests.Redirected) as e:
            raise TranscriptError(str(e), endpoint_fault=True) from None
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
            fault = resp.status_code in ENDPOINT_FAULT_STATUSES or resp.status_code >= 500
            raise TranscriptError(f"{self._url} {ai_requests.answered(resp, model=self._model, api='transcription')}.",
                                  endpoint_fault=fault)
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
            heard = [Segment(float(s["start"]), float(s["end"]), str(s.get("text") or ""),
                             chance(s.get("no_speech_prob"))) for s in segments]
        except (KeyError, TypeError, ValueError) as e:
            raise TranscriptError(f"{self._url} sent segments without times.", endpoint_fault=True) from e
        if seconds and any(s.end > seconds + END_SLACK_SEC for s in heard):
            raise TranscriptError(f"{self._url} timed segments past the {seconds:.0f} s it was sent "
                                  "(its times aren't seconds).", endpoint_fault=True)
        return Heard(heard, str(data.get("language") or ""))

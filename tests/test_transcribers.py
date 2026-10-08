"""The machines that hear the speech (the transcripts job): the built-in
Whisper and transcription servers (OpenAI's /audio/transcriptions).

Each says what was said, timed in the speech it was sent, and whether a
failure was the machine's (its work goes to another machine; with none
left the job waits, and no clip is charged) or the clip's.
"""

from __future__ import annotations

import json
import subprocess
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.client.workers.transcripts.base import Heard, Segment, TranscriptError
from src.client.workers.transcripts.local import BuiltInWhisper, unavailable
from src.client.workers.transcripts.openai_compatible import OpenAITranscriber

pytestmark = pytest.mark.fast

VERBOSE = {"task": "transcribe", "language": "english", "duration": 2.5, "text": " The quick brown fox.",
           "segments": [{"id": 0, "seek": 0, "start": 0.0, "end": 2.4, "text": " The quick brown fox.",
                         "tokens": [1, 2], "temperature": 0.0, "avg_logprob": -0.2, "compression_ratio": 1.0,
                         "no_speech_prob": 0.01}]}


@pytest.fixture
def speech(tmp_path: Path) -> Path:
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF....WAVEfmt speech")
    return wav


class _Server:
    """A transcription server on localhost: answers each POST with what `answer` says."""

    def __init__(self, answer):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                head = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
                form = BytesParser(policy=HTTP).parsebytes(head + body)
                fields = {p.get_param("name", header="content-disposition"): p for p in form.iter_parts()}
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "fields": {k: (v.get_filename(), v.get_content_type(), v.get_payload(decode=True))
                                                  for k, v in fields.items()}})
                status, payload = answer
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_port}/v1"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        self._httpd.shutdown()


@pytest.fixture
def server():
    made = []

    def make(answer=(200, VERBOSE)):
        made.append(_Server(answer))
        return made[-1]

    yield make
    for s in made:
        s.close()


# ---------------------------------------------------------------------------
# A transcription server
# ---------------------------------------------------------------------------


def test_the_speech_goes_to_the_server_with_the_model_as_it_names_it(server, speech):
    s = server()
    heard = OpenAITranscriber(s.url + "/", "sk-1", "Systran/faster-whisper-small").transcribe(speech)
    assert heard == Heard([Segment(0.0, 2.4, " The quick brown fox.")], "english")
    [req] = s.requests
    assert req["path"] == "/v1/audio/transcriptions" and req["auth"] == "Bearer sk-1"
    fields = req["fields"]
    assert fields["file"] == ("speech.wav", "audio/wav", speech.read_bytes())
    assert fields["model"][2] == b"Systran/faster-whisper-small"
    assert fields["response_format"][2] == b"verbose_json"
    assert fields["timestamp_granularities[]"][2] == b"segment"
    # Nothing asks it to skip silences: the worker already did.
    assert not {k for k in fields if "vad" in k}


def test_no_key_no_authorization(server, speech):
    s = server()
    OpenAITranscriber(s.url, None, "small").transcribe(speech)
    assert s.requests[0]["auth"] is None


def test_nothing_said_is_no_segments(server, speech):
    s = server((200, {"text": "", "language": "en"}))
    assert OpenAITranscriber(s.url, None, "small").transcribe(speech) == Heard([], "en")


@pytest.mark.parametrize(("answer", "fault", "says"), [
    ((401, {"error": "bad key"}), True, "refused the key"),
    ((404, {"detail": "Model 'small' is not installed"}), True, "has no small"),
    ((429, {"error": "busy"}), True, "answered 429"),
    ((500, {"error": "CUDA out of memory"}), True, "CUDA out of memory"),
    ((503, b""), True, "answered 503."),
    # The server took it but couldn't make sense of this clip's audio.
    ((400, {"error": "audio too short"}), False, "audio too short"),
    ((413, {"error": "too large"}), False, "answered 413"),
    # It isn't speaking the API this needs.
    ((200, b"<html>hello</html>"), True, "OpenAI's JSON"),
    ((200, {"text": "said without times"}), True, "without times"),
    ((200, {"segments": [{"text": "no times"}]}), True, "without times"),
])
def test_a_failure_says_whose_it_is(server, speech, answer, fault, says):
    s = server(answer)
    with pytest.raises(TranscriptError) as e:
        OpenAITranscriber(s.url, None, "small").transcribe(speech)
    assert e.value.endpoint_fault is fault and says in str(e.value)


def test_a_server_that_isnt_there_or_doesnt_answer_is_the_machines_fault(speech):
    with pytest.raises(TranscriptError) as e:
        OpenAITranscriber("http://127.0.0.1:9/v1", None, "small").transcribe(speech)
    assert e.value.endpoint_fault and "Couldn't reach" in str(e.value)
    with patch("src.client.workers.transcripts.openai_compatible.requests.post", side_effect=requests.ReadTimeout()):
        with pytest.raises(TranscriptError) as e:
            OpenAITranscriber("http://brain/v1", None, "small").transcribe(speech)
    assert e.value.endpoint_fault and "didn't answer" in str(e.value)


# ---------------------------------------------------------------------------
# The built-in Whisper
# ---------------------------------------------------------------------------


def _ran(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def test_the_built_in_whisper_runs_the_model_on_the_speech_in_a_subprocess(speech):
    out = json.dumps({"segments": [{"start": 0.0, "end": 1.5, "text": " Hi."}], "language": "en"})
    with patch("subprocess.run", return_value=_ran(stdout=out)) as run:
        heard = BuiltInWhisper("small").transcribe(speech)
    assert heard == Heard([Segment(0.0, 1.5, " Hi.")], "en")
    cmd = run.call_args.args[0]
    assert cmd[1].endswith("transcripts/child.py") and cmd[2:] == ["transcribe", str(speech), "small", "auto"]


@pytest.mark.parametrize(("ran", "fault"), [
    # The model won't load (not downloaded, no room): the computer's trouble.
    (_ran(1, json.dumps({"error": "Couldn't load small: no space left", "stage": "load"})), True),
    (_ran(1, json.dumps({"error": "CUDA failed with error out of memory", "stage": "transcribe"})), True),
    # Died without a word (killed, a crash in the GPU code).
    (_ran(-9, "", "Killed"), True),
    # Whisper itself failed on this audio.
    (_ran(1, json.dumps({"error": "Invalid data found when processing input", "stage": "transcribe"})), False),
])
def test_a_built_in_failure_says_whose_it_is(speech, ran, fault):
    with patch("subprocess.run", return_value=ran):
        with pytest.raises(TranscriptError) as e:
            BuiltInWhisper("small").transcribe(speech)
    assert e.value.endpoint_fault is fault


def test_a_built_in_transcription_that_never_ends_is_the_clips(speech):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("python", 3600)):
        with pytest.raises(TranscriptError) as e:
            BuiltInWhisper("small").transcribe(speech)
    assert e.value.endpoint_fault is False


def test_the_built_in_whisper_needs_faster_whisper():
    assert unavailable() == ""
    with patch("importlib.util.find_spec", return_value=None):
        assert "faster-whisper isn't installed" in unavailable()

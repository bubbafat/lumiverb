"""A dropped connection or the API restarting doesn't fail a call that's
safe to send again (src/processing/api.py).

The real ApiClient over an httpx MockTransport whose answers each test
scripts: a connection reset, refused, or no answer in time, and nginx's
502/503/504 while the API restarts. A GET is tried again; a POST only when
its caller says it's safe to send twice; nothing after a 4xx or a 500; and
the tries stop after about half a minute.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import httpx
import pytest

from src.processing import api
from src.processing.api import RETRY_DELAYS, ApiClient, LumiverbAPIError

pytestmark = pytest.mark.fast

OK = {"ok": True}


def _reset() -> httpx.ReadError:
    return httpx.ReadError("[Errno 54] Connection reset by peer")


class Script:
    """Answers each request with the next of `answers` (an exception is
    raised, a status is answered), then 200 OK once they run out; records
    each request."""

    def __init__(self, *answers: int | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if not self.answers:
            return httpx.Response(200, json=OK)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"error": {"code": f"http_{answer}", "message": str(answer)}})

    @property
    def tries(self) -> int:
        return len(self.requests)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits between tries, recorded instead of slept."""
    waits: list[float] = []
    monkeypatch.setattr(api, "_sleep", waits.append)
    return waits


def _client(script: Callable[[httpx.Request], httpx.Response]) -> ApiClient:
    client = ApiClient("http://lumiverb.test", "key")
    client._client = httpx.Client(transport=httpx.MockTransport(script))
    return client


def test_a_get_is_tried_again_through_the_api_restarting(slept):
    script = Script(503, 502)
    assert _client(script).get("/v1/libraries").json() == OK
    assert script.tries == 3 and slept == [0.5, 1.0]


def test_a_safe_post_is_tried_again_through_a_reset_connection(slept):
    script = Script(_reset())
    assert _client(script).post("/v1/scans/scan_1/found", json={"files": []}, retry=True).json() == OK
    assert script.requests == [("POST", "/v1/scans/scan_1/found")] * 2 and slept == [0.5]


def test_a_post_not_said_to_be_safe_is_sent_once(slept):
    # An upload, a producer's save: sent twice it would be done twice.
    script = Script(_reset())
    with pytest.raises(httpx.ReadError):
        _client(script).post("/v1/ingest", data={"rel_path": "a.jpg"})
    script = Script(503)
    with pytest.raises(LumiverbAPIError) as err:
        _client(script).post("/v1/libraries/lib_1/scans", json={})
    assert err.value.status_code == 503
    assert script.tries == 1 and slept == []


def test_a_call_refused_or_unanswered_in_time_is_tried_again(slept):
    for failure in (httpx.ConnectError("[Errno 61] Connection refused"), httpx.ReadTimeout("timed out"),
                    httpx.RemoteProtocolError("Server disconnected without sending a response.")):
        script = Script(failure)
        assert _client(script).raw("POST", "/v1/scans/scan_1/finish", json={}, retry=True).json() == OK
        assert script.tries == 2


def test_the_tries_stop_after_about_half_a_minute(slept):
    script = Script(*[503] * 20)
    with pytest.raises(LumiverbAPIError) as err:
        _client(script).get("/v1/libraries")
    assert err.value.status_code == 503
    assert slept == list(RETRY_DELAYS) and script.tries == len(RETRY_DELAYS) + 1
    assert 25 <= sum(slept) <= 35
    script = Script(*[_reset()] * 20)
    with pytest.raises(httpx.ReadError):
        _client(script).post("/v1/scans/scan_1/hashes", json={"files": []}, retry=True)
    assert script.tries == len(RETRY_DELAYS) + 1


def test_a_call_that_fails_slowly_is_tried_once_more_not_six_times(monkeypatch: pytest.MonkeyPatch):
    # No answer within the timeout (two minutes): another two minutes is
    # worth one more try, not twelve more minutes.
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(api, "_sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))

    def slow(request: httpx.Request) -> httpx.Response:
        clock.now += 120
        script.requests.append((request.method, request.url.path))
        raise httpx.ReadTimeout("timed out")

    script = Script()
    with pytest.raises(httpx.ReadTimeout):
        _client(slow).get("/v1/libraries")
    assert script.tries == 2


def test_an_answer_from_the_api_itself_is_never_tried_again(slept):
    for status in (400, 404, 409, 422, 500):
        script = Script(status)
        with pytest.raises(LumiverbAPIError):
            _client(script).post("/v1/scans/scan_1/finish", json={}, retry=True)
        assert script.tries == 1
        script = Script(status)
        assert _client(script).raw("GET", "/v1/scans/scan_1/plan").status_code == status
        assert script.tries == 1
    assert slept == []


def test_raw_tries_a_get_again_and_another_method_only_when_said(slept):
    script = Script(_reset())
    assert _client(script).raw("GET", "/v1/libraries").status_code == 200
    assert script.tries == 2
    script = Script(_reset())
    with pytest.raises(httpx.ReadError):
        _client(script).raw("DELETE", "/v1/scans/scan_1")
    assert script.tries == 1


def test_a_stream_is_tried_again_until_it_starts(slept):
    script = Script(502, _reset())
    with _client(script).stream("/v1/assets/a/artifacts/lumiverb.video.analysis_proxy") as resp:
        assert resp.status_code == 200 and resp.read() == b'{"ok":true}'
    assert script.tries == 3
    script = Script(403)
    with pytest.raises(LumiverbAPIError) as err, _client(script).stream("/v1/assets/a/proxy"):
        pass
    assert err.value.code == "http_403" and script.tries == 1


def test_a_producers_saves_are_never_sent_twice(slept):
    """Saving (src/producers/runner.py) passes a write on as it's given: a
    producer's POST, never said to be safe, is sent once."""
    from src.producers.runner import Saving

    script = Script(_reset(), _reset())
    saving = Saving(_client(script))
    with pytest.raises(httpx.ReadError):
        saving.post("/v1/assets/batch-vision", json={"items": [{"asset_id": "a", "description": "x"}]})
    assert script.tries == 1
    assert saving.get("/v1/producers").json() == OK  # a read is tried again
    assert script.tries == 3


def test_the_cli_says_it_tries_again(slept, capsys):
    script = Script(503)
    client = _client(script)
    client.print_errors = True
    client.get("/v1/libraries")
    assert "trying again in 0.5 s" in capsys.readouterr().err

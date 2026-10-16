"""What an error says (src/processing/api.py _error_of): the API's own
envelope as it says it; anything else (a proxy's page while the API
restarts, another server at the address) by its status and where it came
from, never a page of HTML. The real ApiClient over an httpx MockTransport."""

from __future__ import annotations

import httpx
import pytest

from src.processing.api import ApiClient, LumiverbAPIError

pytestmark = pytest.mark.fast

NGINX_502 = """<html>
<head><title>502 Bad Gateway</title></head>
<body>
<center><h1>502 Bad Gateway</h1></center>
<hr><center>nginx/1.29.1</center>
</body>
</html>
"""


def _error(response: httpx.Response, *, printed: bool = False) -> LumiverbAPIError:
    client = ApiClient("http://localhost:6464", "key")
    client._client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
    client.print_errors = printed
    with pytest.raises(LumiverbAPIError) as err:
        client.post("/v1/upkeep/cleanup")  # sent once: what came back is what's said
    return err.value


def test_the_apis_own_error_is_said_as_it_said_it():
    e = _error(httpx.Response(401, json={"error": {"code": "unauthorized",
                                                     "message": "Admin key or tenant API key required"}}))
    assert (e.code, e.message, e.status_code) == ("unauthorized", "Admin key or tenant API key required", 401)


def test_a_proxys_page_while_the_api_restarts_is_one_line(capsys):
    e = _error(httpx.Response(502, text=NGINX_502, headers={"content-type": "text/html"}), printed=True)
    assert e.message == ("HTTP 502 Bad Gateway from http://localhost:6464: Lumiverb isn't answering behind it"
                         " (starting up?). Try again in a moment.")
    err = capsys.readouterr().err
    assert err == f"Error [unknown]: {e.message}\n" and "<" not in err


def test_another_servers_page_isnt_taken_for_lumiverbs():
    e = _error(httpx.Response(404, text="<!doctype html><h1>Not Found</h1>", headers={"content-type": "text/html"}))
    assert e.message == "HTTP 404 Not Found from http://localhost:6464, not an answer from Lumiverb's API: is that where it is?"


@pytest.mark.parametrize(("body", "said"), [
    ("Internal Server Error", "HTTP 500 Internal Server Error from http://localhost:6464: Internal Server Error"),
    ("", "HTTP 500 Internal Server Error from http://localhost:6464, not an answer from Lumiverb's API: is that where it is?"),
    ("x" * 301, "HTTP 500 Internal Server Error from http://localhost:6464, not an answer from Lumiverb's API: is that where it is?"),
    ("one\ntwo", "HTTP 500 Internal Server Error from http://localhost:6464, not an answer from Lumiverb's API: is that where it is?"),
])
def test_a_short_line_of_text_is_said_and_nothing_longer(body, said):
    assert _error(httpx.Response(500, text=body)).message == said

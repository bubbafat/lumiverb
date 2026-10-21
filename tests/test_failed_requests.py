"""What a browser couldn't load at all (POST /v1/me/failed-requests): the connection dropped, so the server
never saw the request, and the web app sends it once one gets through. Each is a line in the log (Admin ›
Logs), with who and their browser; only plain one-line text gets in, from a person signed in, a few at a
time."""

from __future__ import annotations

import logging

import pytest

from src.server.api import rate_limit
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture install needs)
    control,
)
from tests.test_reset_ends_the_old_password import (  # noqa: F401 (install: a fixture)
    ROBERT,
    _bearer,
    _new_key,
    install,
)

pytestmark = pytest.mark.slow

IPHONE_CHROME = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) CriOS/130.0 Mobile/15E148 Safari/604.1"
FAILED = {"method": "GET", "path": "/v1/projects", "error": "Load failed", "at": "2026-10-21T09:14:03Z",
          "page": "/projects"}


@pytest.fixture(autouse=True)
def _sends_counted_afresh(monkeypatch):
    monkeypatch.setattr(rate_limit.failed_requests_limiter, "_hits", {})


def _send(client, token: str, *failed: dict, ua: str = IPHONE_CHROME):
    return client.post("/v1/me/failed-requests", json={"failed": list(failed)},
                       headers={**_bearer(token), "User-Agent": ua})


def _logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "src.server.api.routers.me"]


def test_each_is_a_line_in_the_log_with_who_and_their_browser(install, caplog):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    caplog.set_level(logging.WARNING, logger="src.server.api.routers.me")
    other = {**FAILED, "method": "PUT", "path": "/v1/assets/ast_01/rating", "error": "Failed to fetch", "page": ""}
    r = _send(client, robert, FAILED, other)
    assert r.status_code == 204, r.text
    assert _logged(caplog) == [
        f"web: {ROBERT}'s browser couldn't reach the server for GET /v1/projects at 2026-10-21T09:14:03+00:00:"
        f" Load failed (on /projects; {IPHONE_CHROME})",
        f"web: {ROBERT}'s browser couldn't reach the server for PUT /v1/assets/ast_01/rating at"
        f" 2026-10-21T09:14:03+00:00: Failed to fetch (on ?; {IPHONE_CHROME})",
    ]


@pytest.mark.parametrize("bad", [
    {"error": "Load failed\nweb: someone else's line"},  # a forged line
    {"error": "\x1b[31mred\x1b[0m"},  # an escape that colors a terminal
    {"path": "/v1/projects?q=private words"},  # a search's words stay in the browser
    {"path": "https://elsewhere.example/v1/x"},
    {"path": "/v1/" + "x" * 200},
    {"method": "TRACE"},
    {"page": "/projects\r\n"},
])
def test_only_plain_one_line_text_gets_in(install, caplog, bad):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    caplog.set_level(logging.WARNING, logger="src.server.api.routers.me")
    r = _send(client, robert, {**FAILED, **bad})
    assert r.status_code == 422, r.text
    assert _logged(caplog) == []


def test_twenty_at_a_time_and_a_browser_that_says_nothing_of_itself(install, caplog):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    caplog.set_level(logging.WARNING, logger="src.server.api.routers.me")
    assert _send(client, robert, *[FAILED] * 21).status_code == 422
    assert _send(client, robert).status_code == 422
    assert _send(client, robert, FAILED, ua="").status_code == 204
    assert _logged(caplog)[-1].endswith("(on /projects; no browser said)")


def test_a_person_signed_in_never_a_key(install):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    key = _new_key(client, robert)
    refused = _send(client, key, FAILED)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "signed_in_only", refused.text
    assert client.post("/v1/me/failed-requests", json={"failed": [FAILED]}).status_code == 401


def test_a_script_filling_the_log_is_held_back(install):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    for _ in range(rate_limit.failed_requests_limiter.max_requests):
        assert _send(client, robert, FAILED).status_code == 204
    assert _send(client, robert, FAILED).status_code == 429

"""Changing your own password, signed in (POST /v1/me/password, Settings ›
Account › Change password): only with the password you have, never with an
API key; it ends what the old one could have made, as a reset does (every
other session, the API keys you made, your links not yet used) after saying
which keys end; and this browser stays signed in, with a new session."""

from __future__ import annotations

import pytest

from src.server.api import rate_limit
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture install needs)
    PASSWORD,
    control,
)
from tests.test_reset_ends_the_old_password import (  # noqa: F401 (install: a fixture)
    NEW,
    ROBERT,
    _bearer,
    _login,
    _new_key,
    _works,
    install,
)

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _changes_counted_afresh(monkeypatch):
    monkeypatch.setattr(rate_limit.change_password_limiter, "_hits", {})


def _change(client, token: str, current: str = PASSWORD, new: str = NEW, **more):
    return client.post(
        "/v1/me/password",
        json={"current_password": current, "new_password": new, **more},
        headers=_bearer(token),
    )


def _signs_in(client, password: str) -> int:
    return client.post("/v1/auth/login", json={"email": ROBERT, "password": password}).status_code


def test_a_person_changes_their_own_password_and_stays_signed_in_here(install):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    elsewhere = _login(client, ROBERT, PASSWORD)

    changed = _change(client, robert)
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["api_keys_revoked"] == 0 and body["token_type"] == "bearer"
    here = body["access_token"]
    assert client.get("/v1/me", headers=_bearer(here)).json()["user_id"], (
        "this browser's new session works"
    )
    assert _works(client, robert) == 401 and _works(client, elsewhere) == 401, (
        "every other session has ended"
    )
    assert _signs_in(client, NEW) == 200 and _signs_in(client, PASSWORD) == 401


def test_only_with_the_password_they_have_and_a_wrong_one_signs_nobody_out(install):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    wrong = _change(client, robert, current="not my password at all")
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "wrong_password", (
        wrong.text
    )
    assert _works(client, robert) == 200, "a 400, not a 401: the browser isn't signed out"
    assert _signs_in(client, PASSWORD) == 200 and _signs_in(client, NEW) == 401, "nothing changed"


def test_the_keys_it_would_end_are_said_first(install):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    key = _new_key(client, robert)

    asked = _change(client, robert)
    assert asked.status_code == 409, asked.text
    error = asked.json()["error"]
    assert error["code"] == "keys_end_with_password" and "1 API key you made" in error["message"]
    assert [(k["label"], k["device"]) for k in error["details"]["keys"]] == [("cli", False)]
    assert _works(client, key) == 200 and _signs_in(client, PASSWORD) == 200, (
        "nothing changed until answered"
    )

    changed = _change(client, robert, end_api_keys=True)
    assert changed.status_code == 200 and changed.json()["api_keys_revoked"] == 1, changed.text
    assert _works(client, key) == 401
    assert _signs_in(client, NEW) == 200


def test_never_with_an_api_key(install):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    key = _new_key(client, robert)
    refused = _change(client, key)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "signed_in_only", (
        refused.text
    )
    assert _signs_in(client, PASSWORD) == 200


@pytest.mark.parametrize("new", ["eleven char", "x" * 73, "é" * 37])
def test_the_new_password_is_12_to_72_characters(install, new):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    refused = _change(client, robert, new=new)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "password_length", (
        refused.text
    )
    assert _signs_in(client, PASSWORD) == 200


def test_guessing_the_current_password_is_held_back(install):  # noqa: F811 (install: a fixture)
    client, _, robert = install
    for _ in range(rate_limit.change_password_limiter.max_requests):
        assert _change(client, robert, current="a guess at it, wrong").status_code == 400
    assert _change(client, robert).status_code == 429, "even the right one, until the minute is up"
    assert _signs_in(client, PASSWORD) == 200

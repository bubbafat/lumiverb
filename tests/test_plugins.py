"""Plug-ins (ADR-021) against the real API and database: each account's
switch, what turning one on asks (its acknowledgment, shown and accepted by
version, every time it's turned on), every acceptance kept, a new version
of the text turning it off until it's accepted again, account admins only,
and the CLI showing the text before it accepts."""

from __future__ import annotations

import dataclasses
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

import src.plugins
from src.client.cli.client import LumiverbClient
from src.client.cli.main import app as cli_app
from src.plugins import registry
from src.server.repository.plugins import PluginRepository
from tests.test_library_access import PASSWORD, _person
from tests.test_scans_api import env  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.slow
runner = CliRunner()
GEONAMES = "geonames"


@pytest.fixture(autouse=True)
def _off(env):  # noqa: F811
    """Every test starts with every plug-in off and no acceptance kept, in both accounts."""
    yield
    for headers in (env.admin, env.other):
        for p in registry():
            assert env.client.put(f"/v1/plugins/{p}", json={"enabled": False}, headers=headers).status_code == 200
    with env.db.begin() as conn:
        conn.execute(text("DELETE FROM plugin_acknowledgments"))


def _plugins(env, headers) -> dict:  # noqa: F811
    r = env.client.get("/v1/plugins", headers=headers)
    assert r.status_code == 200, r.text
    return {p["plugin_id"]: p for p in r.json()["plugins"]}


def _version() -> str:
    return registry()[GEONAMES].acknowledgment.version


def _accepted(env) -> list[tuple]:  # noqa: F811
    with env.db.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT plugin_id, version, user_id FROM plugin_acknowledgments ORDER BY id"))]


def _signed_in(env) -> tuple[dict, str, str]:  # noqa: F811
    """An account admin signed in (a JWT), their user id and email."""
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    user_id = _person(env, email, "admin")
    r = env.client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, user_id, email


def test_geonames_is_the_one_plug_in_and_asks_its_license_first() -> None:
    assert list(registry()) == [GEONAMES]
    spec = registry()[GEONAMES]
    ack = spec.acknowledgment
    assert ack is not None and ack.version and "CC BY 4.0" in ack.text
    assert "download.geonames.org" in ack.text and "Nothing about your photos" in ack.text
    assert {link.url for link in ack.links} >= {"https://www.geonames.org/",
                                                "https://creativecommons.org/licenses/by/4.0/"}


def test_turning_one_on_asks_its_acknowledgment_and_keeps_who_accepted(env) -> None:  # noqa: F811
    listed = _plugins(env, env.admin)[GEONAMES]
    assert listed["state"] == "off" and listed["acknowledged"] is None
    assert listed["acknowledgment"]["version"] == _version() and listed["acknowledgment"]["links"]

    headers, user_id, email = _signed_in(env)
    for body in ({"enabled": True}, {"enabled": True, "acknowledged": "not-the-version"}):
        r = env.client.put(f"/v1/plugins/{GEONAMES}", json=body, headers=headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "acknowledgment_required", r.text
        details = r.json()["error"]["details"]
        assert details["plugin_id"] == GEONAMES and details["version"] == _version()
        assert details["text"] == listed["acknowledgment"]["text"] and details["links"]
    assert _plugins(env, env.admin)[GEONAMES]["state"] == "off" and _accepted(env) == []

    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": _version()},
                       headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "on", r.text
    assert r.json()["acknowledged"]["by"] == email and r.json()["acknowledged"]["version"] == _version()
    assert _accepted(env) == [(GEONAMES, _version(), user_id)]
    # Others see that it's on, and which version, not who.
    seen = _plugins(env, env.viewer)[GEONAMES]
    assert seen["state"] == "on" and seen["acknowledged"]["version"] == _version()
    assert seen["acknowledged"]["by"] is None

    # On already: nothing asked, nothing more kept.
    assert env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True}, headers=headers).status_code == 200
    assert len(_accepted(env)) == 1

    # Off asks nothing; the acceptance stays kept; turned on again, it asks again.
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": False}, headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "off" and r.json()["acknowledged"] is None
    assert len(_accepted(env)) == 1
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "acknowledgment_required"
    _ = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": _version()},
                       headers=headers)
    assert len(_accepted(env)) == 2


def test_a_new_version_of_its_text_turns_it_off_until_accepted_again(env, monkeypatch) -> None:  # noqa: F811
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": _version()},
                       headers=env.admin)
    assert r.status_code == 200, r.text
    old = registry()[GEONAMES]
    newer = dataclasses.replace(old, acknowledgment=dataclasses.replace(old.acknowledgment, version="next",
                                                                        text=old.acknowledgment.text + "\n\nMore."))
    monkeypatch.setitem(src.plugins.registry(), GEONAMES, newer)

    listed = _plugins(env, env.admin)[GEONAMES]
    assert listed["state"] == "waiting" and listed["acknowledged"]["version"] == old.acknowledgment.version
    assert listed["acknowledgment"]["version"] == "next"
    with env.db.connect() as conn:
        from sqlmodel import Session

        assert PluginRepository(Session(bind=conn)).is_on(GEONAMES) is False
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True}, headers=env.admin)
    assert r.status_code == 409 and r.json()["error"]["details"]["version"] == "next"
    assert r.json()["error"]["details"]["text"].endswith("More.")
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": "next"}, headers=env.admin)
    assert r.status_code == 200 and r.json()["state"] == "on"
    assert [v for _, v, _ in _accepted(env)] == [old.acknowledgment.version, "next"]


def test_each_account_its_own_and_only_its_admins_turn_one_on(env) -> None:  # noqa: F811
    for headers in (env.editor, env.viewer):
        r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": _version()},
                           headers=headers)
        assert r.status_code == 403, r.text
    assert env.client.put("/v1/plugins/nope", json={"enabled": True}, headers=env.admin).status_code == 404
    assert env.client.get("/v1/plugins").status_code == 401

    assert env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": _version()},
                          headers=env.admin).status_code == 200
    assert _plugins(env, env.admin)[GEONAMES]["state"] == "on"
    assert _plugins(env, env.other)[GEONAMES]["state"] == "off"


class _Through:
    """The CLI's client's connection, sent through the test's API with a key."""

    def __init__(self, env, headers: dict) -> None:  # noqa: F811
        self._env, self._headers = env, headers

    def request(self, method: str, url: str, **kw):
        return self._env.client.request(method, url, headers={**self._headers, **(kw.pop("headers", None) or {})},
                                        **kw)

    def close(self) -> None:
        pass


def _cli(env, *args: str, confirm: bool | None = None):  # noqa: F811
    client = LumiverbClient(base_url="http://testserver", token="unused")
    client._client = _Through(env, env.admin)  # type: ignore[assignment]
    with patch("src.client.cli.commands.plugins.LumiverbClient", return_value=client), \
            patch("typer.confirm", return_value=bool(confirm)) as asked:
        result = runner.invoke(cli_app, list(args))
    return result, asked.called


def test_the_cli_shows_the_text_before_it_accepts(env) -> None:  # noqa: F811
    r, asked = _cli(env, "plugins", "enable", GEONAMES, confirm=False)
    assert r.exit_code == 0 and asked and "Left off" in r.output, r.output
    assert "CC BY 4.0" in r.output and "creativecommons.org/licenses/by/4.0" in r.output
    assert _plugins(env, env.admin)[GEONAMES]["state"] == "off" and _accepted(env) == []

    r, asked = _cli(env, "plugins", "enable", GEONAMES, confirm=True)
    assert r.exit_code == 0 and asked and "is on" in r.output, r.output
    r, _ = _cli(env, "plugins", "list")
    assert r.exit_code == 0 and GEONAMES in r.output and "version" in r.output, r.output
    r, _ = _cli(env, "plugins", "disable", GEONAMES)
    assert r.exit_code == 0 and _plugins(env, env.admin)[GEONAMES]["state"] == "off", r.output

    r, asked = _cli(env, "plugins", "enable", GEONAMES, "--accept")
    assert r.exit_code == 0 and not asked and "CC BY 4.0" in r.output and "is on" in r.output, r.output
    assert len(_accepted(env)) == 2

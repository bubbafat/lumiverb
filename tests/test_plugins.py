"""Each account's plug-ins (ADR-021's switch, ADR-023's installed plug-ins)
against the real API and database, with GeoNames, Lumiverb's own (installed
as migrating installs it, src/server/own_plugins.py): each account's switch,
what turning one on asks (its acknowledgment, shown and accepted by version,
every time it's turned on), every acceptance kept, a new version of the text
turning it off until it's accepted again only when the plug-in says so
(reaccept_from), account admins only, and the CLI showing the text before it
accepts."""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from src.client.cli.client import LumiverbClient
from src.client.cli.main import app as cli_app
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.plugin_catalog import find
from src.server.repository.plugins import PluginRepository, on_sql
from tests.test_library_access import PASSWORD, _person
from tests.test_scans_api import env  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.slow
runner = CliRunner()
GEONAMES = "geonames"


@pytest.fixture(scope="module", autouse=True)
def _installed(env, tmp_path_factory):  # noqa: F811
    """GeoNames installed on this module's server, as migrating installs it (off by default)."""
    from src.server.own_plugins import install_own

    install_own(get_settings().control_plane_database_url, tmp_path_factory.mktemp("data"), say=lambda _: None)
    with get_control_session() as control:
        shipped = control.execute(text("SELECT manifest FROM installed_plugins WHERE plugin_id = :p"),
                                  {"p": GEONAMES}).scalar()
    assert shipped is not None
    yield shipped


@pytest.fixture(autouse=True)
def _off(env, _installed):  # noqa: F811
    """Every test starts with GeoNames off and no acceptance kept, in both accounts, as it was installed."""
    yield
    for headers in (env.admin, env.other):
        assert env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": False}, headers=headers).status_code == 200
    with env.db.begin() as conn:
        conn.execute(text("DELETE FROM plugin_acknowledgments"))
    _manifest(_installed)


def _manifest(manifest: dict) -> None:
    with get_control_session() as control:
        control.execute(text("UPDATE installed_plugins SET manifest = CAST(:m AS jsonb) WHERE plugin_id = :p"),
                        {"m": json.dumps(manifest), "p": GEONAMES})
        control.commit()


def _plugins(env, headers) -> dict:  # noqa: F811
    r = env.client.get("/v1/plugins", headers=headers)
    assert r.status_code == 200, r.text
    return {p["plugin_id"]: p for p in r.json()["plugins"]}


def _version() -> str:
    return find(GEONAMES).spec.acknowledgment.version


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


def test_geonames_is_installed_and_asks_its_license_first() -> None:
    entry = find(GEONAMES)
    assert entry is not None and entry.installed is not None and entry.installed.on_by_default is False
    ack = entry.spec.acknowledgment
    assert ack is not None and ack.version == "3" and ack.reaccept_from == "1" and "CC BY 4.0" in ack.text
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


def _with_text(installed: dict, version: str, reaccept_from: str) -> None:
    """GeoNames as a later release would ship it: its text at this version, accepted again from that."""
    ack = dict(installed["acknowledgment"], version=version, reaccept_from=reaccept_from,
               text=installed["acknowledgment"]["text"] + "\n\nMore.")
    if not reaccept_from:
        ack.pop("reaccept_from")
    _manifest({**installed, "acknowledgment": ack})


def _on_in_sql(env) -> bool:  # noqa: F811
    """What the producers' queue asks (its work's applies: src/server/plugin_producers.py), as the database
    answers it."""
    entry = find(GEONAMES)
    with env.db.connect() as conn:
        return bool(conn.execute(text(f"SELECT {on_sql(GEONAMES, spec=entry.spec, default=entry.installed.default)}"))
                    .scalar())


def _on_in_python(env) -> bool:  # noqa: F811
    with env.db.connect() as conn:
        from sqlmodel import Session

        return PluginRepository(Session(bind=conn)).is_on(GEONAMES)


def test_a_new_version_of_its_text_stays_on_unless_the_plug_in_says_accept_again(env, _installed) -> None:  # noqa: F811
    _with_text(_installed, "1", reaccept_from="")  # as a first release declares it
    first = _version()
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": first},
                       headers=env.admin)
    assert r.status_code == 200, r.text
    assert _on_in_sql(env) and _on_in_python(env)

    # Its wording changed, the same terms: what was accepted still stands.
    _with_text(_installed, "2", reaccept_from="")
    listed = _plugins(env, env.admin)[GEONAMES]
    assert listed["state"] == "on" and listed["acknowledged"]["version"] == first
    assert listed["acknowledgment"]["version"] == "2"
    assert _on_in_sql(env) and _on_in_python(env)
    # On already: nothing asked, nothing more kept.
    assert env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True}, headers=env.admin).status_code == 200
    assert [v for _, v, _ in _accepted(env)] == [first]

    # Its terms changed: it says so, and it's off (waiting) until the new text is accepted.
    _with_text(_installed, "3", reaccept_from="3")
    listed = _plugins(env, env.admin)[GEONAMES]
    assert listed["state"] == "waiting" and listed["acknowledged"]["version"] == first
    assert not _on_in_sql(env) and not _on_in_python(env)
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True}, headers=env.admin)
    assert r.status_code == 409 and r.json()["error"]["details"]["version"] == "3"
    assert r.json()["error"]["details"]["text"].endswith("More.")
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": "3"}, headers=env.admin)
    assert r.status_code == 200 and r.json()["state"] == "on"
    assert [v for _, v, _ in _accepted(env)] == [first, "3"]
    assert _on_in_sql(env) and _on_in_python(env)

    # A later wording keeps the version that asked: accepting 3 stands for 4 (and 10: numbers, not text).
    for later in ("4", "10"):
        _with_text(_installed, later, reaccept_from="3")
        assert _plugins(env, env.admin)[GEONAMES]["state"] == "on" and _on_in_sql(env) and _on_in_python(env)


def test_a_server_that_skipped_the_release_that_asked_still_asks(env, _installed) -> None:  # noqa: F811
    _with_text(_installed, "1", reaccept_from="")
    r = env.client.put(f"/v1/plugins/{GEONAMES}", json={"enabled": True, "acknowledged": _version()},
                       headers=env.admin)
    assert r.status_code == 200, r.text
    # Version 2 asked again (reaccept_from "2"); this server goes from 1 straight to 2.1, which only rewords.
    _with_text(_installed, "2.1", reaccept_from="2")
    assert _plugins(env, env.admin)[GEONAMES]["state"] == "waiting"
    assert not _on_in_sql(env) and not _on_in_python(env)


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

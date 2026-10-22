# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Setting roles per library, and a library's admin (ADR-018 decision 8,
phase 8; src/server/api/routers/library_people.py, routers/libraries.py,
routers/users.py, `lumiverb library people|role|private`, `lumiverb user
libraries`).

Against the real API and databases (tests/test_scans_api.py's accounts): an
account admin sets who has which role on a library; a library's admin does
the same for theirs, pauses its scans and makes it private, and nothing of
another library; where its files are and who reads them stay an account
admin's; what they set takes effect on the next request. The CLI's commands
run against the same API.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from src.client.cli.client import LumiverbClient
from src.client.cli.main import app as cli_app
from src.server.config import get_settings
from tests.test_library_access import PASSWORD, _key, _person
from tests.test_scans_api import (  # noqa: F401 — the shared fixture
    File,
    _error,
    _library,
    _ok,
    _scan,
    env,
)

pytestmark = pytest.mark.slow
runner = CliRunner()


def _people(env, library_id: str, headers: dict) -> dict:
    return {p["email"]: p for p in _ok(env.client.get(f"/v1/libraries/{library_id}/people",
                                                      headers=headers))["people"]}


def _role(env, library_id: str, user_id: str, role, headers: dict):
    return env.client.put(f"/v1/libraries/{library_id}/people/{user_id}", json={"role": role}, headers=headers)


def _sees(env, headers: dict) -> set[str]:
    return {lib["library_id"] for lib in _ok(env.client.get("/v1/libraries", headers=headers))}


@pytest.fixture
def team(env, monkeypatch):
    """Libraries A and B; an account editor who will be A's admin, signed in
    (a key they make is an editor's, never above its own role: it edits A,
    it doesn't run it); an account viewer with a key."""
    monkeypatch.setattr(get_settings(), "jwt_secret", "a-test-secret-long-enough-for-hs256-signing")
    tag = uuid.uuid4().hex[:8]
    a, b = _library(env), _library(env)
    lead_email, member_email = f"lead-{tag}@example.com", f"member-{tag}@example.com"
    lead_id, member_id = _person(env, lead_email, "editor"), _person(env, member_email, "viewer")
    token = _ok(env.client.post("/v1/auth/login", json={"email": lead_email, "password": PASSWORD}))["access_token"]
    return SimpleNamespace(a=a, b=b, lead_id=lead_id, member_id=member_id, lead_email=lead_email,
                           member_email=member_email, lead={"Authorization": f"Bearer {token}"},
                           lead_key=_key(env, lead_id, "editor"), member=_key(env, member_id, "viewer"))


def test_an_account_admin_sets_who_has_which_role_on_a_library(env, team):
    people = _people(env, team.a, env.admin)
    assert people[team.member_email]["role"] == "viewer" and people[team.member_email]["set_role"] is None
    set_ = _ok(_role(env, team.a, team.member_id, "editor", env.admin))
    assert (set_["role"], set_["set_role"]) == ("editor", "editor")
    assert _people(env, team.a, env.admin)[team.member_email]["role"] == "editor"
    _ok(_role(env, team.a, team.member_id, "none", env.admin))
    assert team.a not in _sees(env, team.member), "they no longer see it, at once"
    _ok(_role(env, team.a, team.member_id, None, env.admin))
    assert team.a in _sees(env, team.member), "the rule's again: their account role"

    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    admin_id = _person(env, admin_email, "admin")
    _error(_role(env, team.a, admin_id, "none", env.admin), 409, "account_admin")
    assert env.client.put(f"/v1/libraries/{team.a}/people/usr_nope", json={"role": "viewer"},
                          headers=env.admin).status_code == 404
    assert _role(env, team.a, team.member_id, "owner", env.admin).status_code == 422
    other_account = env.client.get(f"/v1/libraries/{team.a}/people", headers=env.other)
    assert other_account.status_code == 404


def test_a_librarys_admin_runs_their_library_and_nothing_else(env, team):
    _ok(_role(env, team.a, team.lead_id, "admin", env.admin))
    libraries = {lib["library_id"]: lib for lib in _ok(env.client.get("/v1/libraries", headers=team.lead))}
    assert libraries[team.a]["role"] == "admin" and libraries[team.b]["role"] == "editor"

    _ok(_role(env, team.a, team.member_id, "editor", team.lead))  # its people
    _ok(env.client.patch(f"/v1/libraries/{team.a}", json={"private": True}, headers=team.lead))
    assert _ok(env.client.get(f"/v1/libraries/{team.a}", headers=team.lead))["private"] is True
    _ok(env.client.patch(f"/v1/libraries/{team.a}", json={"infer_location": True}, headers=team.lead))
    assert _ok(env.client.get(f"/v1/libraries/{team.a}", headers=team.lead))["infer_location"] is True
    _ok(env.client.post("/v1/libraries/scans/pause", json={"library_ids": [team.a]}, headers=team.lead), 204)
    _ok(env.client.post("/v1/libraries/scans/resume", json={"library_ids": [team.a]}, headers=team.lead), 204)

    for r in (env.client.get(f"/v1/libraries/{team.b}/people", headers=team.lead),
              _role(env, team.b, team.member_id, "none", team.lead),
              env.client.patch(f"/v1/libraries/{team.b}", json={"private": True}, headers=team.lead),
              env.client.patch(f"/v1/libraries/{team.b}", json={"infer_location": True}, headers=team.lead),
              env.client.post("/v1/libraries/scans/pause", json={"library_ids": [team.a, team.b]}, headers=team.lead),
              env.client.post("/v1/libraries/scans/pause", json={"all": True}, headers=team.lead)):
        _error(r, 403, "library_admin_required")
    # Not its editors' either.
    assert env.client.patch(f"/v1/libraries/{team.a}", json={"infer_location": False},
                            headers=team.member).status_code == 403
    # What's the account's stays an account admin's: making it public, its previews' place, new libraries.
    assert env.client.patch(f"/v1/libraries/{team.a}", json={"is_public": True},
                            headers=team.lead).status_code == 403
    assert env.client.get(f"/v1/libraries/{team.a}/previews", headers=team.lead).status_code == 403
    _error(env.client.get(f"/v1/libraries/{team.a}/people", headers=team.member), 403, "library_admin_required")
    _error(env.client.get(f"/v1/libraries/{team.a}/people", headers=team.lead_key), 403, "library_admin_required")


def test_where_its_files_are_and_who_reads_them_are_an_account_admins(env, team):
    """A library's own admin who isn't an account admin sees its folder and its
    reader and changes neither, as they make no library: a folder or a device
    is the account's to give. An account admin chooses both, signed in or
    with their key."""
    _ok(_role(env, team.a, team.lead_id, "admin", env.admin))
    before = _ok(env.client.get(f"/v1/libraries/{team.a}", headers=team.lead))
    assert (before["role"], before["reader"]) == ("admin", "server")
    device = _ok(env.client.post("/v1/devices", json={"name": f"lead's {team.a}"}, headers=env.admin))

    elsewhere = "/Volumes/elsewhere"
    for r in (env.client.patch(f"/v1/libraries/{team.a}", json={"root_path": elsewhere}, headers=team.lead),
              env.client.patch(f"/v1/libraries/{team.a}", json={"root_path": elsewhere, "private": True},
                               headers=team.lead),
              env.client.put(f"/v1/libraries/{team.a}/reader", json={"reader": device["device_id"]}, headers=team.lead),
              env.client.put(f"/v1/libraries/{team.a}/reader", json={"reader": "server", "root_path": elsewhere},
                             headers=team.lead),
              # Nor on a library they edit.
              env.client.patch(f"/v1/libraries/{team.b}", json={"root_path": elsewhere}, headers=team.lead),
              env.client.put(f"/v1/libraries/{team.b}/reader", json={"reader": "server"}, headers=team.lead)):
        _error(r, 403, "forbidden")
    after = _ok(env.client.get(f"/v1/libraries/{team.a}", headers=env.admin))
    assert (after["root_path"], after["reader"], after["private"]) == (before["root_path"], "server", False)
    # What else is theirs stays theirs.
    _ok(env.client.patch(f"/v1/libraries/{team.a}", json={"private": True}, headers=team.lead))

    boss_email = f"boss-{uuid.uuid4().hex[:8]}@example.com"
    _person(env, boss_email, "admin")
    token = _ok(env.client.post("/v1/auth/login", json={"email": boss_email, "password": PASSWORD}))["access_token"]
    boss = {"Authorization": f"Bearer {token}"}
    assert _ok(env.client.patch(f"/v1/libraries/{team.a}", json={"root_path": "/Volumes/moved"},
                                headers=boss))["root_path"] == "/Volumes/moved"
    chosen = _ok(env.client.put(f"/v1/libraries/{team.a}/reader",
                                json={"reader": device["device_id"], "root_path": "/Users/lead/a"}, headers=env.admin))
    assert (chosen["reader"], chosen["root_path"]) == (device["device_id"], "/Users/lead/a")
    seen = _ok(env.client.get(f"/v1/libraries/{team.a}", headers=team.lead))
    assert (seen["reader"], seen["reader_name"], seen["root_path"]) == (
        device["device_id"], f"lead's {team.a}", "/Users/lead/a"), "its admin sees who reads it and where"
    assert _ok(env.client.put(f"/v1/libraries/{team.a}/reader", json={"reader": "server", "root_path": "/Volumes/a"},
                              headers=boss))["reader"] == "server"


def test_a_private_library_and_someone_who_sees_only_what_theyre_given(env, team):
    _ok(env.client.patch(f"/v1/libraries/{team.b}", json={"private": True}, headers=env.admin))
    assert team.b not in _sees(env, team.member) and team.b in _sees(env, env.admin)
    assert _people(env, team.b, env.admin)[team.member_email]["role"] == "none"
    _ok(_role(env, team.b, team.member_id, "viewer", env.admin))
    assert team.b in _sees(env, team.member)

    member = _ok(env.client.patch(f"/v1/users/{team.member_id}", json={"every_library": False},
                                  headers=env.admin))
    assert member["every_library"] is False and member["role"] == "viewer"
    assert _sees(env, team.member) == {team.b}, "only the library they're given"
    assert _ok(env.client.patch(f"/v1/users/{team.member_id}", json={"every_library": True},
                                headers=env.admin))["every_library"] is True
    assert team.a in _sees(env, team.member)

    email = f"guest-{uuid.uuid4().hex[:8]}@example.com"
    guest = _ok(env.client.post("/v1/users", headers=env.admin, json={
        "email": email, "password": PASSWORD, "role": "editor", "every_library": False}), 201)
    assert guest["every_library"] is False
    assert _sees(env, _key(env, guest["user_id"], "editor")) == set()


def test_a_role_set_takes_effect_on_what_they_can_edit(env, team):
    clip = _scan(env, team.a, [File("a.jpg", f"a {team.a}")], headers=env.admin).uploaded["a.jpg"]
    note = {"text": "a note"}
    assert env.client.put(f"/v1/assets/{clip}/note", json=note, headers=team.lead).status_code == 200
    _ok(_role(env, team.a, team.lead_id, "viewer", env.admin))
    assert env.client.put(f"/v1/assets/{clip}/note", json=note, headers=team.lead).status_code == 403
    _ok(_role(env, team.a, team.lead_id, "none", env.admin))
    assert env.client.get(f"/v1/assets/{clip}", headers=team.lead).status_code == 404


def test_a_server_admin_s_role_on_a_library_is_a_server_admin_s_to_set(env, team):
    """Whoever holds a server admin's password has the install: a library's admin, or an account admin's key,
    sets anyone's role there but theirs (403 server_admin_user, nothing set); a server admin does."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import UserRepository

    tag = uuid.uuid4().hex[:8]
    keeper_email, boss_email = f"keeper-{tag}@example.com", f"boss-{tag}@example.com"
    keeper_id, boss_id = _person(env, keeper_email, "editor"), _person(env, boss_email, "admin")
    with get_control_session() as session:
        for user_id in (keeper_id, boss_id):
            UserRepository(session).name_server_admin(user_id)
    _ok(_role(env, team.a, team.lead_id, "admin", env.admin))

    for caller in (team.lead, env.admin):
        refused = _role(env, team.a, keeper_id, "viewer", caller)
        _error(refused, 403, "server_admin_user")
        assert f"{keeper_email} is a server admin" in refused.json()["error"]["message"]
    assert _people(env, team.a, env.admin)[keeper_email]["set_role"] is None, "nothing set"
    _ok(_role(env, team.a, team.member_id, "editor", team.lead))  # anyone else's, as before

    token = _ok(env.client.post("/v1/auth/login", json={"email": boss_email, "password": PASSWORD}))["access_token"]
    boss = {"Authorization": f"Bearer {token}"}
    assert _ok(_role(env, team.a, keeper_id, "viewer", boss))["set_role"] == "viewer"
    assert _ok(_role(env, team.a, keeper_id, None, boss))["set_role"] is None


# ---------------------------------------------------------------------------
# The CLI, against the same API
# ---------------------------------------------------------------------------


class _Through:
    """The CLI's client's connection, sent through the test's API with a key."""

    def __init__(self, env, headers: dict) -> None:
        self._env, self._headers = env, headers

    def request(self, method: str, url: str, **kw):
        return self._env.client.request(method, url, headers={**self._headers, **(kw.pop("headers", None) or {})},
                                        **kw)

    def close(self) -> None:
        pass


def _cli(env, headers: dict, *args: str):
    client = LumiverbClient(base_url="http://testserver", token="unused")
    client._client = _Through(env, headers)  # type: ignore[assignment]
    with patch("src.client.cli.main.LumiverbClient", return_value=client), \
            patch("src.client.cli.commands.users.LumiverbClient", return_value=client):
        return runner.invoke(cli_app, list(args))


def _name_of(env, library_id: str) -> str:
    return _ok(env.client.get(f"/v1/libraries/{library_id}", headers=env.admin))["name"]


def test_the_cli_sets_roles_privacy_and_who_sees_every_library(env, team):
    name = _name_of(env, team.a)
    r = _cli(env, env.admin, "library", "role", "--name", name, "--email", team.member_email, "--role", "none")
    assert r.exit_code == 0, r.output
    assert team.a not in _sees(env, team.member)
    r = _cli(env, env.admin, "library", "people", "--name", name)
    assert r.exit_code == 0 and team.member_email in r.output and "set for this library" in r.output, r.output
    r = _cli(env, env.admin, "library", "role", "--name", name, "--email", team.member_email, "--role", "default")
    assert r.exit_code == 0 and team.a in _sees(env, team.member), r.output

    r = _cli(env, env.admin, "library", "private", "--name", name, "--on")
    assert r.exit_code == 0 and team.a not in _sees(env, team.member), r.output
    r = _cli(env, env.admin, "library", "private", "--name", name, "--off")
    assert r.exit_code == 0 and team.a in _sees(env, team.member), r.output

    r = _cli(env, env.admin, "user", "libraries", "--email", team.member_email, "--only-given")
    assert r.exit_code == 0 and _sees(env, team.member) == set(), r.output
    r = _cli(env, env.admin, "user", "list")
    assert r.exit_code == 0 and "only given" in r.output, r.output
    r = _cli(env, env.admin, "user", "libraries", "--email", team.member_email, "--every")
    assert r.exit_code == 0 and team.a in _sees(env, team.member), r.output

    r = _cli(env, team.member, "library", "role", "--name", name, "--email", team.lead_email, "--role", "none")
    assert r.exit_code == 1, "not theirs to set"


def test_the_cli_moves_a_library_for_an_account_admin_and_says_why_not_to_its_own_admin(env, team):
    _ok(_role(env, team.a, team.lead_id, "admin", env.admin))
    name = _name_of(env, team.a)
    r = _cli(env, team.lead, "library", "update", "--name", name, "--root-path", "/Volumes/leads")
    assert r.exit_code == 1 and "Error [forbidden]: Admin API key required" in r.stderr, r.output
    assert "Library updated" not in r.output
    r = _cli(env, env.admin, "library", "update", "--name", name, "--root-path", "/Volumes/moved-by-cli")
    assert r.exit_code == 0 and "root_path: /Volumes/moved-by-cli" in r.output, r.output
    # Renaming stays its editors'.
    r = _cli(env, team.lead, "library", "update", "--name", name, "--new-name", f"{name}-renamed")
    assert r.exit_code == 0 and f"name: {name}-renamed" in r.output, r.output


def test_the_cli_turns_infer_location_on_and_asks_before_removing_guesses(env, team):
    from tests.machine_lineage import made

    name = _name_of(env, team.b)
    r = _cli(env, env.admin, "library", "infer-location", "--name", name, "--on")
    assert r.exit_code == 0 and "guessing locations" in r.output, r.output
    assert _ok(env.client.get(f"/v1/libraries/{team.b}", headers=env.admin))["infer_location"] is True
    clip = _scan(env, team.b, [File("g.jpg", f"g {team.b}")], headers=env.admin).uploaded["g.jpg"]
    guess = {"guess": {"lat": 5.0, "lon": 5.0, "radius_m": 100, "basis": {}}, "minutes": 360,
             "lineage": made("lumiverb.location.guess")}
    assert _ok(env.client.put(f"/v1/assets/locations/guess/{clip}", json=guess, headers=env.admin)) == {
        "result": "stored"}

    r = _cli(env, team.member, "library", "infer-location", "--name", name, "--off")
    assert r.exit_code == 1, "not theirs to set"
    with patch("typer.confirm", return_value=False):
        r = _cli(env, env.admin, "library", "infer-location", "--name", name, "--off")
    assert r.exit_code == 0 and "1 clip in" in r.output, r.output
    assert _ok(env.client.get(f"/v1/assets/{clip}", headers=env.admin))["location"]["source"] == "time"
    r = _cli(env, env.admin, "library", "infer-location", "--name", name, "--off", "--yes")
    assert r.exit_code == 0 and "no location guesses" in r.output, r.output
    assert _ok(env.client.get(f"/v1/assets/{clip}", headers=env.admin))["location"] is None
    assert _ok(env.client.get(f"/v1/libraries/{team.b}", headers=env.admin))["infer_location"] is False

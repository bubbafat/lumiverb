"""The server admin (ADR-018 decision 8): what belongs to the install rather
than an account (its accounts, where previews live, the network, backups,
logs) is a person's, who signs in: users.server_admin, read at each request
(dependencies.require_server_admin). Whoever sets the install up is one; an
account admin who isn't one, and any key a person made, gets 403
server_admin_required on every such route; naming or removing one takes
effect at once, and the last one stays; the install's own key (ADMIN_KEY)
still looks after its accounts; `lumiverb-server server-admins` names and
removes them on the machine itself. A route of the install's is held to
SERVER_ROUTES here, so a new one is added on purpose."""

from __future__ import annotations

import re
import threading

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from src.server.api import rate_limit
from src.server.api.dependencies import require_server_admin
from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.lifecycle import cli
from src.server.repository.control_plane import UserRepository
from tests.real_account import admin_with_database, drop_databases, someone
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture)
    HERE,
    LOCALHOST,
    _env_file,
    control,
)

pytestmark = pytest.mark.slow

INSTALL_KEY = "the-install-s-own-key"
ALL_STEPS = ["storage", "network", "find", "ai", "people", "library"]

SERVER_ROUTES = {
    ("POST", "/v1/admin/tenants"), ("GET", "/v1/admin/tenants"), ("PATCH", "/v1/admin/tenants/{tenant_id}"),
    ("DELETE", "/v1/admin/tenants/{tenant_id}"), ("POST", "/v1/admin/tenants/{tenant_id}/keys"),
    ("GET", "/v1/admin/tenants/{tenant_id}/keys"),
    ("GET", "/v1/admin/server-admins"), ("POST", "/v1/admin/server-admins"),
    ("DELETE", "/v1/admin/server-admins/{user_id}"),
    ("PUT", "/v1/system/network"), ("PUT", "/v1/system/network/certificate"),
    ("DELETE", "/v1/system/network/certificate"),
    ("GET", "/v1/system/backups"), ("POST", "/v1/system/backups"), ("PUT", "/v1/system/backups/folder"),
    ("GET", "/v1/system/storage"), ("PUT", "/v1/system/storage/previews"), ("POST", "/v1/system/storage/move"),
    ("GET", "/v1/system/logs"),
    ("PUT", "/v1/plugins/{plugin_id}/server-settings"),
}


def _needs_server_admin() -> set[tuple[str, str]]:
    def calls(dependant):
        for sub in dependant.dependencies:
            yield sub.call
            yield from calls(sub)

    return {(method, context.path) for context in iter_route_contexts(app.routes)
            if isinstance(context.original_route, APIRoute)
            and require_server_admin in set(calls(context.original_route.dependant))
            for method in context.methods or ()}


def _refused(client: TestClient, headers: dict[str, str]) -> None:
    """Every route of the install's turns them away, before anything is read or done."""
    for method, path in sorted(SERVER_ROUTES):
        r = client.request(method, re.sub(r"\{[^}]+\}", "x", path), headers=headers, json={})
        assert (r.status_code, r.json()["error"]["code"]) == (403, "server_admin_required"), (method, path, r.text)


@pytest.fixture
def install(control, monkeypatch):  # noqa: F811
    """A set-up install on the machine itself: its first admin signed in (a
    server admin), their account's database made, the install's own key set."""
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    monkeypatch.setenv("ADMIN_KEY", INSTALL_KEY)
    get_settings.cache_clear()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        robert, tenant_id = admin_with_database(client, control)
        yield client, robert, tenant_id
    drop_databases(control)


def test_the_install_s_routes_are_these():
    assert _needs_server_admin() == SERVER_ROUTES


def test_whoever_sets_up_the_install_is_its_server_admin_and_nobody_else_is(install):
    client, robert, tenant_id = install
    assert client.get("/v1/me", headers=robert).json()["server_admin"] is True
    for role in ("admin", "editor", "viewer"):
        theirs = someone(client, tenant_id, role)
        me = client.get("/v1/me", headers=theirs).json()
        assert (me["role"], me["server_admin"]) == (role, False)
        _refused(client, theirs)


def test_a_key_is_never_a_server_admin(install):
    client, robert, _ = install
    made = client.post("/v1/keys", json={"label": "a script", "role": "admin"}, headers=robert)
    assert made.status_code == 200, made.text
    key = {"Authorization": f"Bearer {made.json()['plaintext']}"}
    assert client.get("/v1/me", headers=key).json()["server_admin"] is False
    _refused(client, key)


def test_naming_and_removing_one_takes_effect_at_once(install):
    client, robert, tenant_id = install
    editor = someone(client, tenant_id, "editor")  # their role in the account doesn't come into it
    assert client.get("/v1/admin/tenants", headers=editor).status_code == 403

    named = client.post("/v1/admin/server-admins", json={"email": " editor@example.com "}, headers=robert)
    assert named.status_code == 200, named.text
    assert [(a["email"], a["account"], a["tenant_id"]) for a in named.json()["server_admins"]] == [
        ("editor@example.com", "Lumiverb", tenant_id), ("robert@example.com", "Lumiverb", tenant_id)]
    assert client.get("/v1/admin/tenants", headers=editor).status_code == 200, "the same sign-in, at once"
    assert client.get("/v1/me", headers=editor).json()["server_admin"] is True
    again = client.post("/v1/admin/server-admins", json={"email": "editor@example.com"}, headers=robert)
    assert len(again.json()["server_admins"]) == 2, "one already is: nothing changes"

    editor_id = next(a["user_id"] for a in named.json()["server_admins"] if a["email"] == "editor@example.com")
    assert client.delete(f"/v1/admin/server-admins/{editor_id}", headers=robert).status_code == 204
    gone = client.get("/v1/admin/tenants", headers=editor)
    assert (gone.status_code, gone.json()["error"]["code"]) == (403, "server_admin_required")
    assert client.get("/v1/me", headers=editor).json()["server_admin"] is False
    assert client.get("/v1/libraries", headers=editor).status_code == 200, "their sign-in and account stay"


def test_the_last_one_stays_and_only_people_here_are_named(install):
    client, robert, _ = install
    me = client.get("/v1/me", headers=robert).json()["user_id"]
    last = client.delete(f"/v1/admin/server-admins/{me}", headers=robert)
    assert (last.status_code, last.json()["error"]["code"]) == (409, "last_server_admin")
    nobody = client.post("/v1/admin/server-admins", json={"email": "stranger@example.com"}, headers=robert)
    assert (nobody.status_code, nobody.json()["error"]["code"]) == (404, "user_not_found")
    assert client.delete("/v1/admin/server-admins/usr_nobody", headers=robert).status_code == 404
    listed = client.get("/v1/admin/server-admins", headers=robert).json()["server_admins"]
    assert [a["email"] for a in listed] == ["robert@example.com"]


def test_two_taking_each_other_s_away_at_once_leave_one(install):
    client, _, tenant_id = install
    someone(client, tenant_id, "admin")
    with get_control_session() as session:
        users = UserRepository(session)
        ids = [users.get_by_email(e).user_id for e in ("admin@example.com", "robert@example.com")]
        users.set_server_admin(ids[0], True)
    ready, results = threading.Barrier(2), []

    def take_away(user_id: str) -> None:
        with get_control_session() as session:
            ready.wait()
            results.append(UserRepository(session).set_server_admin(user_id, False, keep_one=True))

    threads = [threading.Thread(target=take_away, args=(i,)) for i in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(results) == [False, True]
    with get_control_session() as session:
        assert len(UserRepository(session).server_admins()) == 1


def test_the_install_s_own_key_still_looks_after_its_accounts(install):
    client, _, tenant_id = install
    key = {"Authorization": f"Bearer {INSTALL_KEY}"}
    assert [t["tenant_id"] for t in client.get("/v1/admin/tenants", headers=key).json()] == [tenant_id]
    assert [a["email"] for a in client.get("/v1/admin/server-admins", headers=key).json()["server_admins"]] == [
        "robert@example.com"]
    # No account's key: the install's settings are a server admin's, signed in.
    assert client.get("/v1/system/backups", headers=key).status_code == 401
    assert client.get("/v1/me", headers=key).status_code == 401
    # Any other token is no key at all, whatever characters a header carries.
    for token in ("wrong", "clé"):
        r = client.get("/v1/admin/tenants", headers={"Authorization": f"Bearer {token}".encode("latin-1")})
        assert r.status_code == 401, (token, r.text)


def test_the_admin_page_shows_the_install_s_rows_to_a_server_admin_only(install):
    client, robert, tenant_id = install
    other = someone(client, tenant_id, "admin")

    def rows(headers: dict[str, str]) -> list[str]:
        r = client.get("/v1/system/health", headers=headers)
        assert r.status_code == 200, r.text
        return [row["key"] for row in r.json()["rows"]]

    assert rows(robert) == ["website", "processing", "ai", "search", "storage", "disk", "backups",
                            "own_plugins"]
    assert rows(other) == ["website", "processing", "ai", "search", "storage"]


def test_setting_up_where_previews_live_and_who_can_open_it_is_a_server_admin_s(install):
    client, robert, tenant_id = install
    other = someone(client, tenant_id, "admin")
    theirs = client.get("/v1/tenant/setup", headers=other).json()
    assert theirs["steps"] == ["find", "ai", "people", "library"]
    done = client.put("/v1/tenant/setup", json={"done": theirs["steps"]}, headers=other).json()
    assert done["finished"] is True, "every step of theirs"
    mine = client.get("/v1/tenant/setup", headers=robert).json()
    assert (mine["steps"], mine["finished"]) == (ALL_STEPS, False)
    assert client.put("/v1/tenant/setup", json={"done": ALL_STEPS}, headers=robert).json()["finished"] is True


def test_on_the_machine_its_owner_names_and_removes_them(install, control, tmp_path, capsys):  # noqa: F811
    client, robert, tenant_id = install
    someone(client, tenant_id, "viewer")
    env = str(_env_file(tmp_path, control))

    assert cli.main(["server-admins", "add", "viewer@example.com", "--env", env]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "✓ viewer@example.com is a server admin", "Server admins:",
        "  robert@example.com (Lumiverb)", "  viewer@example.com (Lumiverb)"]
    assert cli.main(["server-admins", "remove", "robert@example.com", "--env", env]) == 0
    assert client.get("/v1/admin/tenants", headers=robert).status_code == 403, "at once"
    # The last one too: whoever is at the machine can always name another.
    assert cli.main(["server-admins", "remove", "viewer@example.com", "--env", env]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "This Lumiverb has no server admin: name one with `lumiverb-server server-admins add EMAIL`")

    assert cli.main(["server-admins", "add", "stranger@example.com", "--env", env]) == 1
    assert capsys.readouterr().err.strip() == "✗ Nobody signs in here with that email"
    assert cli.main(["server-admins", "add", "--env", env]) == 2
    assert cli.main(["server-admins", "--env", env]) == 0
    assert capsys.readouterr().out.strip().endswith("server-admins add EMAIL`")

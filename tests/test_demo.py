"""Try it as a viewer (src/server/demo.py, /v1/auth/demo): a demo server lets
anyone in, with no password, as the viewer its env file names, for a session
that reads only, whatever that viewer's roles on each library. Off unless it
names a viewer who isn't a server admin; turned off, every demo session ends
at its next request."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.server.api.main import app
from src.server.config import get_settings
from tests.real_account import someone
from tests.test_first_run_setup import HERE, PASSWORD, control  # noqa: F401 (a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

pytestmark = pytest.mark.slow
ANON = {"Authorization": ""}


def _demo(monkeypatch, email: str) -> None:
    monkeypatch.setenv("LUMIVERB_DEMO_VIEWER", email)
    get_settings.cache_clear()


def _try(client) -> dict[str, str]:
    r = client.post("/v1/auth/demo", headers=ANON)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _user_id(client, headers) -> str:
    return client.get("/v1/me", headers=headers).json()["user_id"]


def test_off_unless_it_names_a_viewer(install, monkeypatch):  # noqa: F811
    client, robert, tenant_id = install
    for role in ("viewer", "editor"):
        someone(client, tenant_id, role)
    for email in ("", "nobody@example.com", "editor@example.com", "robert@example.com"):
        _demo(monkeypatch, email)
        for method in ("GET", "POST"):
            r = client.request(method, "/v1/auth/demo", headers=ANON)
            assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found"), (email, method, r.text)
    _demo(monkeypatch, " Viewer@Example.com ")
    assert client.get("/v1/auth/demo", headers=ANON).json() == {"on": True}

    # A viewer made a server admin is no demo's.
    r = client.post("/v1/admin/server-admins", json={"email": "viewer@example.com"}, headers=robert)
    assert r.status_code == 200, r.text
    assert client.post("/v1/auth/demo", headers=ANON).status_code == 404


def test_a_demo_session_reads_and_changes_nothing_whatever_its_roles(install, monkeypatch):  # noqa: F811
    client, robert, tenant_id = install
    typed = someone(client, tenant_id, "viewer")
    _demo(monkeypatch, "viewer@example.com")
    demo = _try(client)
    me = client.get("/v1/me", headers=demo).json()
    assert (me["email"], me["role"], me["server_admin"], me["demo"]) == ("viewer@example.com", "viewer", False, True)
    assert client.get("/v1/me", headers=typed).json()["demo"] is False

    library = client.post("/v1/libraries", json={"name": "Footage", "root_path": "/Volumes/footage"}, headers=robert)
    assert library.status_code == 200, library.text
    library_id = library.json()["library_id"]
    made_admin = client.put(f"/v1/libraries/{library_id}/people/{_user_id(client, typed)}", json={"role": "admin"},
                            headers=robert)
    assert made_admin.status_code == 200, made_admin.text
    assert client.get("/v1/libraries", headers=demo).status_code == 200
    assert client.patch(f"/v1/libraries/{library_id}", json={"name": "Typed"}, headers=typed).status_code == 200, \
        "the viewer signed in with their password: the library's admin"

    for method, path, body in (
        ("PATCH", f"/v1/libraries/{library_id}", {"name": "Demo"}),
        ("POST", "/v1/keys", {"label": "mine", "role": "viewer"}),
        ("POST", "/v1/views", {"name": "mine", "query": {}}),
        ("POST", "/v1/views", {"name": "a\x00b", "query": {}}),  # its body never read (src/server/api/no_nul.py)
        ("DELETE", f"/v1/libraries/{library_id}", None),
    ):
        r = client.request(method, path, json=body, headers=demo)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "demo_reads_only"), (method, path, r.text)
    names = {lib["library_id"]: lib["name"] for lib in client.get("/v1/libraries", headers=robert).json()}
    assert names[library_id] == "Typed"


def test_a_demo_session_refreshes_as_one_and_ends_with_the_demo(install, monkeypatch):  # noqa: F811
    client, _, tenant_id = install
    someone(client, tenant_id, "viewer")
    _demo(monkeypatch, "viewer@example.com")
    demo = _try(client)
    r = client.post("/v1/auth/refresh", headers=demo)
    assert r.status_code == 200, r.text
    again = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/v1/me", headers=again).json()["demo"] is True
    assert client.post("/v1/keys", json={"label": "x", "role": "viewer"}, headers=again).status_code == 403

    later = _try(client)
    _demo(monkeypatch, "")
    r = client.get("/v1/me", headers=again)
    assert (r.status_code, r.json()["error"]["code"]) == (401, "token_revoked"), r.text
    assert client.post("/v1/auth/refresh", headers=later).status_code == 401
    assert client.post("/v1/auth/demo", headers=ANON).status_code == 404


def test_signing_in_to_the_demo_is_held_to_a_few_a_minute(install, monkeypatch):  # noqa: F811
    client, _, tenant_id = install
    someone(client, tenant_id, "viewer")
    _demo(monkeypatch, "viewer@example.com")
    codes = [client.post("/v1/auth/demo", headers=ANON).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429], codes


def test_on_a_demo_server_a_visitor_tries_it_and_reaches_nothing_else(install, monkeypatch):  # noqa: F811
    """With the exposure on (src/server/api/exposure.py), as the demo server runs: a visitor, through nginx."""
    client, robert, tenant_id = install
    someone(client, tenant_id, "viewer")
    _demo(monkeypatch, "viewer@example.com")
    monkeypatch.setenv("LUMIVERB_EXPOSURE", "demo")
    get_settings.cache_clear()
    visitor = TestClient(app, client=HERE, base_url="https://try.lumiverb.io",
                         headers={"X-Forwarded-For": "203.0.113.9"})
    typed = visitor.post("/v1/auth/login", json={"email": "viewer@example.com", "password": PASSWORD}, headers=ANON)
    assert (typed.status_code, typed.json()["error"]["code"]) == (404, "not_found"), typed.text
    demo = _try(visitor)
    assert visitor.get("/v1/me", headers=demo).json()["demo"] is True
    assert visitor.get("/v1/libraries", headers=demo).status_code == 200
    for method, path in (("POST", "/v1/keys"), ("GET", "/v1/ai"), ("GET", "/v1/system/network")):
        r = visitor.request(method, path, json={}, headers=demo)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found"), (method, path, r.text)
    assert visitor.get("/v1/ai", headers=robert).status_code == 404, "an admin's sign-in too, from elsewhere"

    # On the machine itself (a tunnel to it too): everything, a password's sign-in as well.
    assert client.post("/v1/auth/login", json={"email": "viewer@example.com", "password": PASSWORD},
                       headers=ANON).status_code == 200
    assert client.get("/v1/ai", headers=robert).status_code == 200

"""Authorization hardening: what visitors, editors and anonymous callers can't do.

- A public page's cursor carries only a date or an id (not a file name or EXIF).
- Visitors can't probe file names (path_prefix, by-path, path search) or cameras (similar).
- API keys don't outlive their creator's role; editors manage only their own keys.
- Login and forgot-password: no X-Forwarded-For bypass, a per-email limit, no timing tell.
- Passwords past bcrypt's 72 bytes are refused, not a 500.
- Routes only the scheduler calls are admin-only.
- A trashed library stops being public.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from src.server.api.main import app
from src.server.api.rate_limit import forgot_password_limiter, login_limiter
from src.server.api.routers import auth
from src.server.config import get_settings
from tests.test_public_library_middleware import _ingest, located, public_lib_client  # noqa: F401
from tests.test_public_search_cap import FakeQuickwit

_PASSWORD = "correct horse battery"


def _decode(cursor: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, email: str, password: str = _PASSWORD):
    login_limiter._hits.clear()
    return client.post("/v1/auth/login", json={"email": email, "password": password})


def _user(client: TestClient, admin: dict, email: str, role: str) -> tuple[str, dict]:
    r = client.post("/v1/users", json={"email": email, "password": _PASSWORD, "role": role}, headers=admin)
    assert r.status_code == 201, r.text
    token = _login(client, email).json()["access_token"]
    return r.json()["user_id"], _bearer(token)


# ---------------------------------------------------------------------------
# 1. A visitor's cursor
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("sort", ["rel_path", "iso", "aperture", "focal_length", "exposure_time_us", "file_size",
                                  "created_at"])
def test_public_pages_refuse_sorts_a_cursor_would_leak(public_lib_client, located, sort):
    client, api_key, library_id, _ = public_lib_client
    page = {"library_id": library_id, "sort": sort, "limit": 1}
    query = [("f", f"library:{library_id}"), ("sort", sort), ("limit", "1")]
    assert client.get("/v1/assets/page", params=page).status_code == 403
    assert client.get("/v1/query", params=query).status_code == 403
    # Signed in, every sort works.
    assert client.get("/v1/assets/page", params=page, headers=_bearer(api_key)).status_code == 200
    assert client.get("/v1/query", params=query, headers=_bearer(api_key)).status_code == 200


@pytest.mark.slow
@pytest.mark.parametrize("sort", ["taken_at", "asset_id"])
def test_public_pages_page_by_date_or_id(public_lib_client, located, sort):
    client, _, library_id, _ = public_lib_client
    _ingest(client, public_lib_client[1], library_id, f"trips/by-{sort}.jpg", media_type="image")
    for r in (client.get("/v1/assets/page", params={"library_id": library_id, "sort": sort, "limit": 1}),
              client.get("/v1/query", params=[("f", f"library:{library_id}"), ("sort", sort), ("limit", "1")])):
        assert r.status_code == 200, r.text
        cursor = _decode(r.json()["next_cursor"])
        assert "trips" not in json.dumps(cursor) and "beach" not in json.dumps(cursor)


# ---------------------------------------------------------------------------
# 4, 5. Probing file names and cameras
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_public_page_narrows_to_folders_not_files(public_lib_client, located):
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/assets/page", params={"library_id": library_id, "path_prefix": "trips"})
    assert r.status_code == 200, r.text
    assert located in [i["asset_id"] for i in r.json()["items"]]
    for probe in ("trips/secret-beach.jpg", "nope", "trip", "t%"):
        r = client.get("/v1/assets/page", params={"library_id": library_id, "path_prefix": probe})
        assert r.status_code == 403, (probe, r.status_code)


@pytest.mark.slow
def test_public_query_narrows_to_folders_not_files(public_lib_client, located):
    client, _, library_id, _ = public_lib_client
    base = [("f", f"library:{library_id}")]
    r = client.get("/v1/query", params=[*base, ("f", "path:trips")])
    assert r.status_code == 200, r.text
    assert located in [i["asset_id"] for i in r.json()["items"]]
    assert client.get("/v1/query", params=[*base, ("f", "path:trips/secret-beach.jpg")]).status_code == 403
    assert client.get("/v1/assets/facets", params=[*base, ("f", "path:trips/secret-beach.jpg")]).status_code == 403


@pytest.mark.slow
def test_by_path_isnt_public(public_lib_client, located):
    client, api_key, library_id, _ = public_lib_client
    params = {"library_id": library_id, "rel_path": "trips/secret-beach.jpg"}
    assert client.get("/v1/assets/by-path", params=params).status_code == 403
    signed_in = client.get("/v1/assets/by-path", params=params, headers=_bearer(api_key))
    assert signed_in.status_code == 200 and signed_in.json()["asset_id"] == located


@pytest.mark.slow
@pytest.mark.parametrize("term", ["secret", "beach", "Acme", "X1"])
def test_public_search_doesnt_read_paths_or_cameras(public_lib_client, located, term):
    """Quickwit is off in tests: this is the Postgres fallback."""
    client, api_key, library_id, _ = public_lib_client
    params = [("f", f"library:{library_id}"), ("f", f"query:{term}")]
    assert located not in [i["asset_id"] for i in client.get("/v1/query", params=params).json()["items"]]
    assert located in [i["asset_id"] for i in
                       client.get("/v1/query", params=params, headers=_bearer(api_key)).json()["items"]]


@pytest.mark.fast
def test_public_quickwit_search_leaves_path_tokens_out(monkeypatch):
    from src.server.api.routers.query import _run_quickwit_search
    from src.server.models.query_filter import SearchTerm

    fake = FakeQuickwit()
    monkeypatch.setattr("src.server.search.quickwit_client.QuickwitClient", lambda: fake)
    _run_quickwit_search("ten_1", [SearchTerm(q="beach")], ["lib_1"], limit=50, public=True)
    public, fake.asset_queries = fake.asset_queries, []
    _run_quickwit_search("ten_1", [SearchTerm(q="beach")], ["lib_1"], limit=50, public=False)
    assert public and not any("path_tokens" in q for q in public)
    assert any("path_tokens" in q for q in fake.asset_queries)


@pytest.mark.slow
@pytest.mark.parametrize("param", ["camera_make", "camera_model"])
def test_public_similar_refuses_camera_filters(public_lib_client, located, param):
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/similar", params={"asset_id": located, "library_id": library_id, param: "Acme"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 2. API keys and their creator
# ---------------------------------------------------------------------------


def _works(client: TestClient, key: str) -> bool:
    return client.get("/v1/libraries", headers=_bearer(key)).status_code == 200


@pytest.mark.slow
def test_editors_see_and_revoke_only_their_own_keys(public_lib_client):
    client, api_key, _, _ = public_lib_client
    admin = _bearer(api_key)
    _, ed = _user(client, admin, "keys-own@example.com", "editor")
    mine = client.post("/v1/keys", json={"label": "mine", "role": "editor"}, headers=ed).json()
    # A key the editor's key mints is the editor's too.
    child = client.post("/v1/keys", json={"label": "child", "role": "viewer"},
                        headers=_bearer(mine["plaintext"])).json()
    worker = client.post("/v1/keys", json={"label": "worker", "role": "editor"}, headers=admin).json()

    listed = {k["key_id"] for k in client.get("/v1/keys", headers=ed).json()["keys"]}
    assert listed == {mine["key_id"], child["key_id"]}
    assert {k["key_id"] for k in client.get("/v1/keys", headers=_bearer(mine["plaintext"])).json()["keys"]} == listed
    everything = {k["key_id"] for k in client.get("/v1/keys", headers=admin).json()["keys"]}
    assert {mine["key_id"], child["key_id"], worker["key_id"]} <= everything

    assert client.delete(f"/v1/keys/{worker['key_id']}", headers=ed).status_code == 404
    assert client.delete(f"/v1/keys/{child['key_id']}", headers=ed).status_code == 204
    assert client.delete(f"/v1/keys/{worker['key_id']}", headers=admin).status_code == 204


@pytest.mark.slow
def test_keys_dont_outlive_their_creators_role(public_lib_client):
    client, api_key, _, _ = public_lib_client
    admin = _bearer(api_key)
    uid, ed = _user(client, admin, "keys-demoted@example.com", "editor")
    editor_key = client.post("/v1/keys", json={"label": "e", "role": "editor"}, headers=ed).json()["plaintext"]
    viewer_key = client.post("/v1/keys", json={"label": "v", "role": "viewer"}, headers=ed).json()["plaintext"]
    worker_key = client.post("/v1/keys", json={"label": "w", "role": "editor"}, headers=admin).json()["plaintext"]
    assert all(_works(client, k) for k in (editor_key, viewer_key, worker_key))

    assert client.patch(f"/v1/users/{uid}", json={"role": "viewer"}, headers=admin).status_code == 200
    assert not _works(client, editor_key)  # above the new role
    assert _works(client, viewer_key) and _works(client, worker_key)

    assert client.delete(f"/v1/users/{uid}", headers=admin).status_code == 204
    assert not _works(client, viewer_key)
    assert _works(client, worker_key) and _works(client, api_key)  # no creator: untouched


# ---------------------------------------------------------------------------
# 3. Login rate limits
# ---------------------------------------------------------------------------


def _request(peer: str, xff: str | None):
    from starlette.requests import Request

    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1)})


@pytest.mark.fast
def test_rate_limit_trusts_only_our_proxys_forwarded_for():
    ip = login_limiter._client_ip
    # nginx appends the address it saw: the right-most entry is the real one.
    assert ip(_request("127.0.0.1", "6.6.6.6, 203.0.113.9")) == "203.0.113.9"
    assert ip(_request("::1", "203.0.113.9")) == "203.0.113.9"
    # Not from our proxy: the header is whatever the client sent.
    assert ip(_request("198.51.100.4", "6.6.6.6")) == "198.51.100.4"
    assert ip(_request("testclient", "6.6.6.6")) == "testclient"


@pytest.fixture
def no_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(auth.UserRepository, "get_by_email", lambda self, email: None)

    def _fake_db():
        yield None

    app.dependency_overrides[auth._get_db] = _fake_db
    login_limiter._hits.clear()
    forgot_password_limiter._hits.clear()
    yield
    app.dependency_overrides.pop(auth._get_db, None)
    login_limiter._hits.clear()
    forgot_password_limiter._hits.clear()
    get_settings.cache_clear()


@pytest.mark.fast
def test_rotating_forwarded_for_doesnt_dodge_the_login_limit(no_db):
    client = TestClient(app)
    codes = [client.post("/v1/auth/login", json={"email": f"n{i}@example.com", "password": "x"},
                         headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code for i in range(6)]
    assert codes == [401] * 5 + [429]


@pytest.mark.fast
def test_rotating_addresses_doesnt_dodge_the_per_email_limit(no_db):
    codes = [TestClient(app, client=(f"198.51.100.{i}", 1)).post(
        "/v1/auth/login", json={"email": "Target@Example.com" if i % 2 else "target@example.com", "password": "x"},
    ).status_code for i in range(6)]
    assert codes == [401] * 5 + [429]
    other = TestClient(app, client=("198.51.100.99", 1)).post(
        "/v1/auth/login", json={"email": "someone@example.com", "password": "x"})
    assert other.status_code == 401


# ---------------------------------------------------------------------------
# 6. Timing
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_unknown_email_login_is_one_check_against_a_ready_hash(no_db, monkeypatch):
    calls: list[str] = []
    real = auth.bcrypt.checkpw
    monkeypatch.setattr(auth.bcrypt, "checkpw", lambda pw, h: calls.append("checkpw") or real(pw, h))
    monkeypatch.setattr(auth.bcrypt, "hashpw", lambda *a: calls.append("hashpw"))
    r = TestClient(app).post("/v1/auth/login", json={"email": "ghost@example.com", "password": "whatever"})
    assert r.status_code == 401
    assert calls == ["checkpw"]


@pytest.mark.fast
def test_forgot_password_answers_before_looking_anyone_up(no_db, monkeypatch):
    for name, value in {"SMTP_HOST": "smtp.invalid", "SMTP_PORT": "587", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
                        "SMTP_FROM": "from@example.com", "APP_HOST": "https://app.invalid"}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    sent: list[str] = []
    monkeypatch.setattr(auth, "_send_reset_email", lambda email: sent.append(email))
    r = TestClient(app).post("/v1/auth/forgot-password", json={"email": "ghost@example.com"})
    assert r.status_code == 204
    assert sent == ["ghost@example.com"]  # after the response, in the background


# ---------------------------------------------------------------------------
# 7. Passwords past 72 bytes
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_long_password_login_is_a_401(no_db):
    r = TestClient(app).post("/v1/auth/login", json={"email": "ghost@example.com", "password": "é" * 40})
    assert r.status_code == 401


@pytest.mark.fast
def test_long_password_reset_is_a_400(no_db):
    r = TestClient(app).post("/v1/auth/reset-password", json={"token": "t", "password": "é" * 40})
    assert r.status_code == 400 and "72" in r.text


@pytest.mark.slow
def test_long_password_new_user_is_a_400(public_lib_client):
    client, api_key, _, _ = public_lib_client
    r = client.post("/v1/users", json={"email": "long@example.com", "password": "a" * 73, "role": "viewer"},
                    headers=_bearer(api_key))
    assert r.status_code == 400 and "72" in r.text


# ---------------------------------------------------------------------------
# 8. The scheduler's routes
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_editors_cant_report_for_the_scheduler(public_lib_client, located):
    client, api_key, _, _ = public_lib_client
    _, ed = _user(client, _bearer(api_key), "sched@example.com", "editor")
    failures = {"items": [{"asset_id": located, "artifact": "proxy", "error": "x"}]}
    assert client.post("/v1/producers/failures", json=failures, headers=ed).status_code == 403
    assert client.post("/v1/ai/machines/mch_any/status", json={"online": False}, headers=ed).status_code == 403


# ---------------------------------------------------------------------------
# 9. A trashed library
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_trashed_library_isnt_public(public_lib_client):
    client, api_key, _, _ = public_lib_client
    admin = _bearer(api_key)
    lib = client.post("/v1/libraries", json={"name": "Gone", "root_path": "/gone"}, headers=admin).json()["library_id"]
    assert client.patch(f"/v1/libraries/{lib}", json={"is_public": True}, headers=admin).status_code == 200
    assert client.delete(f"/v1/libraries/{lib}", headers=admin).status_code == 204
    assert client.post(f"/v1/libraries/{lib}/restore", headers=admin).status_code == 200
    assert client.get(f"/v1/libraries/{lib}", headers=admin).json()["is_public"] is False

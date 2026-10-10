"""Tests for hybrid auth: JWT + API key coexistence, logout, password validation, role guards."""

from __future__ import annotations

import os
import time

import hashlib
import uuid

import bcrypt
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.api.routers import auth as auth_module
from src.server.config import get_settings
from src.server.database import _engines

from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations

JWT_SECRET = "test-jwt-secret-for-hybrid-auth-tests"
JWT_ALGORITHM = "HS256"

# Unique per test run to prevent collisions if parallel tests share a DB.
_RUN_ID = uuid.uuid4().hex[:8]
TEST_API_KEY = f"test-api-key-hybrid-{_RUN_ID}"
TENANT_ID = f"tnt_hybrid_{_RUN_ID}"
USR_ADMIN = f"usr_admin_{_RUN_ID}"
USR_VIEWER = f"usr_viewer_{_RUN_ID}"
USR_EDITOR = f"usr_editor_{_RUN_ID}"


@pytest.fixture(scope="module")
def hybrid_env():
    """Full control plane with a user, an API key, and tenant routing — for hybrid auth tests."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer(PG_IMAGE) as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        with PostgresContainer(PG_IMAGE) as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            # Seed: tenant, routing, API key, and users.
            api_key_hash = hashlib.sha256(TEST_API_KEY.encode()).hexdigest()
            password_hash = bcrypt.hashpw(b"correct-horse-battery-staple", bcrypt.gensalt(rounds=4)).decode()

            engine = create_engine(control_url)
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO tenants (tenant_id, name, plan, status, created_at) "
                        "VALUES (:tid, 'Test Tenant', 'free', 'active', now())"
                    ),
                    {"tid": TENANT_ID},
                )
                conn.execute(
                    text(
                        "INSERT INTO tenant_db_routing (tenant_id, connection_string, region, created_at) "
                        "VALUES (:tid, :cs, 'local', now())"
                    ),
                    {"tid": TENANT_ID, "cs": tenant_url},
                )
                conn.execute(
                    text(
                        "INSERT INTO api_keys (key_id, tenant_id, key_hash, name, scopes, created_at, role) "
                        "VALUES (:kid, :tid, :kh, 'test', '[]'::jsonb, now(), 'admin')"
                    ),
                    {"kid": f"ak_{_RUN_ID}", "tid": TENANT_ID, "kh": api_key_hash},
                )
                conn.execute(
                    text(
                        "INSERT INTO users (user_id, tenant_id, email, password_hash, role) "
                        "VALUES (:uid, :tid, :email, :ph, 'admin')"
                    ),
                    {"uid": USR_ADMIN, "tid": TENANT_ID, "email": f"admin_{_RUN_ID}@test.com", "ph": password_hash},
                )
                conn.execute(
                    text(
                        "INSERT INTO users (user_id, tenant_id, email, password_hash, role) "
                        "VALUES (:uid, :tid, :email, :ph, 'viewer')"
                    ),
                    {"uid": USR_VIEWER, "tid": TENANT_ID, "email": f"viewer_{_RUN_ID}@test.com", "ph": password_hash},
                )
                conn.execute(
                    text(
                        "INSERT INTO users (user_id, tenant_id, email, password_hash, role) "
                        "VALUES (:uid, :tid, :email, :ph, 'editor')"
                    ),
                    {"uid": USR_EDITOR, "tid": TENANT_ID, "email": f"editor_{_RUN_ID}@test.com", "ph": password_hash},
                )
                conn.commit()
            engine.dispose()

            u = make_url(control_url)
            tenant_tpl = str(u.set(database="{tenant_id}"))
            os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
            os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
            os.environ["ADMIN_KEY"] = "test-admin-secret"
            os.environ["JWT_SECRET"] = JWT_SECRET
            get_settings.cache_clear()
            _engines.clear()

            with TestClient(app) as client:
                yield client

            _engines.clear()
            get_settings.cache_clear()


def _make_jwt(user_id: str, role: str, expired: bool = False, tampered: bool = False, tv: int = 0) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "tenant_id": TENANT_ID,
        "role": role,
        "tv": tv,
        "jti": uuid.uuid4().hex,
        "exp": now - 3600 if expired else now + 3600,
        "refresh_exp": now + 7 * 24 * 3600,
    }
    secret = "wrong-secret" if tampered else JWT_SECRET
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


# --- Logout endpoint ---

@pytest.mark.slow
def test_logout_returns_204(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_ADMIN, "admin")
    r = hybrid_env.post("/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204


@pytest.mark.fast
def test_logout_no_auth_returns_204(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logout is under /v1/auth/ which skips tenant middleware, so no auth needed."""
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.post("/v1/auth/logout")
        assert r.status_code == 204
    get_settings.cache_clear()


# --- Password validation ---

@pytest.mark.slow
def test_login_valid_credentials(hybrid_env: TestClient) -> None:
    r = hybrid_env.post("/v1/auth/login", json={"email": f"admin_{_RUN_ID}@test.com", "password": "correct-horse-battery-staple"})
    assert r.status_code == 200
    data = r.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.slow
def test_login_wrong_password(hybrid_env: TestClient) -> None:
    r = hybrid_env.post("/v1/auth/login", json={"email": f"admin_{_RUN_ID}@test.com", "password": "wrong-password-here"})
    assert r.status_code == 401


@pytest.mark.slow
def test_login_unknown_email(hybrid_env: TestClient) -> None:
    r = hybrid_env.post("/v1/auth/login", json={"email": "nobody@test.com", "password": "correct-horse-battery-staple"})
    assert r.status_code == 401


@pytest.mark.fast
def test_reset_password_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset-password rejects passwords shorter than 12 chars."""
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    get_settings.cache_clear()

    def _fake_db():
        yield None

    app.dependency_overrides[auth_module._get_db] = _fake_db
    try:
        with TestClient(app) as client:
            r = client.post("/v1/auth/reset-password", json={"token": "tok", "password": "short"})
        assert r.status_code == 400
        assert "12" in r.json()["error"]["message"]
    finally:
        app.dependency_overrides.pop(auth_module._get_db, None)
        get_settings.cache_clear()


# --- JWT middleware ---

@pytest.mark.slow
def test_jwt_admin_resolves_tenant(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_ADMIN, "admin")
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {token}"})
    # Should get 200 (empty list) — not 401.
    assert r.status_code == 200


@pytest.mark.slow
def test_jwt_viewer_resolves_tenant(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_VIEWER, "viewer")
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


@pytest.mark.slow
def test_expired_jwt_returns_401(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_ADMIN, "admin", expired=True)
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


@pytest.mark.slow
def test_tampered_jwt_returns_401(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_ADMIN, "admin", tampered=True)
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


@pytest.mark.slow
def test_api_key_still_works(hybrid_env: TestClient) -> None:
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {TEST_API_KEY}"})
    assert r.status_code == 200


# --- Role enforcement ---

@pytest.mark.slow
def test_viewer_cannot_create_library(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_VIEWER, "viewer")
    r = hybrid_env.post(
        "/v1/libraries",
        json={"name": "test", "root_path": "/tmp/test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


@pytest.mark.slow
def test_editor_can_create_library(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_EDITOR, "editor")
    r = hybrid_env.post(
        "/v1/libraries",
        json={"name": "editor-lib", "root_path": "/tmp/editor-test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201), f"Expected 200/201 but got {r.status_code}: {r.text}"


@pytest.mark.slow
def test_viewer_cannot_list_users(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_VIEWER, "viewer")
    r = hybrid_env.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.slow
def test_editor_cannot_list_users(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_EDITOR, "editor")
    r = hybrid_env.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.slow
def test_admin_can_list_users(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_ADMIN, "admin")
    r = hybrid_env.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    users = r.json()
    assert len(users) >= 3


# --- Password length on create user ---

@pytest.mark.slow
def test_create_user_password_too_short(hybrid_env: TestClient) -> None:
    token = _make_jwt(USR_ADMIN, "admin")
    r = hybrid_env.post(
        "/v1/users",
        json={"email": "short@test.com", "password": "short", "role": "viewer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "12" in r.json()["error"]["message"]


# --- Token refresh and revocation (users re-read from the database) ---

_PASSWORD = "correct-horse-battery-staple"


def _new_user(role: str) -> tuple[str, str]:
    """A fresh user of this tenant: (user_id, email)."""
    from src.server.database import get_control_session

    uid = f"usr_{uuid.uuid4().hex[:10]}"
    email = f"{uid}@test.com"
    ph = bcrypt.hashpw(_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    with get_control_session() as session:
        session.execute(
            text("INSERT INTO users (user_id, tenant_id, email, password_hash, role) VALUES (:u, :t, :e, :p, :r)"),
            {"u": uid, "t": TENANT_ID, "e": email, "p": ph, "r": role},
        )
        session.commit()
    return uid, email


def _login(client: TestClient, email: str) -> str:
    from src.server.api.rate_limit import login_limiter

    login_limiter._hits.clear()  # these tests log in more often than a person may
    r = client.post("/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _claims(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.slow
def test_refresh_keeps_the_original_refresh_window(hybrid_env: TestClient) -> None:
    _, email = _new_user("viewer")
    token = _login(hybrid_env, email)
    first = _claims(token)["refresh_exp"]
    for _ in range(2):
        r = hybrid_env.post("/v1/auth/refresh", headers=_bearer(token))
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        assert _claims(token)["refresh_exp"] == first  # no sliding


@pytest.mark.slow
def test_refresh_takes_the_role_from_the_database(hybrid_env: TestClient) -> None:
    from src.server.database import get_control_session

    uid, email = _new_user("editor")
    token = _login(hybrid_env, email)
    with get_control_session() as session:  # a role changed without a version bump
        session.execute(text("UPDATE users SET role = 'viewer' WHERE user_id = :u"), {"u": uid})
        session.commit()
    r = hybrid_env.post("/v1/auth/refresh", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert _claims(r.json()["access_token"])["role"] == "viewer"


@pytest.mark.slow
def test_middleware_takes_the_role_from_the_database(hybrid_env: TestClient) -> None:
    from src.server.database import get_control_session

    uid, email = _new_user("editor")
    token = _login(hybrid_env, email)
    with get_control_session() as session:  # a role changed without a version bump
        session.execute(text("UPDATE users SET role = 'viewer' WHERE user_id = :u"), {"u": uid})
        session.commit()
    r = hybrid_env.post("/v1/libraries", json={"name": f"nope-{uid}", "root_path": "/x"}, headers=_bearer(token))
    assert r.status_code == 403


@pytest.mark.slow
def test_refresh_refused_for_a_deleted_user(hybrid_env: TestClient) -> None:
    from src.server.database import get_control_session

    uid, email = _new_user("viewer")
    token = _login(hybrid_env, email)
    with get_control_session() as session:
        session.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": uid})
        session.commit()
    assert hybrid_env.post("/v1/auth/refresh", headers=_bearer(token)).status_code == 401
    assert hybrid_env.get("/v1/libraries", headers=_bearer(token)).status_code == 401


@pytest.mark.slow
def test_a_token_refreshes_once(hybrid_env: TestClient) -> None:
    _, email = _new_user("viewer")
    token = _login(hybrid_env, email)
    assert hybrid_env.post("/v1/auth/refresh", headers=_bearer(token)).status_code == 200
    assert hybrid_env.post("/v1/auth/refresh", headers=_bearer(token)).status_code == 401


@pytest.mark.slow
def test_role_change_revokes_tokens(hybrid_env: TestClient) -> None:
    uid, email = _new_user("editor")
    token = _login(hybrid_env, email)
    assert hybrid_env.get("/v1/libraries", headers=_bearer(token)).status_code == 200
    admin = _login(hybrid_env, f"admin_{_RUN_ID}@test.com")
    r = hybrid_env.patch(f"/v1/users/{uid}", json={"role": "viewer"}, headers=_bearer(admin))
    assert r.status_code == 200, r.text
    assert hybrid_env.get("/v1/libraries", headers=_bearer(token)).status_code == 401
    assert hybrid_env.post("/v1/auth/refresh", headers=_bearer(token)).status_code == 401
    # A new login carries the new role.
    assert _claims(_login(hybrid_env, email))["role"] == "viewer"


@pytest.mark.slow
def test_deleting_a_user_revokes_tokens(hybrid_env: TestClient) -> None:
    uid, email = _new_user("viewer")
    token = _login(hybrid_env, email)
    admin = _login(hybrid_env, f"admin_{_RUN_ID}@test.com")
    assert hybrid_env.delete(f"/v1/users/{uid}", headers=_bearer(admin)).status_code == 204
    assert hybrid_env.get("/v1/libraries", headers=_bearer(token)).status_code == 401


@pytest.mark.slow
def test_password_reset_revokes_tokens(hybrid_env: TestClient) -> None:
    from datetime import timedelta

    from src.server.database import get_control_session
    from src.server.repository.control_plane import PasswordResetTokenRepository
    from src.shared.utils import utcnow

    uid, email = _new_user("viewer")
    token = _login(hybrid_env, email)
    with get_control_session() as session:
        PasswordResetTokenRepository(session).create(uid, "reset-me-please", utcnow() + timedelta(hours=1))
    r = hybrid_env.post("/v1/auth/reset-password",
                        json={"token": "reset-me-please", "password": "another-long-password"})
    assert r.status_code == 204, r.text
    assert hybrid_env.get("/v1/libraries", headers=_bearer(token)).status_code == 401
    assert hybrid_env.post("/v1/auth/refresh", headers=_bearer(token)).status_code == 401


@pytest.mark.slow
def test_token_of_an_older_version_is_refused(hybrid_env: TestClient) -> None:
    from src.server.database import get_control_session

    uid, _ = _new_user("viewer")
    with get_control_session() as session:
        session.execute(text("UPDATE users SET token_version = 3 WHERE user_id = :u"), {"u": uid})
        session.commit()
    assert hybrid_env.get("/v1/libraries", headers=_bearer(_make_jwt(uid, "viewer", tv=2))).status_code == 401
    assert hybrid_env.get("/v1/libraries", headers=_bearer(_make_jwt(uid, "viewer", tv=3))).status_code == 200

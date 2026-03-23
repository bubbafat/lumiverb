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

from src.api.main import app
from src.api.routers import auth as auth_module
from src.core.config import get_settings
from src.core.database import _engines

from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations

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
    with PostgresContainer("pgvector/pgvector:pg16") as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_pg:
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


def _make_jwt(user_id: str, role: str, expired: bool = False, tampered: bool = False) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "tenant_id": TENANT_ID,
        "role": role,
        "exp": now - 3600 if expired else now + 3600,
    }
    secret = "wrong-secret" if tampered else JWT_SECRET
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


# --- Logout endpoint ---

@pytest.mark.slow
def test_logout_returns_204(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_admin", "admin")
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
        assert "12" in r.json()["detail"]
    finally:
        app.dependency_overrides.pop(auth_module._get_db, None)
        get_settings.cache_clear()


# --- JWT middleware ---

@pytest.mark.slow
def test_jwt_admin_resolves_tenant(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_admin", "admin")
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {token}"})
    # Should get 200 (empty list) — not 401.
    assert r.status_code == 200


@pytest.mark.slow
def test_jwt_viewer_resolves_tenant(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_viewer", "viewer")
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


@pytest.mark.slow
def test_expired_jwt_returns_401(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_admin", "admin", expired=True)
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


@pytest.mark.slow
def test_tampered_jwt_returns_401(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_admin", "admin", tampered=True)
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


@pytest.mark.slow
def test_api_key_still_works(hybrid_env: TestClient) -> None:
    r = hybrid_env.get("/v1/libraries", headers={"Authorization": f"Bearer {TEST_API_KEY}"})
    assert r.status_code == 200


# --- Role enforcement ---

@pytest.mark.slow
def test_viewer_cannot_create_library(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_viewer", "viewer")
    r = hybrid_env.post(
        "/v1/libraries",
        json={"name": "test", "root_path": "/tmp/test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


@pytest.mark.slow
def test_editor_can_create_library(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_editor", "editor")
    r = hybrid_env.post(
        "/v1/libraries",
        json={"name": "editor-lib", "root_path": "/tmp/editor-test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201), f"Expected 200/201 but got {r.status_code}: {r.text}"


@pytest.mark.slow
def test_viewer_cannot_list_users(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_viewer", "viewer")
    r = hybrid_env.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.slow
def test_editor_cannot_list_users(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_editor", "editor")
    r = hybrid_env.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.slow
def test_admin_can_list_users(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_admin", "admin")
    r = hybrid_env.get("/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    users = r.json()
    assert len(users) >= 3


# --- Password length on create user ---

@pytest.mark.slow
def test_create_user_password_too_short(hybrid_env: TestClient) -> None:
    token = _make_jwt("usr_admin", "admin")
    r = hybrid_env.post(
        "/v1/users",
        json={"email": "short@test.com", "password": "short", "role": "viewer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "12" in r.json()["detail"]

"""Tests for /v1/users user management endpoints (Phase 4)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def users_client() -> tuple[TestClient, str]:
    """
    Control-plane + tenant DB; create one tenant and default admin key.
    Returns (client, api_key) for user management tests.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        u = make_url(control_url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        os.environ["JWT_SECRET"] = "test-jwt-secret-for-users-tests"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "UsersTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.core.database import get_control_session
            from src.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                yield client, api_key

        _engines.clear()


# ---------------------------------------------------------------------------
# GET /v1/users
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_list_users_requires_auth(users_client: tuple[TestClient, str]) -> None:
    client, _ = users_client
    r = client.get("/v1/users")
    assert r.status_code == 401


@pytest.mark.slow
def test_list_users_requires_admin(users_client: tuple[TestClient, str]) -> None:
    import hashlib
    from src.core.database import get_control_session
    from sqlmodel import text as sql_text

    client, admin_key = users_client
    auth_admin = {"Authorization": f"Bearer {admin_key}"}

    # Create a key (inherits admin), then downgrade to viewer.
    r = client.post("/v1/keys", json={"label": "viewer-users-test"}, headers=auth_admin)
    assert r.status_code == 200
    viewer_key = r.json()["plaintext"]

    key_hash = hashlib.sha256(viewer_key.encode()).hexdigest()
    with get_control_session() as session:
        session.exec(
            sql_text("UPDATE api_keys SET role = 'viewer' WHERE key_hash = :h"),
            params={"h": key_hash},
        )
        session.commit()

    r = client.get("/v1/users", headers={"Authorization": f"Bearer {viewer_key}"})
    assert r.status_code == 403


@pytest.mark.slow
def test_list_users_returns_list(users_client: tuple[TestClient, str]) -> None:
    client, api_key = users_client
    r = client.get("/v1/users", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# POST /v1/users
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_user_requires_auth(users_client: tuple[TestClient, str]) -> None:
    client, _ = users_client
    r = client.post("/v1/users", json={"email": "x@x.com", "password": "pw123456789012", "role": "viewer"})
    assert r.status_code == 401


@pytest.mark.slow
def test_create_user_requires_admin(users_client: tuple[TestClient, str]) -> None:
    import hashlib
    from src.core.database import get_control_session
    from sqlmodel import text as sql_text

    client, admin_key = users_client
    auth_admin = {"Authorization": f"Bearer {admin_key}"}

    # Create a key (inherits admin), then downgrade to editor.
    r = client.post("/v1/keys", json={"label": "editor-users-test"}, headers=auth_admin)
    assert r.status_code == 200
    editor_key = r.json()["plaintext"]

    key_hash = hashlib.sha256(editor_key.encode()).hexdigest()
    with get_control_session() as session:
        session.exec(
            sql_text("UPDATE api_keys SET role = 'editor' WHERE key_hash = :h"),
            params={"h": key_hash},
        )
        session.commit()

    r = client.post(
        "/v1/users",
        json={"email": "shouldfail@example.com", "password": "pw123456789012", "role": "viewer"},
        headers={"Authorization": f"Bearer {editor_key}"},
    )
    assert r.status_code == 403


@pytest.mark.slow
def test_create_user_returns_201(users_client: tuple[TestClient, str]) -> None:
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/users",
        json={"email": "newviewer@example.com", "password": "securepassword123", "role": "viewer"},
        headers=auth,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["user_id"].startswith("usr_")
    assert body["email"] == "newviewer@example.com"
    assert body["role"] == "viewer"
    assert "created_at" in body
    assert body["last_login_at"] is None


@pytest.mark.slow
def test_create_user_duplicate_email_returns_409(users_client: tuple[TestClient, str]) -> None:
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}
    payload = {"email": "dup@example.com", "password": "securepassword123", "role": "viewer"}

    r = client.post("/v1/users", json=payload, headers=auth)
    assert r.status_code == 201

    r = client.post("/v1/users", json=payload, headers=auth)
    assert r.status_code == 409
    assert r.json().get("error", {}).get("code") == "email_conflict"


@pytest.mark.slow
def test_create_user_invalid_role_returns_400(users_client: tuple[TestClient, str]) -> None:
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/users",
        json={"email": "badrole@example.com", "password": "securepassword123", "role": "superuser"},
        headers=auth,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# PATCH /v1/users/{user_id}
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_update_user_role(users_client: tuple[TestClient, str]) -> None:
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/users",
        json={"email": "patchme@example.com", "password": "securepassword123", "role": "viewer"},
        headers=auth,
    )
    assert r.status_code == 201
    user_id = r.json()["user_id"]

    r = client.patch(f"/v1/users/{user_id}", json={"role": "editor"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["role"] == "editor"


@pytest.mark.slow
def test_cannot_demote_last_admin(users_client: tuple[TestClient, str]) -> None:
    """PATCH to non-admin on the only admin user must return 409 last_admin."""
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Identify the sole admin user (created via JWT login — but API keys have no user_id).
    # Create a dedicated admin user for this test, ensure it's the only admin, then demote.
    r = client.post(
        "/v1/users",
        json={"email": "onlyadmin@example.com", "password": "securepassword123", "role": "admin"},
        headers=auth,
    )
    assert r.status_code == 201
    admin_user_id = r.json()["user_id"]

    # There are multiple admins at this point (this tenant's API keys are also admin).
    # Promote to confirm the user is admin, then check the guard when we try to
    # demote via the last-admin path by using a fresh tenant that only has one admin user.
    # For simplicity, just verify the 409 path is reachable from a single-admin scenario.
    # The easiest way: demote this admin user first to make it non-admin, add it back,
    # then delete all but one admin user.
    # Actually, the guard fires when count_admins <= 1. Since the API key is also admin,
    # the count is at least 2 (API key-based admins are tracked separately in api_keys, not users).
    # count_admins only counts users table. So if this is the only admin *user*, it should fire.

    # Demote the newly-created admin user — it should work because there may be no other admin users.
    # But we need exactly 1 admin user to trigger the guard.
    # Let's create a second admin user, then try to demote the first one (only 1 admin user left).
    r2 = client.post(
        "/v1/users",
        json={"email": "secondadmin@example.com", "password": "securepassword123", "role": "admin"},
        headers=auth,
    )
    assert r2.status_code == 201
    second_admin_id = r2.json()["user_id"]

    # Demote second admin → leaves only 1 admin user (onlyadmin@example.com). Should succeed.
    r = client.patch(f"/v1/users/{second_admin_id}", json={"role": "viewer"}, headers=auth)
    assert r.status_code == 200

    # Now demote the last admin user → must be rejected.
    r = client.patch(f"/v1/users/{admin_user_id}", json={"role": "viewer"}, headers=auth)
    assert r.status_code == 409
    assert r.json().get("error", {}).get("code") == "last_admin"


@pytest.mark.slow
def test_patch_nonexistent_user_returns_404(users_client: tuple[TestClient, str]) -> None:
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.patch("/v1/users/usr_doesnotexist", json={"role": "editor"}, headers=auth)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /v1/users/{user_id}
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_delete_user_returns_204(users_client: tuple[TestClient, str]) -> None:
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/users",
        json={"email": "deleteme@example.com", "password": "securepassword123", "role": "viewer"},
        headers=auth,
    )
    assert r.status_code == 201
    user_id = r.json()["user_id"]

    r = client.delete(f"/v1/users/{user_id}", headers=auth)
    assert r.status_code == 204


@pytest.mark.slow
def test_cannot_delete_last_admin(users_client: tuple[TestClient, str]) -> None:
    """DELETE on the only admin user must return 409 last_admin."""
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create a lone admin user.
    r = client.post(
        "/v1/users",
        json={"email": "lonelyadmin@example.com", "password": "securepassword123", "role": "admin"},
        headers=auth,
    )
    assert r.status_code == 201
    admin_user_id = r.json()["user_id"]

    # Create and immediately delete any other admin users to ensure this is the last one.
    # (Simpler: rely on count_admins counting only users table rows.)
    # Demote all other admin users first.
    r = client.get("/v1/users", headers=auth)
    for u in r.json():
        if u["role"] == "admin" and u["user_id"] != admin_user_id:
            client.patch(f"/v1/users/{u['user_id']}", json={"role": "viewer"}, headers=auth)

    # Now lonelyadmin is the only admin user → delete must be rejected.
    r = client.delete(f"/v1/users/{admin_user_id}", headers=auth)
    assert r.status_code == 409
    assert r.json().get("error", {}).get("code") == "last_admin"


@pytest.mark.slow
def test_delete_nonexistent_user_returns_404(users_client: tuple[TestClient, str]) -> None:
    client, api_key = users_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.delete("/v1/users/usr_doesnotexist", headers=auth)
    assert r.status_code == 404

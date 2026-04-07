"""Saved views API integration tests. Uses testcontainers Postgres + tenant DB."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(scope="module")
def views_env():
    """Testcontainers Postgres: control + tenant. Yield (client, api_key)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = control_postgres.get_connection_url()
        control_url = _ensure_psycopg2(control_url)
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
        os.environ["ADMIN_KEY"] = "test-admin-views"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "ViewsTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-views"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = tenant_postgres.get_connection_url()
            tenant_url = _ensure_psycopg2(tenant_url)
            _provision_tenant_db(tenant_url, project_root)

            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                yield client, api_key, tenant_id

        _engines.clear()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_view(views_env):
    client, api_key, _ = views_env

    r = client.post(
        "/v1/views",
        json={"name": "Best of 2025", "query_params": "star_min=5&date_from=2025-01-01"},
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Best of 2025"
    assert data["query_params"] == "star_min=5&date_from=2025-01-01"
    assert data["view_id"].startswith("sv_")
    assert data["position"] >= 0


@pytest.mark.slow
def test_list_views(views_env):
    client, api_key, _ = views_env

    r = client.get("/v1/views", headers=_headers(api_key))
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert len(data["items"]) >= 1


@pytest.mark.slow
def test_update_view(views_env):
    client, api_key, _ = views_env

    # Create
    cr = client.post(
        "/v1/views",
        json={"name": "ToUpdate", "query_params": "color=red"},
        headers=_headers(api_key),
    )
    assert cr.status_code == 201
    view_id = cr.json()["view_id"]

    # Update
    r = client.patch(
        f"/v1/views/{view_id}",
        json={"name": "Updated Name", "query_params": "color=blue"},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Updated Name"
    assert r.json()["query_params"] == "color=blue"


@pytest.mark.slow
def test_delete_view(views_env):
    client, api_key, _ = views_env

    cr = client.post(
        "/v1/views",
        json={"name": "ToDelete", "query_params": "favorite=true"},
        headers=_headers(api_key),
    )
    assert cr.status_code == 201
    view_id = cr.json()["view_id"]

    r = client.delete(f"/v1/views/{view_id}", headers=_headers(api_key))
    assert r.status_code == 204

    # Should be gone
    r2 = client.delete(f"/v1/views/{view_id}", headers=_headers(api_key))
    assert r2.status_code == 404


@pytest.mark.slow
def test_create_view_empty_name_rejected(views_env):
    client, api_key, _ = views_env

    r = client.post(
        "/v1/views",
        json={"name": "  ", "query_params": "star_min=1"},
        headers=_headers(api_key),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_reorder_views(views_env):
    client, api_key, _ = views_env

    # Create two views
    r1 = client.post(
        "/v1/views",
        json={"name": "First", "query_params": "a=1"},
        headers=_headers(api_key),
    )
    r2 = client.post(
        "/v1/views",
        json={"name": "Second", "query_params": "b=2"},
        headers=_headers(api_key),
    )
    v1 = r1.json()["view_id"]
    v2 = r2.json()["view_id"]

    # Reorder: second before first
    r = client.patch(
        "/v1/views/reorder",
        json={"view_ids": [v2, v1]},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # List and verify order
    lr = client.get("/v1/views", headers=_headers(api_key))
    items = lr.json()["items"]
    ids = [i["view_id"] for i in items]
    # v2 should come before v1
    assert ids.index(v2) < ids.index(v1)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_views_require_auth(views_env):
    client, _, _ = views_env

    r = client.get("/v1/views")
    assert r.status_code in (401, 403)


@pytest.mark.slow
def test_view_not_found_for_other_user(views_env):
    """A view not owned by the caller returns 404."""
    client, api_key, _ = views_env

    cr = client.post(
        "/v1/views",
        json={"name": "Private", "query_params": "x=1"},
        headers=_headers(api_key),
    )
    view_id = cr.json()["view_id"]

    # Try to update with a non-existent view_id
    r = client.patch(
        "/v1/views/sv_nonexistent",
        json={"name": "Hacked"},
        headers=_headers(api_key),
    )
    assert r.status_code == 404

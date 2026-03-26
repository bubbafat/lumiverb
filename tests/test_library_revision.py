"""API tests for GET /v1/libraries/{id}/revision endpoint."""

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
def revision_client() -> tuple[TestClient, str, str]:
    """
    Two testcontainers Postgres: control + tenant. Create tenant via admin API,
    provision tenant DB, return (client, api_key, library_id).
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
        os.environ["ADMIN_KEY"] = "test-admin-revision"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "RevisionTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-revision"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
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
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "RevisionLib", "root_path": "/rev"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]
                yield client, api_key, library_id

        _engines.clear()


@pytest.mark.slow
def test_revision_new_library(revision_client: tuple[TestClient, str, str]) -> None:
    """New library returns revision=0 and asset_count=0."""
    client, api_key, library_id = revision_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(f"/v1/libraries/{library_id}/revision", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["revision"] == 0
    assert data["asset_count"] == 0


@pytest.mark.slow
def test_revision_response_shape(revision_client: tuple[TestClient, str, str]) -> None:
    """Response has library_id, revision, and asset_count fields."""
    client, api_key, library_id = revision_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(f"/v1/libraries/{library_id}/revision", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert "library_id" in data
    assert "revision" in data
    assert "asset_count" in data
    assert data["library_id"] == library_id
    assert isinstance(data["revision"], int)
    assert isinstance(data["asset_count"], int)


@pytest.mark.slow
def test_revision_nonexistent_library(revision_client: tuple[TestClient, str, str]) -> None:
    """Nonexistent library returns 404."""
    client, api_key, _ = revision_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/libraries/lib_nonexistent/revision", headers=auth)
    assert r.status_code == 404

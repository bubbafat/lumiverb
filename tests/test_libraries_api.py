"""Libraries API tests. TestClient + testcontainers Postgres; create tenant via admin API with provision_tenant_database mocked, then provision tenant DB manually."""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines


def _ensure_psycopg2(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _run_control_migrations(url: str) -> None:
    env = os.environ.copy()
    env["ALEMBIC_CONTROL_URL"] = url
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic-control.ini", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def _provision_tenant_db_second_container(
    tenant_url: str,
    project_root: str,
) -> None:
    """Run tenant migrations on a second Postgres container (tenant_url)."""
    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    engine.dispose()

    env = os.environ.copy()
    env["ALEMBIC_TENANT_URL"] = tenant_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.fixture(scope="module")
def libraries_client() -> tuple[TestClient, str]:
    """
    Two testcontainers Postgres: one for control plane, one for tenant. Create a real tenant
    via the admin API (with provision_tenant_database mocked), then point routing at the
    second container and run tenant migrations there. Returns (client, api_key).
    """
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
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "LibrariesTestTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        # Second container for tenant DB; run tenant migrations, then point routing at it
        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = tenant_postgres.get_connection_url()
            tenant_url = _ensure_psycopg2(tenant_url)
            _provision_tenant_db_second_container(tenant_url, project_root)

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


@pytest.mark.slow
def test_create_library(libraries_client: tuple[TestClient, str]) -> None:
    """POST /v1/libraries returns 200 and library_id starts with lib_."""
    client, api_key = libraries_client
    r = client.post(
        "/v1/libraries",
        json={"name": "My Photos", "root_path": "/photos"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["library_id"].startswith("lib_")
    assert data["name"] == "My Photos"
    assert data["root_path"] == "/photos"
    assert data["scan_status"] == "idle"


@pytest.mark.slow
def test_create_library_duplicate_name(libraries_client: tuple[TestClient, str]) -> None:
    """POST same name twice: second request returns 409."""
    client, api_key = libraries_client
    name = "UniqueName_" + __import__("secrets").token_urlsafe(8)
    r1 = client.post(
        "/v1/libraries",
        json={"name": name, "root_path": "/path1"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/v1/libraries",
        json={"name": name, "root_path": "/path2"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r2.status_code == 409


@pytest.mark.slow
def test_list_libraries(libraries_client: tuple[TestClient, str]) -> None:
    """Create two libraries, GET /v1/libraries returns both."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    client.post("/v1/libraries", json={"name": "ListA", "root_path": "/a"}, headers=auth)
    client.post("/v1/libraries", json={"name": "ListB", "root_path": "/b"}, headers=auth)
    r = client.get("/v1/libraries", headers=auth)
    assert r.status_code == 200
    libraries = r.json()
    names = {lib["name"] for lib in libraries}
    assert "ListA" in names
    assert "ListB" in names
    for lib in libraries:
        assert "library_id" in lib
        assert lib["library_id"].startswith("lib_")
        assert "name" in lib
        assert "root_path" in lib
        assert "scan_status" in lib
        assert "last_scan_at" in lib


@pytest.mark.slow
def test_create_library_requires_auth(libraries_client: tuple[TestClient, str]) -> None:
    """POST /v1/libraries without Authorization header returns 401."""
    client, _ = libraries_client
    r = client.post(
        "/v1/libraries",
        json={"name": "NoAuth", "root_path": "/nope"},
    )
    assert r.status_code == 401

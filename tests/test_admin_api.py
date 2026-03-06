"""Admin API tests. Use TestClient and testcontainers Postgres; mock provision_tenant_database."""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
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


@pytest.fixture
def admin_client() -> TestClient:
    """FastAPI TestClient with control plane DB and ADMIN_KEY set; provision_tenant_database mocked."""
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        url = _ensure_psycopg2(url)
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()

        _run_control_migrations(url)

        from sqlalchemy.engine import make_url
        u = make_url(url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                yield client

        _engines.clear()


@pytest.mark.slow
def test_health_no_auth() -> None:
    """GET /health returns ok without auth."""
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.slow
def test_create_tenant_returns_api_key(admin_client: TestClient) -> None:
    """POST /v1/admin/tenants returns 200 with tenant_id and api_key starting with lv_."""
    r = admin_client.post(
        "/v1/admin/tenants",
        json={"name": "Acme", "plan": "free", "email": "admin@acme.com"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "tenant_id" in data
    assert data["tenant_id"].startswith("ten_")
    assert "api_key" in data
    assert data["api_key"].startswith("lv_")
    assert data.get("database") == "provisioned"


@pytest.mark.slow
def test_create_tenant_requires_admin_key(admin_client: TestClient) -> None:
    """POST without auth header returns 401."""
    r = admin_client.post(
        "/v1/admin/tenants",
        json={"name": "Acme", "plan": "free"},
    )
    assert r.status_code == 401


@pytest.mark.slow
def test_list_tenants(admin_client: TestClient) -> None:
    """Create two tenants, GET /v1/admin/tenants returns both."""
    for name in ("Acme", "Globex"):
        admin_client.post(
            "/v1/admin/tenants",
            json={"name": name, "plan": "free"},
            headers={"Authorization": "Bearer test-admin-secret"},
        )
    r = admin_client.get(
        "/v1/admin/tenants",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert r.status_code == 200
    tenants = r.json()
    assert len(tenants) >= 2
    names = {t["name"] for t in tenants}
    assert "Acme" in names
    assert "Globex" in names
    for t in tenants:
        assert "tenant_id" in t
        assert "name" in t
        assert "plan" in t
        assert "status" in t
        assert "api_key" not in t


@pytest.mark.slow
def test_delete_tenant_soft_deletes(admin_client: TestClient) -> None:
    """Create tenant, DELETE, then list shows status deleted."""
    create = admin_client.post(
        "/v1/admin/tenants",
        json={"name": "ToDelete", "plan": "free"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert create.status_code == 200
    tenant_id = create.json()["tenant_id"]

    r = admin_client.delete(
        f"/v1/admin/tenants/{tenant_id}",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert r.status_code == 204

    list_r = admin_client.get(
        "/v1/admin/tenants",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert list_r.status_code == 200
    tenants = [t for t in list_r.json() if t["tenant_id"] == tenant_id]
    assert len(tenants) == 1
    assert tenants[0]["status"] == "deleted"

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


@pytest.fixture(scope="module")
def admin_client() -> TestClient:
    """FastAPI TestClient with control plane DB and ADMIN_KEY set; provision_tenant_database mocked. One container per module."""
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


@pytest.mark.slow
def test_create_additional_key(admin_client: TestClient) -> None:
    """Create a tenant, then create a second key; assert raw key returned and different from first."""
    create = admin_client.post(
        "/v1/admin/tenants",
        json={"name": "KeysTenant", "plan": "free"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert create.status_code == 200
    tenant_id = create.json()["tenant_id"]
    first_key = create.json()["api_key"]
    assert first_key.startswith("lv_")

    r = admin_client.post(
        f"/v1/admin/tenants/{tenant_id}/keys",
        json={"name": "robert-macbook"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["api_key"].startswith("lv_")
    assert data["api_key"] != first_key
    assert data["name"] == "robert-macbook"
    assert data["tenant_id"] == tenant_id


@pytest.mark.slow
def test_create_key_unknown_tenant(admin_client: TestClient) -> None:
    """POST /v1/admin/tenants/{tenant_id}/keys for non-existent tenant returns 404."""
    r = admin_client.post(
        "/v1/admin/tenants/ten_nonexistent9999/keys",
        json={"name": "some-key"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_create_key_soft_deleted_tenant_returns_404(admin_client: TestClient) -> None:
    """POST /v1/admin/tenants/{tenant_id}/keys for soft-deleted tenant returns 404."""
    create = admin_client.post(
        "/v1/admin/tenants",
        json={"name": "ToDeleteForKeys", "plan": "free"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert create.status_code == 200
    tenant_id = create.json()["tenant_id"]
    admin_client.delete(
        f"/v1/admin/tenants/{tenant_id}",
        headers={"Authorization": "Bearer test-admin-secret"},
    )

    r = admin_client.post(
        f"/v1/admin/tenants/{tenant_id}/keys",
        json={"name": "some-key"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_list_keys(admin_client: TestClient) -> None:
    """Create tenant, create two additional keys, GET returns all three with correct names."""
    create = admin_client.post(
        "/v1/admin/tenants",
        json={"name": "ListKeysTenant", "plan": "free"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert create.status_code == 200
    tenant_id = create.json()["tenant_id"]

    admin_client.post(
        f"/v1/admin/tenants/{tenant_id}/keys",
        json={"name": "key-one"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    admin_client.post(
        f"/v1/admin/tenants/{tenant_id}/keys",
        json={"name": "key-two"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )

    r = admin_client.get(
        f"/v1/admin/tenants/{tenant_id}/keys",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert r.status_code == 200
    keys = r.json()
    assert len(keys) == 3  # default + key-one + key-two
    names = {k["name"] for k in keys}
    assert names == {"default", "key-one", "key-two"}
    for k in keys:
        assert k["tenant_id"] == tenant_id
        assert "name" in k
        assert "created_at" in k
        assert "api_key" not in k


@pytest.mark.slow
def test_create_key_requires_admin_auth(admin_client: TestClient) -> None:
    """POST without admin key returns 401."""
    create = admin_client.post(
        "/v1/admin/tenants",
        json={"name": "AuthTestTenant", "plan": "free"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert create.status_code == 200
    tenant_id = create.json()["tenant_id"]

    r = admin_client.post(
        f"/v1/admin/tenants/{tenant_id}/keys",
        json={"name": "unauthorized-key"},
    )
    assert r.status_code == 401

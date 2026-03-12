"""API tests for GET /v1/assets/by-path."""

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


def _provision_tenant_db(tenant_url: str, project_root: str) -> None:
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
def assets_client() -> tuple[TestClient, str]:
    """Two testcontainers Postgres; create tenant; yield (client, api_key)."""
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
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "AssetsByPathTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
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
                yield client, api_key
        _engines.clear()


@pytest.mark.slow
def test_get_asset_by_path_happy_path(assets_client: tuple[TestClient, str]) -> None:
    """Create library + asset; GET /v1/assets/by-path returns matching AssetResponse."""
    client, api_key = assets_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_lib = client.post(
        "/v1/libraries",
        json={
            "name": "AssetsByPathLib",
            "root_path": "/x",
        },
        headers=auth,
    )
    assert r_lib.status_code == 200
    library_id = r_lib.json()["library_id"]

    # Use scans API to create an asset via batch add
    r_scan = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan.status_code == 200
    scan_id = r_scan.json()["scan_id"]

    rel_path = "photos/one.jpg"
    client.post(
        f"/v1/scans/{scan_id}/batch",
        json={
            "items": [
                {
                    "action": "add",
                    "rel_path": rel_path,
                    "file_size": 123,
                    "file_mtime": "2025-01-01T12:00:00Z",
                    "media_type": "image",
                }
            ]
        },
        headers=auth,
    )

    # Resolve by path
    r = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": rel_path},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["library_id"] == library_id
    assert body["rel_path"] == rel_path
    assert body["asset_id"].startswith("ast_")


@pytest.mark.slow
def test_get_asset_by_path_404_when_missing(assets_client: tuple[TestClient, str]) -> None:
    """GET /v1/assets/by-path returns 404 for unknown rel_path."""
    client, api_key = assets_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_lib = client.post(
        "/v1/libraries",
        json={
            "name": "AssetsByPathLibMissing",
            "root_path": "/x",
        },
        headers=auth,
    )
    assert r_lib.status_code == 200
    library_id = r_lib.json()["library_id"]

    r = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": "does/not/exist.jpg"},
        headers=auth,
    )
    assert r.status_code == 404


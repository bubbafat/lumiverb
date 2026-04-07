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

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines


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

        with patch("src.server.api.routers.admin.provision_tenant_database"):
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

    rel_path = "photos/one.jpg"
    client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 123,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "image",
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
def test_stream_proxy_happy_path(assets_client: tuple[TestClient, str], tmp_path: Path) -> None:
    """Create library + asset + proxy file; GET /v1/assets/{asset_id}/proxy streams JPEG bytes."""
    from src.server.config import get_settings
    from src.server.database import get_tenant_session
    from src.server.repository.tenant import AssetRepository
    from src.server.storage.local import get_storage

    client, api_key = assets_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create library
    r_lib = client.post(
        "/v1/libraries",
        json={
            "name": "AssetsStreamLib",
            "root_path": "/x",
        },
        headers=auth,
    )
    assert r_lib.status_code == 200
    library_id = r_lib.json()["library_id"]

    rel_path = "photos/proxy.jpg"
    client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 123,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "image",
        },
        headers=auth,
    )

    # Resolve asset_id
    r_asset = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": rel_path},
        headers=auth,
    )
    assert r_asset.status_code == 200
    asset_id = r_asset.json()["asset_id"]

    # Look up tenant_id and patch proxy_key on the asset to point at a real file on disk.
    ctx = client.get("/v1/tenant/context", headers=auth)
    assert ctx.status_code == 200
    tenant_id = ctx.json()["tenant_id"]

    settings = get_settings()
    storage = get_storage()

    # Write a JPEG-like payload to the expected storage path.
    from sqlalchemy import text as _text

    from src.server.database import _engines, get_control_session
    from src.server.repository.control_plane import TenantDbRoutingRepository

    # Resolve tenant connection string to open tenant session.
    with get_control_session() as control_session:
        routing_repo = TenantDbRoutingRepository(control_session)
        row = routing_repo.get_by_tenant_id(tenant_id)
        assert row is not None
        tenant_url = row.connection_string

    _engines.clear()
    from sqlalchemy import create_engine

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        conn.execute(_text("UPDATE assets SET proxy_key = :key WHERE asset_id = :asset_id"), {"key": f"{tenant_id}/{library_id}/proxies/00/{asset_id}.jpg", "asset_id": asset_id})
        conn.commit()
    engine.dispose()
    _engines.clear()

    proxy_key = f"{tenant_id}/{library_id}/proxies/00/{asset_id}.jpg"
    proxy_path = storage.abs_path(proxy_key)
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"\xff\xd8\xff" + b"\x00" * 10
    proxy_path.write_bytes(payload)

    # Stream proxy via API
    r_stream = client.get(f"/v1/assets/{asset_id}/proxy", headers=auth)
    assert r_stream.status_code == 200
    assert r_stream.headers["content-type"] == "image/jpeg"
    assert r_stream.content == payload


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


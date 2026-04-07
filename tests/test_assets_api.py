"""API tests for assets: list, get, thumbnail streaming."""

import os
import subprocess
import sys
from pathlib import Path
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


@pytest.fixture(scope="module")
def assets_api_client() -> tuple[TestClient, str, str, list[str]]:
    """
    Two testcontainers Postgres; provision tenant DB; create library + 2-3 assets via upsert.
    Yields (client, api_key, library_id, asset_ids).
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
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "AssetsAPITenant", "plan": "free"},
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
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "AssetsAPILib", "root_path": "/photos"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                asset_ids: list[str] = []
                for i, rel_path in enumerate(["a.jpg", "b.jpg", "c.png"]):
                    r_up = client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": library_id,
                            "rel_path": rel_path,
                            "file_size": 1000 + i,
                            "file_mtime": "2025-01-01T12:00:00Z",
                            "media_type": "image",
                        },
                        headers=auth,
                    )
                    assert r_up.status_code == 200
                    r_by_path = client.get(
                        "/v1/assets/by-path",
                        params={"library_id": library_id, "rel_path": rel_path},
                        headers=auth,
                    )
                    assert r_by_path.status_code == 200
                    asset_ids.append(r_by_path.json()["asset_id"])

                yield client, api_key, library_id, asset_ids

        _engines.clear()


@pytest.mark.slow
def test_list_assets_by_library(assets_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """GET /v1/assets?library_id=... returns the assets for that library."""
    client, api_key, library_id, asset_ids = assets_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/assets", params={"library_id": library_id}, headers=auth)
    assert r.status_code == 200
    assets = r.json()
    assert len(assets) >= 3
    ids = {a["asset_id"] for a in assets}
    for aid in asset_ids:
        assert aid in ids
    for a in assets:
        assert a["library_id"] == library_id
        assert "rel_path" in a
        assert "media_type" in a
        assert "status" in a


@pytest.mark.slow
def test_list_assets_empty_library(assets_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """GET /v1/assets?library_id=... on a library with no assets returns []."""
    client, api_key, _, _ = assets_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_lib = client.post(
        "/v1/libraries",
        json={"name": "EmptyLib", "root_path": "/empty"},
        headers=auth,
    )
    assert r_lib.status_code == 200
    library_id = r_lib.json()["library_id"]

    r = client.get("/v1/assets", params={"library_id": library_id}, headers=auth)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.slow
def test_list_assets_no_filter(assets_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """GET /v1/assets (no library_id) returns all assets across the tenant."""
    client, api_key, library_id, asset_ids = assets_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/assets", headers=auth)
    assert r.status_code == 200
    assets = r.json()
    assert len(assets) >= 3
    ids = {a["asset_id"] for a in assets}
    for aid in asset_ids:
        assert aid in ids


@pytest.mark.slow
def test_get_asset_by_id(assets_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """GET /v1/assets/{asset_id} returns correct AssetResponse."""
    client, api_key, library_id, asset_ids = assets_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = asset_ids[0]
    r = client.get(f"/v1/assets/{asset_id}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["asset_id"] == asset_id
    assert body["library_id"] == library_id
    assert body["rel_path"] == "a.jpg"
    assert body["media_type"] == "image"
    assert "status" in body


@pytest.mark.slow
def test_get_asset_by_id_404(assets_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """GET /v1/assets/{asset_id} with unknown ID returns 404."""
    client, api_key, _, _ = assets_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/assets/ast_nonexistent0000000000000", headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
def test_list_assets_requires_auth(assets_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """Missing Authorization header returns 401."""
    client, _, library_id, _ = assets_api_client

    r = client.get("/v1/assets", params={"library_id": library_id})
    assert r.status_code == 401


@pytest.mark.slow
def test_stream_thumbnail_happy_path(
    assets_api_client: tuple[TestClient, str, str, list[str]], tmp_path: Path
) -> None:
    """Write real thumbnail to tmp_path, set thumbnail_key via API, GET streams bytes."""
    from src.server.storage.local import get_storage

    client, api_key, library_id, asset_ids = assets_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    ctx = client.get("/v1/tenant/context", headers=auth)
    assert ctx.status_code == 200
    tenant_id = ctx.json()["tenant_id"]

    asset_id = asset_ids[0]
    thumbnail_key = f"{tenant_id}/{library_id}/thumbnails/00/{asset_id}.jpg"
    r_key = client.post(
        f"/v1/assets/{asset_id}/thumbnail-key",
        json={"thumbnail_key": thumbnail_key},
        headers=auth,
    )
    assert r_key.status_code == 200

    storage = get_storage()
    thumb_path = storage.abs_path(thumbnail_key)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"\xff\xd8\xff" + b"\x00" * 10
    thumb_path.write_bytes(payload)

    r = client.get(f"/v1/assets/{asset_id}/thumbnail", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == payload


@pytest.mark.slow
def test_set_thumbnail_key(assets_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """POST /v1/assets/{id}/thumbnail-key returns {asset_id, thumbnail_key}."""
    client, api_key, library_id, asset_ids = assets_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = asset_ids[0]
    thumbnail_key = "tenant/lib/thumb/test.jpg"
    r = client.post(
        f"/v1/assets/{asset_id}/thumbnail-key",
        json={"thumbnail_key": thumbnail_key},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["asset_id"] == asset_id
    assert body["thumbnail_key"] == thumbnail_key

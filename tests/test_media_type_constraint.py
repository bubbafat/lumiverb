"""Test that the media_type column only accepts 'image' or 'video'."""

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
def media_type_client():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
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
                    json={"name": "MediaTypeTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
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

            tenant_engine = create_engine(tenant_url)

            with TestClient(app) as client:
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "MediaTypeLib", "root_path": "/mt"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                r_scan = client.post(
                    "/v1/scans",
                    json={"library_id": library_id, "status": "running"},
                    headers=auth,
                )
                assert r_scan.status_code == 200
                scan_id = r_scan.json()["scan_id"]

                yield client, auth, library_id, scan_id, tenant_engine

        _engines.clear()


@pytest.mark.slow
def test_image_media_type_accepted(media_type_client):
    """media_type='image' is accepted."""
    client, auth, library_id, scan_id, _ = media_type_client
    r = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": "photo.jpg",
            "file_size": 1000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "image",
            "scan_id": scan_id,
        },
        headers=auth,
    )
    assert r.status_code == 200


@pytest.mark.slow
def test_video_media_type_accepted(media_type_client):
    """media_type='video' is accepted."""
    client, auth, library_id, scan_id, _ = media_type_client
    r = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": "clip.mp4",
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
            "scan_id": scan_id,
        },
        headers=auth,
    )
    assert r.status_code == 200


@pytest.mark.slow
def test_invalid_media_type_rejected(media_type_client):
    """media_type='image/jpeg' (or any non-allowed value) is rejected by the DB constraint."""
    _, _, library_id, scan_id, tenant_engine = media_type_client
    with tenant_engine.connect() as conn:
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError, match="ck_assets_media_type"):
            conn.execute(text("""
                INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, status, availability, created_at, updated_at)
                VALUES ('ast_test_bad', :lib, 'bad.txt', 100, 'image/jpeg', 'pending', 'online', NOW(), NOW())
            """), {"lib": library_id})
            conn.commit()

"""Scans API tests: POST /v1/scans/{scan_id}/batch with each action type."""

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
def scans_client() -> tuple[TestClient, str]:
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
                    json={"name": "ScansApiTestTenant", "plan": "free"},
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
def test_batch_add(
    scans_client: tuple[TestClient, str],
) -> None:
    """POST batch with add actions; assert added count."""
    client, api_key = scans_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "BatchAddLib_" + __import__("secrets").token_urlsafe(6), "root_path": "/x"},
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

    r_batch = client.post(
        f"/v1/scans/{scan_id}/batch",
        json={
            "items": [
                {"action": "add", "rel_path": "a.jpg", "file_size": 100, "file_mtime": "2025-01-01T12:00:00Z", "media_type": "image"},
                {"action": "add", "rel_path": "b.jpg", "file_size": 200, "file_mtime": "2025-01-01T12:00:01Z", "media_type": "image"},
            ],
        },
        headers=auth,
    )
    assert r_batch.status_code == 200
    data = r_batch.json()
    assert data["added"] == 2
    assert data["updated"] == 0
    assert data["skipped"] == 0
    assert data["missing"] == 0


@pytest.mark.slow
def test_batch_skip(
    scans_client: tuple[TestClient, str],
) -> None:
    """Add assets via batch, then batch with skip; assert skipped count."""
    client, api_key = scans_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "BatchSkipLib_" + __import__("secrets").token_urlsafe(6), "root_path": "/x"},
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

    client.post(
        f"/v1/scans/{scan_id}/batch",
        json={
            "items": [
                {"action": "add", "rel_path": "s1.jpg", "file_size": 10, "file_mtime": "2025-01-01T12:00:00Z", "media_type": "image"},
                {"action": "add", "rel_path": "s2.jpg", "file_size": 20, "file_mtime": "2025-01-01T12:00:01Z", "media_type": "image"},
            ],
        },
        headers=auth,
    )

    r_page = client.get("/v1/assets/page", params={"library_id": library_id}, headers=auth)
    assert r_page.status_code == 200
    page = r_page.json()["items"]
    assert len(page) == 2
    asset_ids = [a["asset_id"] for a in page]

    r_batch = client.post(
        f"/v1/scans/{scan_id}/batch",
        json={"items": [{"action": "skip", "asset_id": aid} for aid in asset_ids]},
        headers=auth,
    )
    assert r_batch.status_code == 200
    data = r_batch.json()
    assert data["skipped"] == 2
    assert data["added"] == 0


@pytest.mark.slow
def test_batch_update(
    scans_client: tuple[TestClient, str],
) -> None:
    """Add asset via batch, then batch with update; assert updated count."""
    client, api_key = scans_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "BatchUpdateLib_" + __import__("secrets").token_urlsafe(6), "root_path": "/x"},
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

    client.post(
        f"/v1/scans/{scan_id}/batch",
        json={
            "items": [
                {"action": "add", "rel_path": "u.jpg", "file_size": 100, "file_mtime": "2025-01-01T12:00:00Z", "media_type": "image"},
            ],
        },
        headers=auth,
    )

    r_page = client.get("/v1/assets/page", params={"library_id": library_id}, headers=auth)
    assert r_page.status_code == 200
    asset_id = r_page.json()["items"][0]["asset_id"]

    r_batch = client.post(
        f"/v1/scans/{scan_id}/batch",
        json={
            "items": [
                {"action": "update", "asset_id": asset_id, "file_size": 200, "file_mtime": "2025-01-02T12:00:00Z"},
            ],
        },
        headers=auth,
    )
    assert r_batch.status_code == 200
    assert r_batch.json()["updated"] == 1


@pytest.mark.slow
def test_batch_missing(
    scans_client: tuple[TestClient, str],
) -> None:
    """Add asset via batch, then batch with missing; assert missing count."""
    client, api_key = scans_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "BatchMissingLib_" + __import__("secrets").token_urlsafe(6), "root_path": "/x"},
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

    client.post(
        f"/v1/scans/{scan_id}/batch",
        json={
            "items": [
                {"action": "add", "rel_path": "m.jpg", "file_size": 100, "file_mtime": "2025-01-01T12:00:00Z", "media_type": "image"},
            ],
        },
        headers=auth,
    )

    r_page = client.get("/v1/assets/page", params={"library_id": library_id}, headers=auth)
    assert r_page.status_code == 200
    asset_id = r_page.json()["items"][0]["asset_id"]

    r_batch = client.post(
        f"/v1/scans/{scan_id}/batch",
        json={"items": [{"action": "missing", "asset_id": asset_id}]},
        headers=auth,
    )
    assert r_batch.status_code == 200
    assert r_batch.json()["missing"] == 1


@pytest.mark.slow
def test_batch_mixed(
    scans_client: tuple[TestClient, str],
) -> None:
    """One batch with add, skip, update, missing; assert all counts."""
    client, api_key = scans_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "BatchMixedLib_" + __import__("secrets").token_urlsafe(6), "root_path": "/x"},
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

    client.post(
        f"/v1/scans/{scan_id}/batch",
        json={
            "items": [
                {"action": "add", "rel_path": "skip1.jpg", "file_size": 1, "file_mtime": "2025-01-01T12:00:00Z", "media_type": "image"},
                {"action": "add", "rel_path": "skip2.jpg", "file_size": 2, "file_mtime": "2025-01-01T12:00:01Z", "media_type": "image"},
                {"action": "add", "rel_path": "upd.jpg", "file_size": 10, "file_mtime": "2025-01-01T12:00:02Z", "media_type": "image"},
                {"action": "add", "rel_path": "miss.jpg", "file_size": 20, "file_mtime": "2025-01-01T12:00:03Z", "media_type": "image"},
            ],
        },
        headers=auth,
    )

    r_page = client.get("/v1/assets/page", params={"library_id": library_id}, headers=auth)
    assert r_page.status_code == 200
    page = r_page.json()["items"]
    by_path = {a["rel_path"]: a for a in page}

    r_batch = client.post(
        f"/v1/scans/{scan_id}/batch",
        json={
            "items": [
                {"action": "skip", "asset_id": by_path["skip1.jpg"]["asset_id"]},
                {"action": "skip", "asset_id": by_path["skip2.jpg"]["asset_id"]},
                {"action": "update", "asset_id": by_path["upd.jpg"]["asset_id"], "file_size": 100, "file_mtime": "2025-01-02T12:00:00Z"},
                {"action": "missing", "asset_id": by_path["miss.jpg"]["asset_id"]},
                {"action": "add", "rel_path": "new.jpg", "file_size": 30, "file_mtime": "2025-01-01T12:00:04Z", "media_type": "image"},
            ],
        },
        headers=auth,
    )
    assert r_batch.status_code == 200
    data = r_batch.json()
    assert data["added"] == 1
    assert data["updated"] == 1
    assert data["skipped"] == 2
    assert data["missing"] == 1

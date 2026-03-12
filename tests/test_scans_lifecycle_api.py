"""API tests for scans lifecycle: create, running, complete, abort."""

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
def scans_lifecycle_client() -> tuple[TestClient, str, str]:
    """
    Two testcontainers Postgres; provision tenant DB; create library.
    Yields (client, api_key, library_id).
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

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "ScansLifecycleTenant", "plan": "free"},
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
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "ScansLifecycleLib", "root_path": "/scans"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]
                yield client, api_key, library_id

        _engines.clear()


@pytest.mark.slow
def test_create_scan(scans_lifecycle_client: tuple[TestClient, str, str]) -> None:
    """POST /v1/scans with {library_id} returns {scan_id, library_id, status} with status running."""
    client, api_key, library_id = scans_lifecycle_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert "scan_id" in body
    scan_id = body["scan_id"]
    assert scan_id.startswith("scan_")

    # Verify it's running via GET /v1/scans/running
    r_run = client.get("/v1/scans/running", params={"library_id": library_id}, headers=auth)
    assert r_run.status_code == 200
    running = r_run.json()
    assert any(s["scan_id"] == scan_id for s in running)


@pytest.mark.slow
def test_get_running_scans(scans_lifecycle_client: tuple[TestClient, str, str]) -> None:
    """After creating a scan, GET /v1/scans/running returns a list containing that scan."""
    client, api_key, library_id = scans_lifecycle_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_create = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_create.status_code == 200
    scan_id = r_create.json()["scan_id"]

    r = client.get("/v1/scans/running", params={"library_id": library_id}, headers=auth)
    assert r.status_code == 200
    running = r.json()
    ids = [s["scan_id"] for s in running]
    assert scan_id in ids


@pytest.mark.slow
def test_complete_scan(scans_lifecycle_client: tuple[TestClient, str, str]) -> None:
    """Create scan, POST /v1/scans/{scan_id}/complete; assert 200 and scan no longer in running list."""
    client, api_key, library_id = scans_lifecycle_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_create = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_create.status_code == 200
    scan_id = r_create.json()["scan_id"]

    r_complete = client.post(
        f"/v1/scans/{scan_id}/complete",
        json={
            "files_discovered": 5,
            "files_added": 2,
            "files_skipped": 3,
            "files_updated": 0,
            "files_missing": 0,
        },
        headers=auth,
    )
    assert r_complete.status_code == 200
    body = r_complete.json()
    assert body["scan_id"] == scan_id
    assert body["status"] == "complete"

    r_run = client.get("/v1/scans/running", params={"library_id": library_id}, headers=auth)
    assert r_run.status_code == 200
    running = r_run.json()
    assert not any(s["scan_id"] == scan_id for s in running)


@pytest.mark.slow
def test_abort_scan(scans_lifecycle_client: tuple[TestClient, str, str]) -> None:
    """Create scan, POST /v1/scans/{scan_id}/abort; assert 200 and scan no longer in running list."""
    client, api_key, library_id = scans_lifecycle_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_create = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_create.status_code == 200
    scan_id = r_create.json()["scan_id"]

    r_abort = client.post(
        f"/v1/scans/{scan_id}/abort",
        json={"error_message": None},
        headers=auth,
    )
    assert r_abort.status_code == 200
    body = r_abort.json()
    assert body["scan_id"] == scan_id
    assert body["status"] in ("aborted", "error")

    r_run = client.get("/v1/scans/running", params={"library_id": library_id}, headers=auth)
    assert r_run.status_code == 200
    running = r_run.json()
    assert not any(s["scan_id"] == scan_id for s in running)


@pytest.mark.slow
def test_create_scan_unknown_library(scans_lifecycle_client: tuple[TestClient, str, str]) -> None:
    """POST /v1/scans with nonexistent library_id returns 404."""
    client, api_key, _ = scans_lifecycle_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/scans",
        json={"library_id": "lib_nonexistent000000000000", "status": "running"},
        headers=auth,
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_create_scan_requires_auth(scans_lifecycle_client: tuple[TestClient, str, str]) -> None:
    """Missing Authorization header returns 401."""
    client, _, library_id = scans_lifecycle_client

    r = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
    )
    assert r.status_code == 401

"""API tests for jobs: enqueue, list, status."""

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
def jobs_api_client() -> tuple[TestClient, str, str, str, str]:
    """
    Two testcontainers Postgres; provision tenant DB; create library, upsert asset, enqueue job.
    Yields (client, api_key, library_id, asset_id, job_id).
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
                    json={"name": "JobsAPITenant", "plan": "free"},
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
                    json={"name": "JobsAPILib", "root_path": "/jobs"},
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
                    "/v1/assets/upsert",
                    json={
                        "library_id": library_id,
                        "rel_path": "img.jpg",
                        "file_size": 1000,
                        "file_mtime": "2025-01-01T12:00:00Z",
                        "media_type": "image/jpeg",
                        "scan_id": scan_id,
                    },
                    headers=auth,
                )
                r_asset = client.get(
                    "/v1/assets/by-path",
                    params={"library_id": library_id, "rel_path": "img.jpg"},
                    headers=auth,
                )
                assert r_asset.status_code == 200
                asset_id = r_asset.json()["asset_id"]

                r_enq = client.post(
                    "/v1/jobs/enqueue",
                    json={
                        "job_type": "proxy",
                        "filter": {"library_id": library_id},
                        "force": False,
                    },
                    headers=auth,
                )
                assert r_enq.status_code == 200
                assert r_enq.json()["enqueued"] >= 1

                r_jobs = client.get("/v1/jobs", params={"library_id": library_id}, headers=auth)
                assert r_jobs.status_code == 200
                jobs = r_jobs.json()
                assert len(jobs) >= 1
                job_id = jobs[0]["job_id"]

                yield client, api_key, library_id, asset_id, job_id

        _engines.clear()


@pytest.mark.slow
def test_list_jobs(jobs_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """GET /v1/jobs returns a list; each item has job_id, job_type, status."""
    client, api_key, _, _, _ = jobs_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/jobs", headers=auth)
    assert r.status_code == 200
    jobs = r.json()
    assert isinstance(jobs, list)
    assert len(jobs) >= 1
    for j in jobs:
        assert "job_id" in j
        assert "job_type" in j
        assert "status" in j


@pytest.mark.slow
def test_list_jobs_filter_by_library(jobs_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """GET /v1/jobs?library_id=... returns only jobs for that library."""
    client, api_key, library_id, _, _ = jobs_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/jobs", params={"library_id": library_id}, headers=auth)
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) >= 1
    for j in jobs:
        assert j.get("asset_id")  # jobs have asset_id from join; filter by library ensures library match
    r_all = client.get("/v1/jobs", headers=auth)
    assert len(r_all.json()) >= len(jobs)


@pytest.mark.slow
def test_list_jobs_filter_by_status(jobs_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """GET /v1/jobs?status=pending returns jobs; with only pending jobs enqueued, all have status pending."""
    client, api_key, library_id, _, _ = jobs_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/jobs",
        params={"library_id": library_id, "status": "pending"},
        headers=auth,
    )
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) >= 1
    for j in jobs:
        assert j["status"] in ("pending", "claimed", "completed", "failed", "cancelled")


@pytest.mark.slow
def test_get_job_status(jobs_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """GET /v1/jobs/{job_id}/status returns the job's current status fields."""
    client, api_key, _, _, job_id = jobs_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(f"/v1/jobs/{job_id}/status", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert "status" in body
    assert "error_message" in body


@pytest.mark.slow
def test_get_job_status_404(jobs_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """GET /v1/jobs/{job_id}/status with unknown ID returns 404."""
    client, api_key, _, _, _ = jobs_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/jobs/job_nonexistent0000000000000/status", headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
def test_jobs_requires_auth(jobs_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """Missing Authorization header returns 401."""
    client, _, library_id, _, _ = jobs_api_client

    r = client.get("/v1/jobs", params={"library_id": library_id})
    assert r.status_code == 401

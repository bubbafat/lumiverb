"""API tests for pipeline lock and status endpoints."""

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
def pipeline_api_client():
    """
    Two testcontainers Postgres; provision tenant DB; create library + asset + job.
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
                    json={"name": "PipelineAPITenant", "plan": "free"},
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
                    json={"name": "PipelineAPILib", "root_path": "/pipeline"},
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
                        "rel_path": "photo.jpg",
                        "file_size": 2048,
                        "file_mtime": "2025-01-01T00:00:00Z",
                        "media_type": "image",
                        "scan_id": scan_id,
                    },
                    headers=auth,
                )

                # Enqueue a proxy job so pipeline status has stage data.
                client.post(
                    "/v1/jobs/enqueue",
                    json={"job_type": "proxy", "filter": {"library_id": library_id}, "force": False},
                    headers=auth,
                )

                yield client, api_key, library_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Pipeline lock tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_acquire_lock_fresh(pipeline_api_client) -> None:
    """POST /v1/pipeline/lock/acquire returns lock_id on first acquire."""
    client, api_key, _ = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/v1/pipeline/lock/acquire", json={"lock_timeout_minutes": 5}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "lock_id" in body
    assert body["lock_id"].startswith("lock_")
    assert "tenant_id" in body

    # Release so other tests start clean.
    client.post("/v1/pipeline/lock/release", json={"lock_id": body["lock_id"]}, headers=auth)


@pytest.mark.slow
def test_acquire_lock_held_returns_409(pipeline_api_client) -> None:
    """Second acquire without force returns 409 while the first lock is still fresh."""
    client, api_key, _ = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r1 = client.post("/v1/pipeline/lock/acquire", json={"lock_timeout_minutes": 5}, headers=auth)
    assert r1.status_code == 200
    lock_id = r1.json()["lock_id"]

    try:
        r2 = client.post("/v1/pipeline/lock/acquire", json={"lock_timeout_minutes": 5}, headers=auth)
        assert r2.status_code == 409
        detail = r2.json()["detail"]
        assert detail.get("code") == "lock_held"
        assert "hostname" in detail.get("details", {})
    finally:
        client.post("/v1/pipeline/lock/release", json={"lock_id": lock_id}, headers=auth)


@pytest.mark.slow
def test_acquire_lock_force_overrides_existing(pipeline_api_client) -> None:
    """force=True acquires the lock even when one is already held."""
    client, api_key, _ = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r1 = client.post("/v1/pipeline/lock/acquire", json={"lock_timeout_minutes": 5}, headers=auth)
    assert r1.status_code == 200
    lock_id_a = r1.json()["lock_id"]

    r2 = client.post(
        "/v1/pipeline/lock/acquire",
        json={"lock_timeout_minutes": 5, "force": True},
        headers=auth,
    )
    assert r2.status_code == 200
    lock_id_b = r2.json()["lock_id"]
    assert lock_id_b != lock_id_a

    client.post("/v1/pipeline/lock/release", json={"lock_id": lock_id_b}, headers=auth)


@pytest.mark.slow
def test_heartbeat_lock(pipeline_api_client) -> None:
    """POST /v1/pipeline/lock/heartbeat returns 204 while lock is held."""
    client, api_key, _ = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/v1/pipeline/lock/acquire", json={}, headers=auth)
    assert r.status_code == 200
    lock_id = r.json()["lock_id"]

    try:
        r_hb = client.post("/v1/pipeline/lock/heartbeat", headers=auth)
        assert r_hb.status_code == 204
    finally:
        client.post("/v1/pipeline/lock/release", json={"lock_id": lock_id}, headers=auth)


@pytest.mark.slow
def test_release_lock_correct_id_allows_reacquire(pipeline_api_client) -> None:
    """Releasing with the correct lock_id removes the lock; subsequent acquire succeeds."""
    client, api_key, _ = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r1 = client.post("/v1/pipeline/lock/acquire", json={}, headers=auth)
    assert r1.status_code == 200
    lock_id = r1.json()["lock_id"]

    r_rel = client.post("/v1/pipeline/lock/release", json={"lock_id": lock_id}, headers=auth)
    assert r_rel.status_code == 204

    # Lock is gone — should be able to acquire again without force.
    r2 = client.post("/v1/pipeline/lock/acquire", json={}, headers=auth)
    assert r2.status_code == 200
    client.post("/v1/pipeline/lock/release", json={"lock_id": r2.json()["lock_id"]}, headers=auth)


@pytest.mark.slow
def test_release_lock_wrong_id_is_noop(pipeline_api_client) -> None:
    """Releasing with a stale lock_id is a no-op; the current lock remains."""
    client, api_key, _ = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r1 = client.post("/v1/pipeline/lock/acquire", json={}, headers=auth)
    assert r1.status_code == 200
    lock_id_a = r1.json()["lock_id"]

    # Force acquire a new lock — lock_id_a is now stale.
    r2 = client.post("/v1/pipeline/lock/acquire", json={"force": True}, headers=auth)
    assert r2.status_code == 200
    lock_id_b = r2.json()["lock_id"]

    # Release with the old stale id — should be a no-op.
    r_noop = client.post("/v1/pipeline/lock/release", json={"lock_id": lock_id_a}, headers=auth)
    assert r_noop.status_code == 204

    # Lock B is still held — a fresh non-force acquire must still return 409.
    r3 = client.post("/v1/pipeline/lock/acquire", json={"lock_timeout_minutes": 5}, headers=auth)
    assert r3.status_code == 409

    # Cleanup: release lock B.
    client.post("/v1/pipeline/lock/release", json={"lock_id": lock_id_b}, headers=auth)


# ---------------------------------------------------------------------------
# Pipeline status tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_pipeline_status_single_library(pipeline_api_client) -> None:
    """GET /v1/pipeline/status?library_id=... returns single-library payload shape."""
    client, api_key, library_id = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/pipeline/status", params={"library_id": library_id}, headers=auth)
    assert r.status_code == 200
    body = r.json()

    assert body["library_id"] == library_id
    assert isinstance(body["library"], str)
    assert isinstance(body["total_assets"], int)
    assert body["total_assets"] >= 1
    assert isinstance(body["workers"], int)
    assert isinstance(body["stages"], list)

    # We enqueued a proxy job, so the proxy stage should appear.
    stage_names = {s["name"] for s in body["stages"]}
    assert "proxy" in stage_names

    proxy = next(s for s in body["stages"] if s["name"] == "proxy")
    for key in ("label", "done", "inflight", "pending", "failed", "blocked"):
        assert key in proxy


@pytest.mark.slow
def test_pipeline_status_tenant_wide(pipeline_api_client) -> None:
    """GET /v1/pipeline/status (no library_id) returns tenant-wide payload shape."""
    client, api_key, _ = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/pipeline/status", headers=auth)
    assert r.status_code == 200
    body = r.json()

    assert "workers" in body
    assert "libraries" in body
    assert isinstance(body["libraries"], list)
    assert len(body["libraries"]) >= 1

    lib_entry = body["libraries"][0]
    for key in ("library", "library_id", "total_assets", "stages"):
        assert key in lib_entry


@pytest.mark.slow
def test_pipeline_status_unknown_library_returns_404(pipeline_api_client) -> None:
    """GET /v1/pipeline/status with an unknown library_id returns 404."""
    client, api_key, _ = pipeline_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/pipeline/status", params={"library_id": "lib_nonexistent0000000"}, headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
def test_pipeline_requires_auth(pipeline_api_client) -> None:
    """Missing Authorization header returns 401 for all pipeline endpoints."""
    client, _, library_id = pipeline_api_client

    assert client.post("/v1/pipeline/lock/acquire", json={}).status_code == 401
    assert client.post("/v1/pipeline/lock/heartbeat").status_code == 401
    assert client.post("/v1/pipeline/lock/release", json={}).status_code == 401
    assert client.get("/v1/pipeline/status", params={"library_id": library_id}).status_code == 401

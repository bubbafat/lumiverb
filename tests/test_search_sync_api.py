"""API tests for search-sync and jobs/failures endpoints."""

import os
from unittest.mock import MagicMock, patch

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
def search_sync_api_client():
    """
    Two testcontainers Postgres; provision tenant DB; create library + asset.
    Yields (client, api_key, library_id, asset_id).
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
                    json={"name": "SearchSyncAPITenant", "plan": "free"},
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
                    json={"name": "SearchSyncAPILib", "root_path": "/sync"},
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
                        "file_size": 1024,
                        "file_mtime": "2025-06-01T00:00:00Z",
                        "media_type": "image/jpeg",
                        "scan_id": scan_id,
                    },
                    headers=auth,
                )
                r_asset = client.get(
                    "/v1/assets/by-path",
                    params={"library_id": library_id, "rel_path": "photo.jpg"},
                    headers=auth,
                )
                assert r_asset.status_code == 200
                asset_id = r_asset.json()["asset_id"]

                yield client, api_key, library_id, asset_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Search sync: pending count
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_sync_pending_empty_initially(search_sync_api_client) -> None:
    """GET /v1/search-sync/pending returns 0 before any resync is triggered."""
    client, api_key, library_id, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/search-sync/pending", params={"library_id": library_id}, headers=auth)
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ---------------------------------------------------------------------------
# Search sync: resync
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_sync_resync_enqueues_assets(search_sync_api_client) -> None:
    """POST /v1/search-sync/resync enqueues all active assets for the library."""
    client, api_key, library_id, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post("/v1/search-sync/resync", json={"library_id": library_id}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["enqueued"] >= 1


@pytest.mark.slow
def test_search_sync_pending_after_resync(search_sync_api_client) -> None:
    """Pending count > 0 after resync has been called."""
    client, api_key, library_id, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Ensure something is in the queue (resync may have been called already).
    client.post("/v1/search-sync/resync", json={"library_id": library_id}, headers=auth)

    r = client.get("/v1/search-sync/pending", params={"library_id": library_id}, headers=auth)
    assert r.status_code == 200
    assert r.json()["count"] >= 1


@pytest.mark.slow
def test_search_sync_resync_unknown_library_returns_404(search_sync_api_client) -> None:
    """POST /v1/search-sync/resync with unknown library_id returns 404."""
    client, api_key, _, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/search-sync/resync",
        json={"library_id": "lib_nonexistent0000000"},
        headers=auth,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Search sync: process-batch
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_sync_process_batch_when_queue_empty_returns_not_processed(
    search_sync_api_client,
) -> None:
    """POST /v1/search-sync/process-batch returns processed=false when queue is empty."""
    client, api_key, library_id, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Drain anything left in the queue first (mock Quickwit so ingest doesn't hit network).
    mock_qw = MagicMock()
    mock_qw.enabled = True
    with patch("src.workers.search_sync.QuickwitClient", return_value=mock_qw):
        while True:
            r = client.post(
                "/v1/search-sync/process-batch",
                json={"library_id": library_id},
                headers=auth,
            )
            assert r.status_code == 200
            if not r.json()["processed"]:
                break

    # Queue is now empty — next call should return processed=false.
    with patch("src.workers.search_sync.QuickwitClient", return_value=mock_qw):
        r = client.post(
            "/v1/search-sync/process-batch",
            json={"library_id": library_id},
            headers=auth,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] is False
    assert body["synced"] == 0
    assert body["skipped"] == 0


@pytest.mark.slow
def test_search_sync_process_batch_claims_and_marks_synced(search_sync_api_client) -> None:
    """
    process-batch claims pending rows, attempts ingest (mocked Quickwit), marks them synced.

    Assets without AI metadata are skipped (not synced); either way the rows
    are consumed and processed=true is returned.
    """
    client, api_key, library_id, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Enqueue work.
    r_resync = client.post("/v1/search-sync/resync", json={"library_id": library_id}, headers=auth)
    assert r_resync.status_code == 200
    assert r_resync.json()["enqueued"] >= 1

    mock_qw = MagicMock()
    mock_qw.enabled = True

    with patch("src.workers.search_sync.QuickwitClient", return_value=mock_qw):
        r = client.post(
            "/v1/search-sync/process-batch",
            json={"library_id": library_id},
            headers=auth,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] is True
    # synced + skipped equals the number of unique assets processed in this batch.
    assert body["synced"] + body["skipped"] >= 1
    # Quickwit index methods were called (even if ingest was skipped for assets with no metadata).
    mock_qw.ensure_index_for_library.assert_called_once_with(library_id)
    mock_qw.ensure_scene_index_for_library.assert_called_once_with(library_id)


@pytest.mark.slow
def test_search_sync_process_batch_unknown_library_returns_404(search_sync_api_client) -> None:
    """POST /v1/search-sync/process-batch with unknown library_id returns 404."""
    client, api_key, _, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/search-sync/process-batch",
        json={"library_id": "lib_nonexistent0000000"},
        headers=auth,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Jobs failures endpoint
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_failures_empty_when_no_failed_jobs(search_sync_api_client) -> None:
    """GET /v1/jobs/failures returns total_count=0 when no jobs have failed."""
    client, api_key, library_id, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Enqueue a proxy job but don't fail it.
    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library_id}, "force": False},
        headers=auth,
    )

    r = client.get(
        "/v1/jobs/failures",
        params={"library_id": library_id, "job_type": "proxy"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 0
    assert body["rows"] == []


@pytest.mark.slow
def test_failures_lists_failed_jobs(search_sync_api_client) -> None:
    """GET /v1/jobs/failures returns the failed job with correct shape after a job is failed."""
    client, api_key, library_id, _ = search_sync_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Ensure a proxy job exists and claim it.
    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library_id}, "force": True},
        headers=auth,
    )
    r_next = client.get(
        "/v1/jobs/next",
        params={"job_type": "proxy", "library_id": library_id},
        headers=auth,
    )
    assert r_next.status_code == 200
    job_id = r_next.json()["job_id"]

    # Fail the job.
    r_fail = client.post(
        f"/v1/jobs/{job_id}/fail",
        json={"error_message": "test failure"},
        headers=auth,
    )
    assert r_fail.status_code == 200

    # Now failures endpoint should return it.
    r = client.get(
        "/v1/jobs/failures",
        params={"library_id": library_id, "job_type": "proxy"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] >= 1
    assert len(body["rows"]) >= 1

    row = body["rows"][0]
    assert "rel_path" in row
    assert "error_message" in row
    assert row["error_message"] != ""


# ---------------------------------------------------------------------------
# Auth checks
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_sync_requires_auth(search_sync_api_client) -> None:
    """Missing Authorization header returns 401 for all search-sync endpoints."""
    client, _, library_id, _ = search_sync_api_client

    assert client.get("/v1/search-sync/pending", params={"library_id": library_id}).status_code == 401
    assert client.post("/v1/search-sync/resync", json={"library_id": library_id}).status_code == 401
    assert client.post("/v1/search-sync/process-batch", json={"library_id": library_id}).status_code == 401


@pytest.mark.slow
def test_failures_requires_auth(search_sync_api_client) -> None:
    """Missing Authorization header returns 401 for GET /v1/jobs/failures."""
    client, _, library_id, _ = search_sync_api_client

    assert (
        client.get("/v1/jobs/failures", params={"library_id": library_id, "job_type": "proxy"}).status_code
        == 401
    )

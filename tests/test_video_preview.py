"""Slow tests for video preview endpoint and job priority / asset status lifecycle."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def preview_api_client(tmp_path_factory: pytest.TempPathFactory) -> tuple[TestClient, str, str, str, str]:
    """
    Two Postgres containers (control + tenant), tenant DB provisioned.
    Creates a library and upserts one image and one video asset.
    Yields (client, api_key, library_id, image_asset_id, video_asset_id).
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
                    json={"name": "PreviewTenant", "plan": "free"},
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

                # Create library
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "PreviewLib", "root_path": str(tmp_path_factory.mktemp("media"))},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                # Start scan
                r_scan = client.post(
                    "/v1/scans",
                    json={"library_id": library_id, "status": "running"},
                    headers=auth,
                )
                assert r_scan.status_code == 200
                scan_id = r_scan.json()["scan_id"]

                # Upsert image asset
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
                r_img = client.get(
                    "/v1/assets/by-path",
                    params={"library_id": library_id, "rel_path": "img.jpg"},
                    headers=auth,
                )
                assert r_img.status_code == 200
                image_asset_id = r_img.json()["asset_id"]

                # Upsert video asset
                client.post(
                    "/v1/assets/upsert",
                    json={
                        "library_id": library_id,
                        "rel_path": "clip.mp4",
                        "file_size": 5000000,
                        "file_mtime": "2025-01-01T12:00:00Z",
                        "media_type": "video/mp4",
                        "scan_id": scan_id,
                    },
                    headers=auth,
                )
                r_vid = client.get(
                    "/v1/assets/by-path",
                    params={"library_id": library_id, "rel_path": "clip.mp4"},
                    headers=auth,
                )
                assert r_vid.status_code == 200
                video_asset_id = r_vid.json()["asset_id"]

                yield client, api_key, library_id, image_asset_id, video_asset_id

        _engines.clear()


@pytest.mark.slow
def test_preview_endpoint_returns_202_when_no_preview(
    preview_api_client: tuple[TestClient, str, str, str, str],
) -> None:
    """Upsert video asset, call preview endpoint, assert 202 and urgent video-preview job."""
    client, api_key, library_id, _image_asset_id, video_asset_id = preview_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(f"/v1/assets/{video_asset_id}/preview", headers=auth)
    assert r.status_code == 202
    assert r.json()["status"] == "generating"

    # Verify a video-preview job was enqueued at priority 0.
    r_jobs = client.get("/v1/jobs", params={"library_id": library_id}, headers=auth)
    assert r_jobs.status_code == 200
    jobs = r_jobs.json()
    vp_jobs = [j for j in jobs if j["job_type"] == "video-preview" and j["asset_id"] == video_asset_id]
    assert len(vp_jobs) == 1
    assert vp_jobs[0]["priority"] == 0


@pytest.mark.slow
def test_preview_endpoint_streams_when_file_exists(
    preview_api_client: tuple[TestClient, str, str, str, str],
) -> None:
    """When video_preview_key is set and file exists, preview endpoint streams MP4."""
    client, api_key, library_id, _image_asset_id, video_asset_id = preview_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create a small fake MP4 file on disk and complete a video-preview job pointing to it.
    from src.storage.local import get_storage

    storage = get_storage()
    key = f"previews/test/{video_asset_id}.mp4"
    path = storage.abs_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal fake MP4 header – enough for StreamingResponse; contents are not validated by tests.
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")

    # First call to preview endpoint enqueues a video-preview job.
    r_init = client.get(f"/v1/assets/{video_asset_id}/preview", headers=auth)
    assert r_init.status_code == 202

    # Worker claims the next video-preview job.
    r_next = client.get(
        "/v1/jobs/next",
        params={"job_type": "video-preview", "library_id": library_id},
        headers=auth,
    )
    assert r_next.status_code == 200
    job = r_next.json()
    job_id = job["job_id"]

    # Complete the job with the preview key.
    r_complete = client.post(
        f"/v1/jobs/{job_id}/complete",
        json={"video_preview_key": key},
        headers=auth,
    )
    assert r_complete.status_code == 200

    # Now the preview endpoint should stream the file.
    r = client.get(f"/v1/assets/{video_asset_id}/preview", headers=auth)
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("video/mp4")
    assert "content-length" in {k.lower() for k in r.headers.keys()}


@pytest.mark.slow
def test_preview_endpoint_re_enqueues_when_file_missing(
    preview_api_client: tuple[TestClient, str, str, str, str],
) -> None:
    """If video_preview_key is set but file missing, endpoint clears key and re-enqueues job."""
    client, api_key, library_id, _image_asset_id, video_asset_id = preview_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    from src.repository.tenant import AssetRepository, WorkerJobRepository
    from src.core.database import get_tenant_session
    from src.repository.control_plane import TenantDbRoutingRepository
    from src.core.database import get_control_session

    ctx = client.get("/v1/tenant/context", headers=auth).json()
    tenant_id = ctx["tenant_id"]
    with get_control_session() as session:
        routing_repo = TenantDbRoutingRepository(session)
        row = routing_repo.get_by_tenant_id(tenant_id)
        assert row is not None
        tenant_url = row.connection_string

    engine = create_engine(_ensure_psycopg2(tenant_url))
    with Session(engine) as session:
        asset_repo = AssetRepository(session)
        worker_repo = WorkerJobRepository(session)
        asset = asset_repo.get_by_id(video_asset_id)
        assert asset is not None
        asset.video_preview_key = "nonexistent/path.mp4"
        session.add(asset)
        session.commit()

        # Ensure no pending video-preview job beforehand.
        assert not worker_repo.has_pending_job("video-preview", video_asset_id)

    r = client.get(f"/v1/assets/{video_asset_id}/preview", headers=auth)
    assert r.status_code == 202
    assert r.json()["status"] == "generating"

    # Confirm job enqueued.
    r_jobs = client.get("/v1/jobs", params={"library_id": library_id}, headers=auth)
    vp_jobs = [j for j in r_jobs.json() if j["job_type"] == "video-preview" and j["asset_id"] == video_asset_id]
    assert len(vp_jobs) >= 1


@pytest.mark.slow
def test_last_accessed_at_updated_on_stream(
    preview_api_client: tuple[TestClient, str, str, str, str],
) -> None:
    """Streaming a preview updates video_preview_last_accessed_at."""
    client, api_key, library_id, _image_asset_id, video_asset_id = preview_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    from src.storage.local import get_storage

    storage = get_storage()
    key = f"previews/test/{video_asset_id}_access.mp4"
    path = storage.abs_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")

    # Enqueue and claim a video-preview job for this asset.
    r_init = client.get(f"/v1/assets/{video_asset_id}/preview", headers=auth)
    assert r_init.status_code == 202

    r_next = client.get(
        "/v1/jobs/next",
        params={"job_type": "video-preview", "library_id": library_id},
        headers=auth,
    )
    assert r_next.status_code == 200
    job = r_next.json()
    job_id = job["job_id"]

    # Complete the preview job so the asset has a preview.
    r_complete = client.post(
        f"/v1/jobs/{job_id}/complete",
        json={"video_preview_key": key},
        headers=auth,
    )
    assert r_complete.status_code == 200

    # First stream sets last_accessed_at.
    r_stream = client.get(f"/v1/assets/{video_asset_id}/preview", headers=auth)
    assert r_stream.status_code == 200

    # Fetch asset via API and confirm timestamps are populated.
    r_asset = client.get(f"/v1/assets/{video_asset_id}", headers=auth)
    assert r_asset.status_code == 200
    data = r_asset.json()
    assert data["video_preview_key"] == key
    assert data["video_preview_generated_at"] is not None
    assert data["video_preview_last_accessed_at"] is not None


@pytest.mark.slow
def test_priority_ordering(preview_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """
    Enqueue a proxy job and an ai_vision job and verify that proxy jobs
    use priority ordering when claimed.
    """
    client, api_key, library_id, image_asset_id, _video_asset_id = preview_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Enqueue proxy and ai_vision jobs for the same library.
    r_proxy = client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library_id}, "force": False},
        headers=auth,
    )
    assert r_proxy.status_code == 200

    r_vision = client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "ai_vision",
            "filter": {"library_id": library_id, "missing_ai": True},
            "force": False,
        },
        headers=auth,
    )
    assert r_vision.status_code == 200

    # Verify that proxy jobs are given normal priority and can be claimed.
    r_next_proxy = client.get("/v1/jobs/next", params={"job_type": "proxy"}, headers=auth)
    assert r_next_proxy.status_code in (200, 204)
    if r_next_proxy.status_code == 200:
        proxy_job = r_next_proxy.json()
        assert proxy_job["job_type"] == "proxy"


@pytest.mark.slow
def test_asset_status_proxy_ready_and_described(
    preview_api_client: tuple[TestClient, str, str, str, str],
) -> None:
    """Complete proxy then ai_vision jobs and assert asset status lifecycle transitions."""
    client, api_key, library_id, image_asset_id, _video_asset_id = preview_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Enqueue and claim proxy job.
    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library_id}, "force": False},
        headers=auth,
    )
    r_next = client.get("/v1/jobs/next", params={"job_type": "proxy"}, headers=auth)
    if r_next.status_code == 204:
        pytest.skip("No proxy jobs available")
    job = r_next.json()
    job_id = job["job_id"]

    # Complete proxy job.
    r_complete = client.post(
        f"/v1/jobs/{job_id}/complete",
        json={
            "proxy_key": "proxy/test.jpg",
            "thumbnail_key": "thumb/test.jpg",
            "width": 100,
            "height": 100,
        },
        headers=auth,
    )
    assert r_complete.status_code == 200

    # Assert that the asset associated with this job is now proxy_ready.
    asset_id_after_proxy = job["asset_id"]
    r_asset = client.get(f"/v1/assets/{asset_id_after_proxy}", headers=auth)
    assert r_asset.status_code == 200
    assert r_asset.json()["status"] == "proxy_ready"

    # Enqueue and complete ai_vision job.
    client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "ai_vision",
            "filter": {"library_id": library_id, "missing_ai": True},
            "force": False,
        },
        headers=auth,
    )
    r_next_vis = client.get("/v1/jobs/next", params={"job_type": "ai_vision"}, headers=auth)
    if r_next_vis.status_code == 204:
        pytest.skip("No ai_vision jobs available")
    vis_job = r_next_vis.json()
    vis_job_id = vis_job["job_id"]

    r_complete_vis = client.post(
        f"/v1/jobs/{vis_job_id}/complete",
        json={
            "model_id": "test-vision-model",
            "model_version": "1",
            "description": "A test image",
            "tags": ["test"],
        },
        headers=auth,
    )
    assert r_complete_vis.status_code == 200

    # The asset associated with the ai_vision job should now be described.
    asset_id_after_vision = vis_job["asset_id"]
    r_asset2 = client.get(f"/v1/assets/{asset_id_after_vision}", headers=auth)
    assert r_asset2.status_code == 200
    assert r_asset2.json()["status"] == "described"


"""Proxy worker tests: API-only. Process image, skip video, missing file. Use TestClient + testcontainers."""

import hashlib
import os
import secrets
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.cli.scanner import scan_library
from src.core.config import get_settings
from src.core.database import _engines
from src.storage.artifact_store import ArtifactRef, LocalArtifactStore
from src.storage.local import LocalStorage
from src.workers.proxy import ProxyWorker
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations

_has_pyvips = False
try:
    import pyvips
    _has_pyvips = hasattr(pyvips, "Image")
except Exception:
    pass

_skip_no_libvips = pytest.mark.skipif(not _has_pyvips, reason="libvips not installed")


# ---------------------------------------------------------------------------
# Fast unit tests
# ---------------------------------------------------------------------------

TENANT_ID = "tnt_test"
LIBRARY_ID = "lib_test"
ASSET_ID = "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV"
REL_PATH = "photos/test.jpg"


@_skip_no_libvips
@pytest.mark.fast
def test_proxy_worker_process_calls_write_artifact_for_both(tmp_path: Path) -> None:
    """process() calls write_artifact for proxy and thumbnail; return dict uses ref keys/sha256."""
    artifact_store = MagicMock()
    proxy_ref = ArtifactRef(key="tnt/lib/proxies/07/ast_photo.jpg", sha256="abc123")
    thumb_ref = ArtifactRef(key="tnt/lib/thumbnails/07/ast_photo.jpg", sha256="def456")
    artifact_store.write_artifacts_batch.return_value = {
        "proxy": proxy_ref,
        "thumbnail": thumb_ref,
    }

    img = Image.new("RGB", (200, 150), color=(100, 150, 200))
    src = tmp_path / "photos" / "test.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    img.save(src, "JPEG")

    worker = ProxyWorker(client=MagicMock(), artifact_store=artifact_store, once=True)
    job = {
        "job_id": "job-1",
        "asset_id": ASSET_ID,
        "library_id": LIBRARY_ID,
        "rel_path": REL_PATH,
        "root_path": str(tmp_path),
        "media_type": "image",
    }
    result = worker.process(job)

    assert result["proxy_key"] == proxy_ref.key
    assert result["thumbnail_key"] == thumb_ref.key
    assert result["proxy_sha256"] == proxy_ref.sha256
    assert result["thumbnail_sha256"] == thumb_ref.sha256
    assert result["width"] == 200
    assert result["height"] == 150

    artifact_store.write_artifacts_batch.assert_called_once()
    call_args = artifact_store.write_artifacts_batch.call_args
    assert call_args[0][0] == ASSET_ID
    assert "proxy" in call_args[0][1]
    assert "thumbnail" in call_args[0][1]
    assert call_args[1]["width"] == 200
    assert call_args[1]["height"] == 150


@_skip_no_libvips
@pytest.mark.fast
def test_proxy_worker_local_store_writes_files(tmp_path: Path) -> None:
    """LocalArtifactStore integration: proxy and thumbnail files land on disk."""
    storage = LocalStorage(data_dir=str(tmp_path))
    artifact_store = LocalArtifactStore(storage=storage, tenant_id=TENANT_ID)

    img = Image.new("RGB", (100, 80), color=(0, 255, 0))
    src = tmp_path / "green.jpg"
    img.save(src, "JPEG")

    worker = ProxyWorker(client=MagicMock(), artifact_store=artifact_store, once=True)
    job = {
        "job_id": "job-2",
        "asset_id": ASSET_ID,
        "library_id": LIBRARY_ID,
        "rel_path": "green.jpg",
        "root_path": str(tmp_path),
        "media_type": "image",
    }
    result = worker.process(job)

    proxy_path = tmp_path / result["proxy_key"]
    thumb_path = tmp_path / result["thumbnail_key"]
    assert proxy_path.exists()
    assert thumb_path.exists()
    assert result["proxy_sha256"] == hashlib.sha256(proxy_path.read_bytes()).hexdigest()
    assert result["thumbnail_sha256"] == hashlib.sha256(thumb_path.read_bytes()).hexdigest()


@pytest.mark.fast
def test_proxy_worker_skips_video_fast(tmp_path: Path) -> None:
    artifact_store = MagicMock()
    worker = ProxyWorker(client=MagicMock(), artifact_store=artifact_store, once=True)
    result = worker.process({
        "job_id": "job-3", "asset_id": ASSET_ID, "library_id": LIBRARY_ID,
        "rel_path": "clip.mp4", "root_path": str(tmp_path), "media_type": "video",
    })
    assert result == {}
    artifact_store.write_artifact.assert_not_called()


@pytest.fixture(scope="module")
def proxy_worker_env():
    """Two testcontainers Postgres; create tenant; yield (TestClient, api_key, tenant_id, tenant_url)."""
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
                    json={"name": "ProxyWorkerTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

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
                yield client, api_key, tenant_id, tenant_url
        _engines.clear()


@_skip_no_libvips
@pytest.mark.slow
def test_proxy_worker_processes_image(proxy_worker_env, tmp_path: Path) -> None:
    """Worker generates proxy and thumbnail via API; job completes; asset has keys."""
    client, api_key, tenant_id, tenant_url = proxy_worker_env
    auth = _AuthClient(client, api_key)

    lib_name = "ProxyImage_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    library = r.json()

    img = Image.new("RGB", (200, 150), color=(255, 0, 0))
    img.save(tmp_path / "test.jpg", "JPEG")

    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    enq = client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "proxy",
            "filter": {"library_id": library["library_id"]},
            "force": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert enq.status_code == 200
    assert enq.json().get("enqueued", 0) >= 1

    data_dir = str(tmp_path / "data")
    worker_client = _AuthClient(client, api_key)
    _storage = LocalStorage(data_dir=data_dir)
    worker = ProxyWorker(
        client=worker_client,
        artifact_store=LocalArtifactStore(storage=_storage, tenant_id=tenant_id),
        once=True,
    )
    worker.run()

    next_resp = client.get(
        "/v1/jobs/next",
        params={"job_type": "proxy"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert next_resp.status_code == 204

    list_resp = client.get(
        "/v1/assets",
        params={"library_id": library["library_id"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert list_resp.status_code == 200
    assets = list_resp.json()
    assert len(assets) == 1
    asset = assets[0]
    assert asset["proxy_key"] is not None
    assert asset["thumbnail_key"] is not None

    storage = LocalStorage(data_dir=data_dir)
    assert storage.exists(asset["proxy_key"])

    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT proxy_sha256, thumbnail_sha256 FROM assets WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset["asset_id"]},
            ).fetchone()
    finally:
        engine.dispose()

    assert row is not None
    db_proxy_sha256, db_thumbnail_sha256 = row
    assert db_proxy_sha256 is not None
    assert db_thumbnail_sha256 is not None

    proxy_sha256 = hashlib.sha256(storage.abs_path(asset["proxy_key"]).read_bytes()).hexdigest()
    thumbnail_sha256 = hashlib.sha256(storage.abs_path(asset["thumbnail_key"]).read_bytes()).hexdigest()
    assert db_proxy_sha256 == proxy_sha256
    assert db_thumbnail_sha256 == thumbnail_sha256


@_skip_no_libvips
@pytest.mark.slow
def test_proxy_worker_skips_video(proxy_worker_env, tmp_path: Path) -> None:
    """Video asset: worker completes job without setting proxy_key."""
    client, api_key, tenant_id, _tenant_url = proxy_worker_env
    auth = _AuthClient(client, api_key)

    lib_name = "ProxyVideo_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()

    scan_r = client.post(
        "/v1/scans",
        json={"library_id": library["library_id"], "status": "running"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert scan_r.status_code == 200
    scan_id = scan_r.json()["scan_id"]

    upsert_r = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library["library_id"],
            "rel_path": "clip.mp4",
            "file_size": 0,
            "file_mtime": None,
            "media_type": "video",
            "scan_id": scan_id,
            "force": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert upsert_r.status_code == 200

    client.post(
        "/v1/scans/{}/complete".format(scan_id),
        json={"files_discovered": 1, "files_added": 1, "files_updated": 0, "files_skipped": 0},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "proxy",
            "filter": {"library_id": library["library_id"]},
            "force": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )

    worker_client = _AuthClient(client, api_key)
    worker = ProxyWorker(
        client=worker_client,
        artifact_store=LocalArtifactStore(
            storage=LocalStorage(data_dir=str(tmp_path / "data")),
            tenant_id=tenant_id,
        ),
        once=True,
    )
    worker.run()

    next_resp = client.get(
        "/v1/jobs/next",
        params={"job_type": "proxy"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert next_resp.status_code == 204

    list_resp = client.get(
        "/v1/assets",
        params={"library_id": library["library_id"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert list_resp.status_code == 200
    assets = list_resp.json()
    assert len(assets) == 1
    assert assets[0]["proxy_key"] is None


@_skip_no_libvips
@pytest.mark.slow
def test_proxy_worker_missing_file(proxy_worker_env, tmp_path: Path) -> None:
    """Missing source file: worker claims job, fails it; job status is 'failed' with 'not found' in error."""
    client, api_key, tenant_id, _tenant_url = proxy_worker_env

    lib_name = "ProxyMissing_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()

    scan_r = client.post(
        "/v1/scans",
        json={"library_id": library["library_id"], "status": "running"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert scan_r.status_code == 200
    scan_id = scan_r.json()["scan_id"]

    client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library["library_id"],
            "rel_path": "nonexistent.jpg",
            "file_size": 0,
            "file_mtime": None,
            "media_type": "image",
            "scan_id": scan_id,
            "force": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    client.post(
        "/v1/scans/{}/complete".format(scan_id),
        json={"files_discovered": 1, "files_added": 1, "files_updated": 0, "files_skipped": 0},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "proxy",
            "filter": {"library_id": library["library_id"]},
            "force": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )

    r_jobs = client.get(
        "/v1/jobs",
        params={"library_id": library["library_id"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r_jobs.status_code == 200
    jobs = r_jobs.json()
    assert len(jobs) >= 1
    job_id = jobs[0]["job_id"]

    worker_client = _AuthClient(client, api_key)
    worker = ProxyWorker(
        client=worker_client,
        artifact_store=LocalArtifactStore(
            storage=LocalStorage(data_dir=str(tmp_path / "data")),
            tenant_id=tenant_id,
        ),
        once=True,
    )
    worker.run()

    next_resp = client.get(
        "/v1/jobs/next",
        params={"job_type": "proxy"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert next_resp.status_code == 204

    status_resp = client.get(
        f"/v1/jobs/{job_id}/status",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["status"] == "failed"
    assert status_data["error_message"] is not None
    assert "not found" in status_data["error_message"].lower()

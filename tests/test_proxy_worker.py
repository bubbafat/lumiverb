"""Proxy worker tests: API-only. Process image, skip video, missing file. Use TestClient + testcontainers."""

import os
import secrets
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

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
from src.storage.local import LocalStorage
from src.workers.proxy import ProxyWorker


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


class _AuthClient:
    """HTTP client that wraps TestClient and adds Authorization. Returns response without exiting."""

    def __init__(self, client: TestClient, api_key: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def get(self, path: str, **kwargs: object) -> object:
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._headers)
        return self._client.get(path, **kwargs)

    def post(self, path: str, **kwargs: object) -> object:
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._headers)
        return self._client.post(path, **kwargs)


@pytest.fixture(scope="module")
def proxy_worker_env():
    """Two testcontainers Postgres; create tenant; yield (TestClient, api_key, tenant_id)."""
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
                yield client, api_key, tenant_id
        _engines.clear()


@pytest.mark.slow
def test_proxy_worker_processes_image(proxy_worker_env, tmp_path: Path) -> None:
    """Worker generates proxy and thumbnail via API; job completes; asset has keys."""
    client, api_key, tenant_id = proxy_worker_env
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
        json={"library_id": library["library_id"], "job_type": "proxy"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert enq.status_code == 200
    assert enq.json().get("enqueued", 0) >= 1

    data_dir = str(tmp_path / "data")
    worker_client = _AuthClient(client, api_key)
    worker = ProxyWorker(
        client=worker_client,
        storage=LocalStorage(data_dir=data_dir),
        tenant_id=tenant_id,
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


@pytest.mark.slow
def test_proxy_worker_skips_video(proxy_worker_env, tmp_path: Path) -> None:
    """Video asset: worker completes job without setting proxy_key."""
    client, api_key, tenant_id = proxy_worker_env
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
        json={"library_id": library["library_id"], "job_type": "proxy"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    worker_client = _AuthClient(client, api_key)
    worker = ProxyWorker(
        client=worker_client,
        storage=LocalStorage(data_dir=str(tmp_path / "data")),
        tenant_id=tenant_id,
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


@pytest.mark.slow
def test_proxy_worker_missing_file(proxy_worker_env, tmp_path: Path) -> None:
    """Missing source file: worker claims job, fails it; job status is 'failed' with 'not found' in error."""
    client, api_key, tenant_id = proxy_worker_env

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
        json={"library_id": library["library_id"], "job_type": "proxy"},
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
        storage=LocalStorage(data_dir=str(tmp_path / "data")),
        tenant_id=tenant_id,
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

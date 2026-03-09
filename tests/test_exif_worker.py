"""EXIF worker tests: job completion stores exif/sha256; missing_exif filter."""

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
from src.workers.exif_worker import ExifWorker


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
def exif_worker_env():
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
                    json={"name": "ExifWorkerTenant", "plan": "free"},
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
def test_exif_job_complete_stores_exif(exif_worker_env: tuple, tmp_path: Path) -> None:
    """Create asset, enqueue exif job, POST complete with exif payload; asset has camera_make, exif_extracted_at."""
    client, api_key, _tenant_id = exif_worker_env
    auth = _AuthClient(client, api_key)

    lib_name = "ExifStore_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    library = r.json()

    (tmp_path / "photo.jpg").write_bytes(b"fake jpeg")
    img = Image.new("RGB", (10, 10), color=(0, 128, 255))
    img.save(tmp_path / "photo.jpg", "JPEG")

    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    enq = client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "exif",
            "filter": {"library_id": library["library_id"]},
            "force": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert enq.status_code == 200
    assert enq.json().get("enqueued", 0) >= 1

    next_resp = client.get(
        "/v1/jobs/next",
        params={"job_type": "exif"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert next_resp.status_code == 200
    job = next_resp.json()
    job_id = job["job_id"]
    asset_id = job["asset_id"]

    complete_resp = client.post(
        f"/v1/jobs/{job_id}/complete",
        json={
            "sha256": "a" * 64,
            "exif": {"Make": "Canon", "Model": "EOS R5"},
            "camera_make": "Canon",
            "camera_model": "EOS R5",
            "taken_at": None,
            "gps_lat": None,
            "gps_lon": None,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert complete_resp.status_code == 200

    list_resp = client.get(
        "/v1/assets",
        params={"library_id": library["library_id"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert list_resp.status_code == 200
    assets = list_resp.json()
    assert len(assets) == 1
    asset = assets[0]
    assert asset["asset_id"] == asset_id
    assert asset["camera_make"] == "Canon"
    assert asset["camera_model"] == "EOS R5"
    assert asset["exif_extracted_at"] is not None


@pytest.mark.slow
def test_exif_job_complete_updates_sha256(exif_worker_env: tuple, tmp_path: Path) -> None:
    """Complete exif job with sha256; assert asset sha256 updated."""
    client, api_key, _tenant_id = exif_worker_env
    auth = _AuthClient(client, api_key)

    lib_name = "ExifSha256_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()

    img = Image.new("RGB", (20, 20), color=(255, 0, 0))
    img.save(tmp_path / "red.jpg", "JPEG")

    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "exif", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    next_resp = client.get(
        "/v1/jobs/next",
        params={"job_type": "exif"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert next_resp.status_code == 200
    job = next_resp.json()
    expected_sha = "b" * 64

    client.post(
        f"/v1/jobs/{job['job_id']}/complete",
        json={
            "sha256": expected_sha,
            "exif": {},
            "camera_make": None,
            "camera_model": None,
            "taken_at": None,
            "gps_lat": None,
            "gps_lon": None,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )

    list_resp = client.get(
        "/v1/assets",
        params={"library_id": library["library_id"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert list_resp.status_code == 200
    assets = list_resp.json()
    assert len(assets) == 1
    assert assets[0]["sha256"] == expected_sha


@pytest.mark.slow
def test_exif_missing_exif_filter(exif_worker_env: tuple, tmp_path: Path) -> None:
    """Create assets with/without exif_extracted_at; enqueue with missing_exif=True; only unprocessed enqueued."""
    client, api_key, _tenant_id = exif_worker_env
    auth = _AuthClient(client, api_key)

    lib_name = "ExifFilter_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()

    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    (tmp_path / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    enq_all = client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "exif",
            "filter": {"library_id": library["library_id"]},
            "force": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert enq_all.status_code == 200
    assert enq_all.json().get("enqueued", 0) == 2

    next1 = client.get("/v1/jobs/next", params={"job_type": "exif"}, headers={"Authorization": f"Bearer {api_key}"})
    assert next1.status_code == 200
    job1 = next1.json()
    client.post(
        f"/v1/jobs/{job1['job_id']}/complete",
        json={"sha256": "x", "exif": {}, "camera_make": None, "camera_model": None, "taken_at": None, "gps_lat": None, "gps_lon": None},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    enq_missing = client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "exif",
            "filter": {"library_id": library["library_id"], "missing_exif": True},
            "force": True,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert enq_missing.status_code == 200
    enqueued = enq_missing.json().get("enqueued", 0)
    assert enqueued == 1, "missing_exif=True should enqueue only the asset without exif_extracted_at"

"""Proxy worker tests: process image, skip video, missing file. Use testcontainers Postgres."""

import os
import secrets
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer
from ulid import ULID

from src.core.config import get_settings
from src.core.database import _engines, get_engine_for_url
from src.repository.tenant import (
    AssetRepository,
    LibraryRepository,
    ScanRepository,
    WorkerJobRepository,
)
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


@pytest.fixture(scope="module")
def proxy_worker_env():
    """Two testcontainers Postgres; create tenant; yield (tenant_session, tenant_id, tmp_path_factory)."""
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

        from src.api.main import app
        from fastapi.testclient import TestClient
        from src.repository.control_plane import TenantDbRoutingRepository
        from src.core.database import get_control_session

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

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            engine = get_engine_for_url(tenant_url)
            session = Session(engine)
            try:
                yield (session, tenant_id)
            finally:
                session.close()
        _engines.clear()


@pytest.mark.slow
def test_proxy_worker_processes_image(proxy_worker_env, tmp_path: Path) -> None:
    """Worker generates proxy and thumbnail for a real JPEG; job completes, asset has keys."""
    tenant_session, tenant_id = proxy_worker_env
    # Real 200x150 JPEG
    img = Image.new("RGB", (200, 150), color=(255, 0, 0))
    img.save(tmp_path / "test.jpg", "JPEG")

    lib_repo = LibraryRepository(tenant_session)
    scan_repo = ScanRepository(tenant_session)
    asset_repo = AssetRepository(tenant_session)
    job_repo = WorkerJobRepository(tenant_session)

    lib = lib_repo.create(
        name="ProxyImage_" + secrets.token_urlsafe(4),
        root_path=str(tmp_path),
    )
    scan = scan_repo.create(library_id=lib.library_id, status="complete")
    asset = asset_repo.create_for_scan(
        library_id=lib.library_id,
        rel_path="test.jpg",
        file_size=(tmp_path / "test.jpg").stat().st_size,
        file_mtime=None,
        media_type="image",
        scan_id=scan.scan_id,
    )
    job = job_repo.create("proxy", asset.asset_id)

    data_dir = str(tmp_path / "data")
    worker = ProxyWorker(
        tenant_session=tenant_session,
        tenant_id=tenant_id,
        once=True,
    )
    worker._storage = LocalStorage(data_dir=data_dir)
    worker.run()

    tenant_session.refresh(job)
    tenant_session.refresh(asset)
    assert job.status == "completed"

    asset = asset_repo.get_by_id(asset.asset_id)
    assert asset is not None
    assert asset.proxy_key is not None
    assert asset.thumbnail_key is not None

    storage = LocalStorage(data_dir=data_dir)
    assert storage.exists(asset.proxy_key)


@pytest.mark.slow
def test_proxy_worker_skips_video(proxy_worker_env, tmp_path: Path) -> None:
    """Video asset: worker completes job without setting proxy_key."""
    tenant_session, tenant_id = proxy_worker_env
    lib_repo = LibraryRepository(tenant_session)
    scan_repo = ScanRepository(tenant_session)
    asset_repo = AssetRepository(tenant_session)
    job_repo = WorkerJobRepository(tenant_session)

    lib = lib_repo.create(
        name="ProxyVideo_" + secrets.token_urlsafe(4),
        root_path=str(tmp_path),
    )
    scan = scan_repo.create(library_id=lib.library_id, status="complete")
    asset = asset_repo.create_for_scan(
        library_id=lib.library_id,
        rel_path="clip.mp4",
        file_size=0,
        file_mtime=None,
        media_type="video",
        scan_id=scan.scan_id,
    )
    job = job_repo.create("proxy", asset.asset_id)

    worker = ProxyWorker(
        tenant_session=tenant_session,
        tenant_id=tenant_id,
        once=True,
    )
    worker.run()

    tenant_session.refresh(job)
    assert job.status == "completed"

    asset = asset_repo.get_by_id(asset.asset_id)
    assert asset is not None
    assert asset.proxy_key is None


@pytest.mark.slow
def test_proxy_worker_missing_file(proxy_worker_env, tmp_path: Path) -> None:
    """Missing source file: job fails with error_message containing 'not found'."""
    tenant_session, tenant_id = proxy_worker_env
    lib_repo = LibraryRepository(tenant_session)
    scan_repo = ScanRepository(tenant_session)
    asset_repo = AssetRepository(tenant_session)
    job_repo = WorkerJobRepository(tenant_session)

    lib = lib_repo.create(
        name="ProxyMissing_" + secrets.token_urlsafe(4),
        root_path=str(tmp_path),
    )
    scan = scan_repo.create(library_id=lib.library_id, status="complete")
    asset = asset_repo.create_for_scan(
        library_id=lib.library_id,
        rel_path="nonexistent.jpg",
        file_size=0,
        file_mtime=None,
        media_type="image",
        scan_id=scan.scan_id,
    )
    job = job_repo.create("proxy", asset.asset_id)

    worker = ProxyWorker(
        tenant_session=tenant_session,
        tenant_id=tenant_id,
        once=True,
    )
    worker.run()

    tenant_session.refresh(job)
    assert job.status == "failed"
    assert job.error_message is not None
    assert "not found" in job.error_message.lower()

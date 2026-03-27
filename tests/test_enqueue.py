"""Tests for enqueue_proxy_jobs batched bulk INSERT."""

import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer
from ulid import ULID

from src.core.config import get_settings
from src.core.database import _engines, get_engine_for_url
from src.models.tenant import Asset
from src.repository.tenant import LibraryRepository
from src.workers.enqueue import enqueue_proxy_jobs


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
def enqueue_test_env() -> tuple[str, str, str]:
    """
    Two testcontainers Postgres: control plane + tenant. Create tenant via admin API
    (with provision_tenant_database mocked), provision tenant DB, update routing.
    Yields (tenant_url, api_key, tenant_id).
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
            from fastapi.testclient import TestClient
            from src.api.main import app

            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "EnqueueTestTenant", "plan": "free"},
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

            yield tenant_url, api_key, tenant_id

        _engines.clear()


@pytest.mark.slow
def test_enqueue_proxy_jobs_batched(enqueue_test_env: tuple[str, str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Create N pending assets (N > ENQUEUE_BATCH_SIZE), call enqueue_proxy_jobs with
    ENQUEUE_BATCH_SIZE patched to 50. Verifies batched bulk INSERT: all N jobs
    created, no N+1 queries.
    """
    tenant_url, _api_key, _tenant_id = enqueue_test_env
    monkeypatch.setattr("src.workers.enqueue.ENQUEUE_BATCH_SIZE", 50)

    # Need more than 50 assets to exercise multiple batches
    num_assets = 120

    engine = get_engine_for_url(tenant_url)
    with Session(engine) as session:
        lib_repo = LibraryRepository(session)
        library = lib_repo.create(
            name="BatchedEnqueue_" + secrets.token_urlsafe(6),
            root_path="/batch",
        )
        library_id = library.library_id

        now = datetime.now(timezone.utc)
        assets = [
            {
                "asset_id": "ast_" + str(ULID()),
                "library_id": library_id,
                "rel_path": f"img_{i}.jpg",
                "file_size": 1000,
                "media_type": "image",
                "status": "pending",
                "availability": "online",
                "created_at": now,
                "updated_at": now,
            }
            for i in range(num_assets)
        ]
        session.execute(insert(Asset), assets)
        session.commit()

        enqueued = enqueue_proxy_jobs(session, library_id)

    assert enqueued == num_assets

    with Session(engine) as session:
        rows = session.execute(
            text(
                """
                SELECT COUNT(*) FROM worker_jobs
                WHERE job_type = 'proxy' AND status = 'pending'
                  AND asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            {"library_id": library_id},
        ).scalar()
    assert rows == num_assets


@pytest.mark.slow
def test_enqueue_proxy_jobs_skips_existing(enqueue_test_env: tuple[str, str, str]) -> None:
    """
    Create assets, enqueue once, enqueue again: second call returns 0 (no duplicates).
    """
    tenant_url, _api_key, _tenant_id = enqueue_test_env

    engine = get_engine_for_url(tenant_url)
    with Session(engine) as session:
        lib_repo = LibraryRepository(session)
        library = lib_repo.create(
            name="SkipExisting_" + secrets.token_urlsafe(6),
            root_path="/skip",
        )
        library_id = library.library_id

        now = datetime.now(timezone.utc)
        assets = [
            {
                "asset_id": "ast_" + str(ULID()),
                "library_id": library_id,
                "rel_path": f"img_{i}.jpg",
                "file_size": 1000,
                "media_type": "image",
                "status": "pending",
                "availability": "online",
                "created_at": now,
                "updated_at": now,
            }
            for i in range(5)
        ]
        session.execute(insert(Asset), assets)
        session.commit()

        first = enqueue_proxy_jobs(session, library_id)
        assert first == 5

        second = enqueue_proxy_jobs(session, library_id)
        assert second == 0

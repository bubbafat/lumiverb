"""Tests for retry-failed enqueue logic (internal function tests)."""

import os
import secrets
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
from src.models.filter import AssetFilterSpec
from src.models.tenant import Asset
from src.repository.tenant import LibraryRepository, WorkerJobRepository
from src.workers.enqueue import enqueue_jobs_for_filter
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def retry_test_env() -> tuple[str, str, str]:
    """Control + tenant Postgres, tenant created and routed. Yields (tenant_url, api_key, tenant_id)."""
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
            from src.api.main import app as api_app

            with TestClient(api_app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "RetryEnqueueTenant_" + secrets.token_urlsafe(6), "plan": "free"},
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
def test_enqueue_retry_failed_only_retries_failed_jobs(
    retry_test_env: tuple[str, str, str],
) -> None:
    """
    Create a library with 3 assets.
    Mark one job as failed, one as completed, one as pending.
    Call enqueue with retry_failed=True.
    Assert only the failed job gets a new pending job created.
    Assert completed and pending jobs are untouched.
    """
    tenant_url, _api_key, _tenant_id = retry_test_env

    engine = get_engine_for_url(tenant_url)
    with Session(engine) as session:
        lib_repo = LibraryRepository(session)
        library = lib_repo.create(
            name="Retry_" + secrets.token_urlsafe(6),
            root_path="/retry",
        )
        library_id = library.library_id

        now = datetime.now(timezone.utc)
        asset_failed = "ast_" + str(ULID())
        asset_completed = "ast_" + str(ULID())
        asset_pending = "ast_" + str(ULID())

        assets = [
            {
                "asset_id": asset_failed,
                "library_id": library_id,
                "rel_path": "failed.jpg",
                "file_size": 1000,
                "media_type": "image",
                "status": "pending",
                "availability": "online",
                "created_at": now,
                "updated_at": now,
            },
            {
                "asset_id": asset_completed,
                "library_id": library_id,
                "rel_path": "completed.jpg",
                "file_size": 1000,
                "media_type": "image",
                "status": "pending",
                "availability": "online",
                "created_at": now,
                "updated_at": now,
            },
            {
                "asset_id": asset_pending,
                "library_id": library_id,
                "rel_path": "pending.jpg",
                "file_size": 1000,
                "media_type": "image",
                "status": "pending",
                "availability": "online",
                "created_at": now,
                "updated_at": now,
            },
        ]
        session.execute(insert(Asset), assets)
        session.commit()

        job_repo = WorkerJobRepository(session)
        job_failed = job_repo.create("proxy", asset_failed)
        job_completed = job_repo.create("proxy", asset_completed)
        job_pending = job_repo.create("proxy", asset_pending)

        job_repo.set_failed(job_failed, "Simulated failure")
        job_repo.set_completed(job_completed)
        # job_pending stays pending

        session.commit()

        spec = AssetFilterSpec(library_id=library_id, retry_failed=True)
        n = enqueue_jobs_for_filter(session, spec, "proxy", force=False)
        assert n == 1, "Only the asset with failed job should be enqueued"

        # Asset with failed job: reset in-place to pending (fail_count preserved)
        failed_jobs = session.execute(
            text(
                "SELECT status FROM worker_jobs WHERE asset_id = :aid AND job_type = 'proxy' ORDER BY created_at"
            ),
            {"aid": asset_failed},
        ).fetchall()
        statuses = [r[0] for r in failed_jobs]
        assert statuses == ["pending"], f"Failed job should be reset to pending, got {statuses}"

        # Asset with completed job: untouched (no new job)
        completed_count = session.execute(
            text(
                "SELECT COUNT(*) FROM worker_jobs WHERE asset_id = :aid AND job_type = 'proxy'"
            ),
            {"aid": asset_completed},
        ).scalar()
        assert completed_count == 1, "Completed job should remain single, no new job"
        row = session.execute(
            text("SELECT status FROM worker_jobs WHERE asset_id = :aid AND job_type = 'proxy'"),
            {"aid": asset_completed},
        ).fetchone()
        assert row[0] == "completed"

        # Asset with pending job: untouched (no new job)
        pending_count = session.execute(
            text(
                "SELECT COUNT(*) FROM worker_jobs WHERE asset_id = :aid AND job_type = 'proxy'"
            ),
            {"aid": asset_pending},
        ).scalar()
        assert pending_count == 1, "Pending job should remain single, no new job"
        row = session.execute(
            text("SELECT status FROM worker_jobs WHERE asset_id = :aid AND job_type = 'proxy'"),
            {"aid": asset_pending},
        ).fetchone()
        assert row[0] == "pending"

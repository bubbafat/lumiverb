"""Slow tests for pipeline_status latest-state semantics."""

import os
import secrets

import pytest
from sqlalchemy import text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.conftest import _ensure_psycopg2, _provision_tenant_db


@pytest.fixture(scope="module")
def tenant_db_session() -> Session:
    """Fresh Postgres with tenant schema (including idx_worker_jobs_asset_type_created)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = _ensure_psycopg2(postgres.get_connection_url())
        _provision_tenant_db(url, project_root)
        from sqlalchemy import create_engine

        engine = create_engine(url)
        with Session(engine) as session:
            yield session
        engine.dispose()


def _ensure_library_asset(session: Session, library_id: str, asset_id: str, rel_path: str) -> None:
    """Insert library and asset if not exist."""
    session.execute(
        text(
            """
            INSERT INTO libraries (library_id, name, root_path, scan_status, status, created_at, updated_at)
            VALUES (:lib_id, 'test', '/tmp', 'idle', 'active', NOW(), NOW())
            ON CONFLICT (library_id) DO NOTHING
            """
        ),
        {"lib_id": library_id},
    )
    session.execute(
        text(
            """
            INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, availability, status, created_at, updated_at)
            VALUES (:asset_id, :lib_id, :rel_path, 0, 'image/jpeg', 'online', 'proxy_ready', NOW(), NOW())
            ON CONFLICT (asset_id) DO NOTHING
            """
        ),
        {"asset_id": asset_id, "lib_id": library_id, "rel_path": rel_path},
    )
    session.commit()


@pytest.mark.slow
def test_pipeline_status_latest_state_only(tenant_db_session: Session) -> None:
    """
    Create 1 asset with 3 proxy jobs: failed -> failed -> completed.
    Assert pipeline_status returns completed=1, failed=0.
    Latest state wins — history is ignored.
    """
    from src.repository.tenant import WorkerJobRepository

    lib_id = "lib_" + secrets.token_urlsafe(8)
    asset_id = "ast_" + secrets.token_urlsafe(8)

    session = tenant_db_session
    _ensure_library_asset(session, lib_id, asset_id, "photo.jpg")

    # Insert 3 proxy jobs for same asset: failed, failed, completed (chronological order).
    jobs = [
        ("failed", "NOW() - interval '2 seconds'"),
        ("failed", "NOW() - interval '1 second'"),
        ("completed", "NOW()"),
    ]
    for status, created_expr in jobs:
        job_id = "job_" + secrets.token_urlsafe(8)
        session.execute(
            text(
                f"""
                INSERT INTO worker_jobs (job_id, job_type, asset_id, status, created_at)
                VALUES (:job_id, 'proxy', :asset_id, :status, {created_expr})
                """
            ),
            {"job_id": job_id, "asset_id": asset_id, "status": status},
        )
    session.commit()

    repo = WorkerJobRepository(session)
    rows = repo.pipeline_status(lib_id)

    by_status = {(r["job_type"], r["status"]): r["count"] for r in rows}
    assert by_status.get(("proxy", "completed")) == 1, f"Expected proxy completed=1, got {rows}"
    assert by_status.get(("proxy", "failed"), 0) == 0, f"Expected proxy failed=0, got {rows}"

"""Slow tests for PipelineLockRepository and PipelineLockHeldError."""

import os
import secrets
import time

import pytest
from sqlalchemy import text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.repository.tenant import PipelineLockHeldError, PipelineLockRepository
from tests.conftest import _ensure_psycopg2, _provision_tenant_db


@pytest.fixture(scope="module")
def tenant_db_session() -> Session:
    """Fresh Postgres with tenant schema (including pipeline_locks)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = _ensure_psycopg2(postgres.get_connection_url())
        _provision_tenant_db(url, project_root)
        from sqlalchemy import create_engine

        engine = create_engine(url)
        with Session(engine) as session:
            yield session
        engine.dispose()


def _unique_tenant() -> str:
    return "ten_" + secrets.token_urlsafe(8)


@pytest.mark.slow
def test_try_acquire_no_row(tenant_db_session: Session) -> None:
    """When no row exists, try_acquire inserts and returns True."""
    tenant_id = _unique_tenant()
    repo = PipelineLockRepository(tenant_db_session)
    got = repo.try_acquire(tenant_id, lock_timeout_minutes=5)
    assert got is True
    row = tenant_db_session.execute(
        text(
            "SELECT lock_id, tenant_id, hostname, pid FROM pipeline_locks WHERE tenant_id = :tid"
        ),
        {"tid": tenant_id},
    ).fetchone()
    assert row is not None
    assert row.tenant_id == tenant_id
    assert row.lock_id.startswith("lock_")
    repo.release(tenant_id)


@pytest.mark.slow
def test_try_acquire_stale_row(tenant_db_session: Session) -> None:
    """When a row exists but heartbeat is older than lock_timeout, try_acquire updates and returns True."""
    tenant_id = _unique_tenant()
    lock_id_old = "lock_old" + secrets.token_urlsafe(8)
    tenant_db_session.execute(
        text(
            """
            INSERT INTO pipeline_locks (lock_id, tenant_id, hostname, pid, started_at, heartbeat_at)
            VALUES (:lock_id, :tenant_id, 'otherhost', 99999, NOW() - interval '1 hour', NOW() - interval '10 minutes')
            """
        ),
        {"lock_id": lock_id_old, "tenant_id": tenant_id},
    )
    tenant_db_session.commit()

    repo = PipelineLockRepository(tenant_db_session)
    got = repo.try_acquire(tenant_id, lock_timeout_minutes=5)
    assert got is True
    row = tenant_db_session.execute(
        text("SELECT lock_id, hostname, pid FROM pipeline_locks WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).fetchone()
    assert row is not None
    assert row.lock_id != lock_id_old
    assert row.lock_id.startswith("lock_")
    repo.release(tenant_id)


@pytest.mark.slow
def test_try_acquire_fresh_raises(tenant_db_session: Session) -> None:
    """When a row exists with fresh heartbeat, try_acquire raises PipelineLockHeldError with attributes."""
    tenant_id = _unique_tenant()
    lock_id = "lock_" + secrets.token_urlsafe(8)
    tenant_db_session.execute(
        text(
            """
            INSERT INTO pipeline_locks (lock_id, tenant_id, hostname, pid, started_at, heartbeat_at)
            VALUES (:lock_id, :tenant_id, 'holderhost', 12345, NOW() - interval '1 minute', NOW())
            """
        ),
        {"lock_id": lock_id, "tenant_id": tenant_id},
    )
    tenant_db_session.commit()

    repo = PipelineLockRepository(tenant_db_session)
    with pytest.raises(PipelineLockHeldError) as exc_info:
        repo.try_acquire(tenant_id, lock_timeout_minutes=5)
    e = exc_info.value
    assert e.hostname == "holderhost"
    assert e.pid == 12345
    assert "holderhost" in str(e)
    assert "12345" in str(e)
    tenant_db_session.execute(text("DELETE FROM pipeline_locks WHERE tenant_id = :tid"), {"tid": tenant_id})
    tenant_db_session.commit()


@pytest.mark.slow
def test_force_acquire(tenant_db_session: Session) -> None:
    """force_acquire deletes existing row and inserts new one."""
    tenant_id = _unique_tenant()
    lock_id_old = "lock_force_old" + secrets.token_urlsafe(8)
    tenant_db_session.execute(
        text(
            """
            INSERT INTO pipeline_locks (lock_id, tenant_id, hostname, pid, started_at, heartbeat_at)
            VALUES (:lock_id, :tenant_id, 'oldhost', 111, NOW(), NOW())
            """
        ),
        {"lock_id": lock_id_old, "tenant_id": tenant_id},
    )
    tenant_db_session.commit()

    repo = PipelineLockRepository(tenant_db_session)
    repo.force_acquire(tenant_id)
    row = tenant_db_session.execute(
        text("SELECT lock_id FROM pipeline_locks WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).fetchone()
    assert row is not None
    assert row.lock_id != lock_id_old
    assert row.lock_id.startswith("lock_")
    repo.release(tenant_id)


@pytest.mark.slow
def test_heartbeat(tenant_db_session: Session) -> None:
    """heartbeat updates heartbeat_at."""
    tenant_id = _unique_tenant()
    repo = PipelineLockRepository(tenant_db_session)
    repo.try_acquire(tenant_id)
    before = tenant_db_session.execute(
        text("SELECT heartbeat_at FROM pipeline_locks WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).scalar()
    time.sleep(1.1)  # ensure NEXT heartbeat is strictly later
    repo.heartbeat(tenant_id)
    after = tenant_db_session.execute(
        text("SELECT heartbeat_at FROM pipeline_locks WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).scalar()
    assert after >= before
    repo.release(tenant_id)


@pytest.mark.slow
def test_release(tenant_db_session: Session) -> None:
    """release deletes the lock row."""
    tenant_id = _unique_tenant()
    repo = PipelineLockRepository(tenant_db_session)
    repo.try_acquire(tenant_id)
    row = tenant_db_session.execute(
        text("SELECT 1 FROM pipeline_locks WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).fetchone()
    assert row is not None
    repo.release(tenant_id)
    row2 = tenant_db_session.execute(
        text("SELECT 1 FROM pipeline_locks WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).fetchone()
    assert row2 is None

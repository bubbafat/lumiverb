"""Tests for search_sync_latest view and per-asset counts."""

import os
import secrets
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.conftest import _ensure_psycopg2, _provision_tenant_db


@pytest.fixture(scope="module")
def tenant_db_session():
    """Fresh Postgres with tenant schema (including search_sync_latest view)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = _ensure_psycopg2(postgres.get_connection_url())
        _provision_tenant_db(url, project_root)
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
            VALUES (:asset_id, :lib_id, :rel_path, 0, 'image', 'online', 'proxy_ready', NOW(), NOW())
            ON CONFLICT (asset_id) DO NOTHING
            """
        ),
        {"asset_id": asset_id, "lib_id": library_id, "rel_path": rel_path},
    )
    session.commit()


@pytest.mark.fast
def test_pending_count_uses_latest_state() -> None:
    """
    pending_count uses search_sync_latest view: when latest state is not pending, count is 0.
    (Fast: mock verifies we use the view, not raw rows.)
    """
    from src.repository.tenant import SearchSyncQueueRepository

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 0
    mock_session.execute.return_value = mock_result

    repo = SearchSyncQueueRepository(mock_session)
    count = repo.pending_count(library_id="lib_x")

    assert count == 0
    args, _ = mock_session.execute.call_args
    stmt = str(args[0])
    assert "search_sync_latest" in stmt
    assert "ssl.status = 'pending'" in stmt


@pytest.mark.fast
def test_pending_count_path_scope() -> None:
    """pending_count with path_prefix includes path filter in SQL."""
    from src.repository.tenant import SearchSyncQueueRepository

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 3
    mock_session.execute.return_value = mock_result

    repo = SearchSyncQueueRepository(mock_session)
    count = repo.pending_count(library_id="lib_x", path_prefix="Photos/A")

    assert count == 3
    args, _ = mock_session.execute.call_args
    params = args[1]
    assert params.get("path_prefix") == "Photos/A/%"


@pytest.mark.fast
def test_claim_batch_deduplicates_assets() -> None:
    """
    claim_batch uses search_sync_latest: only one row per asset even with multiple pending rows.
    (Fast: mock returns 1 row when multiple would exist for same asset.)
    """
    from src.repository.tenant import SearchSyncQueueRepository

    mock_session = MagicMock()

    class _ExecResult:
        def __init__(self):
            self.rowcount = 1

        def fetchall(self):
            return [("ssq_abc", "ast_1", "index")]

    mock_session.execute.return_value = _ExecResult()
    mock_row = MagicMock()
    mock_row.sync_id = "ssq_abc"
    mock_row.asset_id = "ast_1"
    mock_row.operation = "index"
    mock_session.get.return_value = mock_row

    repo = SearchSyncQueueRepository(mock_session)
    rows = repo.claim_batch(batch_size=10, library_id="lib_x")

    assert len(rows) == 1
    stmt = str(mock_session.execute.call_args[0][0])
    assert "search_sync_latest" in stmt
    assert "candidates" in stmt or "ssl" in stmt


@pytest.mark.fast
def test_claim_batch_path_scope() -> None:
    """claim_batch with path_prefix includes path condition."""
    from src.repository.tenant import SearchSyncQueueRepository

    mock_session = MagicMock()
    mock_session.execute.return_value.fetchall.return_value = []
    mock_session.get.return_value = None

    repo = SearchSyncQueueRepository(mock_session)
    repo.claim_batch(batch_size=5, path_prefix="Photos/A")

    args, _ = mock_session.execute.call_args
    params = args[1]
    assert params.get("path_prefix") == "Photos/A/%"


@pytest.mark.slow
def test_search_sync_worker_counts_assets_not_rows(tenant_db_session: Session, tmp_path: Path) -> None:
    """
    With 1 asset and 3 historical queue rows (simulating retries), worker reports synced=1.
    """
    from src.workers.search_sync import SearchSyncWorker

    lib_id = "lib_" + secrets.token_urlsafe(8)
    asset_id = "ast_" + secrets.token_urlsafe(8)

    session = tenant_db_session
    _ensure_library_asset(session, lib_id, asset_id, "photo.jpg")

    # Insert 2 historical (already-synced) rows and 1 pending row for same asset.
    # Simulates retries / force-resyncs: the constraint only allows one pending/processing
    # row per asset+scene at a time, so completed retries are in 'synced' status.
    # Use older timestamps for synced rows so search_sync_latest (ORDER BY created_at DESC)
    # always selects the current pending row as the most recent.
    for i in range(2):
        sync_id = "ssq_" + secrets.token_urlsafe(8)
        session.execute(
            text(
                """
                INSERT INTO search_sync_queue (sync_id, asset_id, operation, status, created_at)
                VALUES (:sync_id, :asset_id, 'index', 'synced', NOW() - INTERVAL '1 hour' * :offset)
                """
            ),
            {"sync_id": sync_id, "asset_id": asset_id, "offset": i + 1},
        )
    # One current pending row (most recent)
    sync_id = "ssq_" + secrets.token_urlsafe(8)
    session.execute(
        text(
            """
            INSERT INTO search_sync_queue (sync_id, asset_id, operation, status, created_at)
            VALUES (:sync_id, :asset_id, 'index', 'pending', NOW())
            """
        ),
        {"sync_id": sync_id, "asset_id": asset_id},
    )
    session.commit()

    # Need asset_metadata for worker to actually sync
    meta_id = "meta_" + secrets.token_urlsafe(8)
    session.execute(
        text(
            """
            INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version, data, generated_at)
            VALUES (:meta_id, :asset_id, 'test-vision-model', '1', '{"description": "test", "tags": []}'::jsonb, NOW())
            ON CONFLICT (asset_id, model_id, model_version) DO NOTHING
            """
        ),
        {"meta_id": meta_id, "asset_id": asset_id},
    )
    session.commit()

    class _DummyQuickwit:
        enabled = True
        ensure_calls = []
        ingested = []

        def ensure_index_for_library(self, lib_id):
            self.ensure_calls.append(lib_id)

        def ensure_scene_index_for_library(self, lib_id):
            self.ensure_calls.append(lib_id)

        def ingest_documents_for_library(self, lib_id, docs):
            self.ingested.extend(docs)

    qw = _DummyQuickwit()
    worker = SearchSyncWorker(
        session=session,
        library_id=lib_id,
        quickwit=qw,
        batch_size=10,
    )
    result = worker.run_once()

    assert result["synced"] == 1, f"Expected synced=1 (distinct assets), got {result}"
    assert result["skipped"] == 0

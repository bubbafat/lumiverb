"""Tests for ClearClobberedVideoPreviewKeysStep.

Fast tests cover the dispatch logic with a MagicMock session. The slow
test runs the step against a real testcontainers Postgres so the LIKE
predicate, the UPDATE statement, and the idempotency check all execute
against actual SQL.
"""

from __future__ import annotations

import os
import secrets
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from src.server.upgrade.context import UpgradeContext
from src.server.upgrade.step import UpgradeStepInfo
from src.server.upgrade.steps.clear_clobbered_video_preview_keys import (
    ClearClobberedVideoPreviewKeysStep,
)
from tests.conftest import _ensure_psycopg2, _provision_tenant_db


# ---------------------------------------------------------------------------
# Helpers shared with the existing upgrade-step tests
# ---------------------------------------------------------------------------


class _FakeMetadataRepo:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self._data.get(key)

    def set_value(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete_key(self, key: str) -> None:
        self._data.pop(key, None)


def _make_exec_result(rows: list) -> MagicMock:
    m = MagicMock()
    m.first.return_value = rows[0] if rows else None
    m.fetchall.return_value = rows
    return m


# ---------------------------------------------------------------------------
# Fast unit tests
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_step_info_fields() -> None:
    assert ClearClobberedVideoPreviewKeysStep.info == UpgradeStepInfo(
        step_id="clear_clobbered_video_preview_keys",
        version="1",
        display_name="Clear clobbered video_preview_key rows",
    )


@pytest.mark.fast
def test_needs_work_true_when_bad_row_exists() -> None:
    session = MagicMock()
    session.exec.return_value = _make_exec_result([(1,)])
    ctx = UpgradeContext(session=session, metadata=_FakeMetadataRepo())

    assert ClearClobberedVideoPreviewKeysStep().needs_work(ctx) is True

    # Sanity-check the SELECT used the predicate (so a future refactor can't
    # silently swap LIKE clauses).
    sent_sql = str(session.exec.call_args[0][0])
    assert "video_preview_key LIKE '%/scenes/%'" in sent_sql
    assert "video_preview_key LIKE '%.jpg'" in sent_sql
    assert "LIMIT 1" in sent_sql


@pytest.mark.fast
def test_needs_work_false_when_no_bad_rows() -> None:
    session = MagicMock()
    session.exec.return_value = _make_exec_result([])
    ctx = UpgradeContext(session=session, metadata=_FakeMetadataRepo())

    assert ClearClobberedVideoPreviewKeysStep().needs_work(ctx) is False


@pytest.mark.fast
def test_run_returns_rowcount_and_commits() -> None:
    session = MagicMock()
    update_result = MagicMock()
    update_result.rowcount = 7
    session.exec.return_value = update_result
    ctx = UpgradeContext(session=session, metadata=_FakeMetadataRepo())

    result = ClearClobberedVideoPreviewKeysStep().run(ctx)

    assert result == {"cleared": 7}
    session.commit.assert_called_once()

    sent_sql = str(session.exec.call_args[0][0])
    assert "UPDATE assets" in sent_sql
    assert "video_preview_key = NULL" in sent_sql
    assert "video_preview_generated_at = NULL" in sent_sql
    assert "video_preview_last_accessed_at = NULL" in sent_sql


@pytest.mark.fast
def test_run_treats_none_rowcount_as_zero() -> None:
    """Some drivers return None for rowcount on UPDATE; coerce to 0."""
    session = MagicMock()
    update_result = MagicMock()
    update_result.rowcount = None
    session.exec.return_value = update_result
    ctx = UpgradeContext(session=session, metadata=_FakeMetadataRepo())

    result = ClearClobberedVideoPreviewKeysStep().run(ctx)
    assert result == {"cleared": 0}


# ---------------------------------------------------------------------------
# Slow integration test (testcontainers Postgres)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def upgrade_db():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        _provision_tenant_db(url, project_root)
        engine = create_engine(url)
        yield engine
        engine.dispose()


def _insert_library(conn, *, library_id: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO libraries
              (library_id, name, root_path, status, created_at, updated_at)
            VALUES
              (:id, :name, '/tmp', 'active', NOW(), NOW())
            ON CONFLICT DO NOTHING
            """
        ),
        {"id": library_id, "name": library_id},
    )


def _insert_video_asset(
    conn,
    *,
    asset_id: str,
    library_id: str,
    video_preview_key: str | None,
    set_timestamps: bool = False,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO assets
              (asset_id, library_id, rel_path, availability, file_size,
               media_type, status, created_at, updated_at)
            VALUES
              (:asset_id, :library_id, :rel_path, 'online', 1000,
               'video', 'proxy_ready', NOW(), NOW())
            """
        ),
        {
            "asset_id": asset_id,
            "library_id": library_id,
            "rel_path": f"{asset_id}.mp4",
        },
    )
    if video_preview_key is not None:
        conn.execute(
            text(
                "UPDATE assets"
                " SET video_preview_key = :k,"
                "     video_preview_generated_at = CASE WHEN :ts THEN NOW() ELSE NULL END,"
                "     video_preview_last_accessed_at = CASE WHEN :ts THEN NOW() ELSE NULL END"
                " WHERE asset_id = :id"
            ),
            {"k": video_preview_key, "id": asset_id, "ts": set_timestamps},
        )


def _make_ctx(engine) -> UpgradeContext:
    from sqlmodel import Session

    from src.server.repository.system_metadata import SystemMetadataRepository

    session = Session(engine)
    return UpgradeContext(
        session=session,
        metadata=SystemMetadataRepository(session),
        tenant_id="test_tenant",
    )


@pytest.mark.slow
def test_clear_clobbered_video_preview_keys_end_to_end(upgrade_db) -> None:
    lib_id = "lib_" + secrets.token_hex(4)
    clobbered_id = "ast_" + secrets.token_hex(8)
    clean_id = "ast_" + secrets.token_hex(8)
    null_id = "ast_" + secrets.token_hex(8)
    other_lib_clobbered_id = "ast_" + secrets.token_hex(8)

    clobbered_key = (
        f"ten1/{lib_id}/scenes/74/{clobbered_id}_0000018068.jpg"
    )
    clean_key = f"ten1/{lib_id}/previews/74/{clean_id}_video.mp4"
    other_lib_key = f"ten1/lib_other/scenes/12/{other_lib_clobbered_id}_0000005000.jpg"

    with upgrade_db.connect() as conn:
        _insert_library(conn, library_id=lib_id)
        _insert_library(conn, library_id="lib_other")
        _insert_video_asset(
            conn,
            asset_id=clobbered_id,
            library_id=lib_id,
            video_preview_key=clobbered_key,
            set_timestamps=True,
        )
        _insert_video_asset(
            conn,
            asset_id=clean_id,
            library_id=lib_id,
            video_preview_key=clean_key,
            set_timestamps=True,
        )
        _insert_video_asset(
            conn,
            asset_id=null_id,
            library_id=lib_id,
            video_preview_key=None,
        )
        _insert_video_asset(
            conn,
            asset_id=other_lib_clobbered_id,
            library_id="lib_other",
            video_preview_key=other_lib_key,
            set_timestamps=True,
        )
        conn.commit()

    step = ClearClobberedVideoPreviewKeysStep()
    ctx = _make_ctx(upgrade_db)
    assert step.needs_work(ctx) is True

    result = step.run(ctx)

    # Both clobbered rows (across libraries — the step is tenant-wide).
    assert result == {"cleared": 2}

    with upgrade_db.connect() as conn:
        rows = {
            r[0]: r
            for r in conn.execute(
                text(
                    "SELECT asset_id, video_preview_key,"
                    "       video_preview_generated_at,"
                    "       video_preview_last_accessed_at"
                    " FROM assets"
                    " WHERE asset_id = ANY(:ids)"
                ),
                {
                    "ids": [
                        clobbered_id,
                        clean_id,
                        null_id,
                        other_lib_clobbered_id,
                    ]
                },
            ).fetchall()
        }

    # Clobbered row: all three columns now NULL.
    clob = rows[clobbered_id]
    assert clob[1] is None
    assert clob[2] is None
    assert clob[3] is None

    # Other-lib clobbered row: also NULL — step does not filter by library.
    other = rows[other_lib_clobbered_id]
    assert other[1] is None
    assert other[2] is None
    assert other[3] is None

    # Clean MP4 row: untouched. video_preview_key still set, timestamps still
    # populated.
    clean = rows[clean_id]
    assert clean[1] == clean_key
    assert clean[2] is not None
    assert clean[3] is not None

    # Already-NULL row: unchanged (still NULL).
    nullr = rows[null_id]
    assert nullr[1] is None

    # Idempotent: a fresh ctx now reports no work, and a re-run is a no-op.
    ctx2 = _make_ctx(upgrade_db)
    assert step.needs_work(ctx2) is False
    second = step.run(ctx2)
    assert second == {"cleared": 0}

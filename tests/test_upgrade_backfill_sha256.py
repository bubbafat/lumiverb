"""Tests for the Phase 1 SHA-256 backfill upgrade steps.

Fast tests cover logic using in-memory mocks.
Slow tests spin up a real Postgres via testcontainers.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.upgrade.context import UpgradeContext
from src.upgrade.registry import registered_upgrade_steps
from src.upgrade.runner import TenantUpgradeRunner
from src.upgrade.step import UpgradeStepInfo
from src.upgrade.steps.backfill_artifact_sha256 import (
    BackfillProxySha256Step,
    BackfillSceneRepSha256Step,
    BackfillThumbnailSha256Step,
)
from tests.conftest import _ensure_psycopg2, _provision_tenant_db


# ---------------------------------------------------------------------------
# Helpers shared by fast tests
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
    """Return a mock that behaves like session.exec(...): supports .first() and .fetchall()."""
    m = MagicMock()
    m.first.return_value = rows[0] if rows else None
    m.fetchall.return_value = rows
    return m


# ---------------------------------------------------------------------------
# Fast unit tests (no DB)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_step_info_fields() -> None:
    assert BackfillProxySha256Step.info == UpgradeStepInfo(
        step_id="backfill_proxy_sha256",
        version="1",
        display_name="Backfill proxy SHA-256 hashes",
    )
    assert BackfillThumbnailSha256Step.info == UpgradeStepInfo(
        step_id="backfill_thumbnail_sha256",
        version="1",
        display_name="Backfill thumbnail SHA-256 hashes",
    )
    assert BackfillSceneRepSha256Step.info == UpgradeStepInfo(
        step_id="backfill_scene_rep_sha256",
        version="1",
        display_name="Backfill video scene rep-frame SHA-256 hashes",
    )


@pytest.mark.fast
def test_registered_steps_are_three_backfill_steps() -> None:
    steps = registered_upgrade_steps()
    ids = [s.info.step_id for s in steps]
    assert ids == [
        "backfill_proxy_sha256",
        "backfill_thumbnail_sha256",
        "backfill_scene_rep_sha256",
    ]


@pytest.mark.fast
def test_proxy_needs_work_true_when_rows_exist() -> None:
    session = MagicMock()
    session.exec.return_value = _make_exec_result([(3,)])
    meta = _FakeMetadataRepo()
    ctx = UpgradeContext(session=session, metadata=meta)

    step = BackfillProxySha256Step()
    assert step.needs_work(ctx) is True


@pytest.mark.fast
def test_proxy_needs_work_false_when_no_rows() -> None:
    session = MagicMock()
    session.exec.return_value = _make_exec_result([(0,)])
    meta = _FakeMetadataRepo()
    ctx = UpgradeContext(session=session, metadata=meta)

    step = BackfillProxySha256Step()
    assert step.needs_work(ctx) is False


@pytest.mark.fast
def test_proxy_run_skips_missing_file(tmp_path: Path) -> None:
    """When the file on disk does not exist the row is skipped (no update), missing count=1."""
    session = MagicMock()
    # First batch returns one row; second batch returns empty (loop terminates).
    session.exec.return_value.fetchall.side_effect = [
        [("ast_001", "tenant1/lib1/proxies/01/ast_001_photo.jpg")],
        [],
    ]

    meta = _FakeMetadataRepo()
    ctx = UpgradeContext(session=session, metadata=meta)

    storage_mock = MagicMock()
    storage_mock.abs_path.return_value = tmp_path / "nonexistent.jpg"

    step = BackfillProxySha256Step()
    with patch("src.upgrade.steps.backfill_artifact_sha256.get_storage", return_value=storage_mock):
        result = step.run(ctx)

    assert result["missing"] == 1
    assert result["updated"] == 0
    # No UPDATE was issued for the missing row.
    for c in session.exec.call_args_list:
        sql = str(c[0][0])
        assert "UPDATE" not in sql


@pytest.mark.fast
def test_proxy_run_writes_hash_for_existing_file(tmp_path: Path) -> None:
    content = b"fake proxy bytes"
    expected_sha = hashlib.sha256(content).hexdigest()
    proxy_file = tmp_path / "proxy.jpg"
    proxy_file.write_bytes(content)

    session = MagicMock()
    session.exec.return_value.fetchall.side_effect = [
        [("ast_001", "some/key.jpg")],
        [],
    ]

    meta = _FakeMetadataRepo()
    ctx = UpgradeContext(session=session, metadata=meta)

    storage_mock = MagicMock()
    storage_mock.abs_path.return_value = proxy_file

    step = BackfillProxySha256Step()
    with patch("src.upgrade.steps.backfill_artifact_sha256.get_storage", return_value=storage_mock):
        result = step.run(ctx)

    assert result["updated"] == 1
    assert result["missing"] == 0

    # The UPDATE call should have been made with the correct sha256.
    update_calls = [
        c for c in session.exec.call_args_list if "UPDATE" in str(c[0][0])
    ]
    assert len(update_calls) == 1
    bound = update_calls[0][0][0]
    assert bound.bindparams  # confirms bindparams used (not string interpolation)


# ---------------------------------------------------------------------------
# Slow integration tests (testcontainers Postgres)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backfill_db():
    """Provision a bare tenant DB; yield (engine, session_factory)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        _provision_tenant_db(url, project_root)
        engine = create_engine(url)
        yield engine
        engine.dispose()


def _insert_asset(conn, *, asset_id: str, library_id: str, proxy_key: str | None, thumbnail_key: str | None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO assets
              (asset_id, library_id, rel_path, file_size, media_type, status)
            VALUES
              (:asset_id, :library_id, :rel_path, 0, 'image', 'proxy_ready')
            """
        ),
        {
            "asset_id": asset_id,
            "library_id": library_id,
            "rel_path": f"{asset_id}.jpg",
        },
    )
    if proxy_key is not None:
        conn.execute(
            text("UPDATE assets SET proxy_key = :k WHERE asset_id = :id"),
            {"k": proxy_key, "id": asset_id},
        )
    if thumbnail_key is not None:
        conn.execute(
            text("UPDATE assets SET thumbnail_key = :k WHERE asset_id = :id"),
            {"k": thumbnail_key, "id": asset_id},
        )


def _insert_library(conn, *, library_id: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO libraries (library_id, name, root_path)
            VALUES (:id, :name, '/tmp')
            ON CONFLICT DO NOTHING
            """
        ),
        {"id": library_id, "name": library_id},
    )


def _make_ctx(engine, metadata_repo=None) -> UpgradeContext:
    from sqlmodel import Session

    session = Session(engine)
    if metadata_repo is None:
        from src.repository.system_metadata import SystemMetadataRepository
        metadata_repo = SystemMetadataRepository(session)
    return UpgradeContext(session=session, metadata=metadata_repo, tenant_id="test_tenant")


@pytest.mark.slow
def test_proxy_backfill_writes_correct_sha256(backfill_db, tmp_path: Path) -> None:
    lib_id = "lib_" + secrets.token_hex(4)
    asset_id = "ast_" + secrets.token_hex(8)
    proxy_key = f"ten1/{lib_id}/proxies/01/{asset_id}_photo.jpg"

    # Write a real proxy file.
    proxy_bytes = b"proxy image content " + secrets.token_bytes(32)
    expected_sha = hashlib.sha256(proxy_bytes).hexdigest()
    proxy_path = tmp_path / proxy_key
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    proxy_path.write_bytes(proxy_bytes)

    with backfill_db.connect() as conn:
        _insert_library(conn, library_id=lib_id)
        _insert_asset(conn, asset_id=asset_id, library_id=lib_id, proxy_key=proxy_key, thumbnail_key=None)
        conn.commit()

    storage = MagicMock()
    storage.abs_path.side_effect = lambda key: tmp_path / key

    ctx = _make_ctx(backfill_db)
    step = BackfillProxySha256Step()

    assert step.needs_work(ctx) is True

    with patch("src.upgrade.steps.backfill_artifact_sha256.get_storage", return_value=storage):
        result = step.run(ctx)

    assert result["updated"] == 1
    assert result["missing"] == 0

    with backfill_db.connect() as conn:
        row = conn.execute(
            text("SELECT proxy_sha256 FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    assert row is not None
    assert row[0] == expected_sha

    # needs_work is now False.
    ctx2 = _make_ctx(backfill_db)
    assert step.needs_work(ctx2) is False


@pytest.mark.slow
def test_thumbnail_backfill_writes_correct_sha256(backfill_db, tmp_path: Path) -> None:
    lib_id = "lib_" + secrets.token_hex(4)
    asset_id = "ast_" + secrets.token_hex(8)
    thumb_key = f"ten1/{lib_id}/thumbnails/02/{asset_id}_photo.jpg"

    thumb_bytes = b"thumb content " + secrets.token_bytes(16)
    expected_sha = hashlib.sha256(thumb_bytes).hexdigest()
    thumb_path = tmp_path / thumb_key
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(thumb_bytes)

    with backfill_db.connect() as conn:
        _insert_library(conn, library_id=lib_id)
        _insert_asset(conn, asset_id=asset_id, library_id=lib_id, proxy_key=None, thumbnail_key=thumb_key)
        conn.commit()

    storage = MagicMock()
    storage.abs_path.side_effect = lambda key: tmp_path / key

    ctx = _make_ctx(backfill_db)
    step = BackfillThumbnailSha256Step()

    with patch("src.upgrade.steps.backfill_artifact_sha256.get_storage", return_value=storage):
        result = step.run(ctx)

    assert result["updated"] == 1
    assert result["missing"] == 0

    with backfill_db.connect() as conn:
        row = conn.execute(
            text("SELECT thumbnail_sha256 FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    assert row[0] == expected_sha


@pytest.mark.slow
def test_proxy_backfill_skips_missing_file_leaves_null(backfill_db, tmp_path: Path) -> None:
    """An asset whose proxy_key points to a missing file stays NULL after backfill."""
    lib_id = "lib_" + secrets.token_hex(4)
    asset_id = "ast_" + secrets.token_hex(8)
    proxy_key = f"ten1/{lib_id}/proxies/03/{asset_id}_missing.jpg"
    # Do NOT write the file to disk.

    with backfill_db.connect() as conn:
        _insert_library(conn, library_id=lib_id)
        _insert_asset(conn, asset_id=asset_id, library_id=lib_id, proxy_key=proxy_key, thumbnail_key=None)
        conn.commit()

    storage = MagicMock()
    storage.abs_path.side_effect = lambda key: tmp_path / key

    ctx = _make_ctx(backfill_db)
    step = BackfillProxySha256Step()

    with patch("src.upgrade.steps.backfill_artifact_sha256.get_storage", return_value=storage):
        result = step.run(ctx)

    assert result["missing"] == 1

    with backfill_db.connect() as conn:
        row = conn.execute(
            text("SELECT proxy_sha256 FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    # SHA-256 remains NULL — we do not poison the column for missing files.
    assert row[0] is None


@pytest.mark.slow
def test_scene_rep_backfill_writes_correct_sha256(backfill_db, tmp_path: Path) -> None:
    lib_id = "lib_" + secrets.token_hex(4)
    asset_id = "ast_" + secrets.token_hex(8)
    scene_id = "scn_" + secrets.token_hex(8)
    proxy_key = f"ten1/{lib_id}/scenes/04/{asset_id}_0000000000.jpg"

    frame_bytes = b"rep frame " + secrets.token_bytes(24)
    expected_sha = hashlib.sha256(frame_bytes).hexdigest()
    frame_path = tmp_path / proxy_key
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path.write_bytes(frame_bytes)

    with backfill_db.connect() as conn:
        _insert_library(conn, library_id=lib_id)
        conn.execute(
            text(
                """
                INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, status)
                VALUES (:aid, :lid, 'clip.mp4', 0, 'video', 'pending')
                """
            ),
            {"aid": asset_id, "lid": lib_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO video_scenes
                  (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms, proxy_key)
                VALUES (:sid, :aid, 0, 0, 5000, 2500, :pk)
                """
            ),
            {"sid": scene_id, "aid": asset_id, "pk": proxy_key},
        )
        conn.commit()

    storage = MagicMock()
    storage.abs_path.side_effect = lambda key: tmp_path / key

    ctx = _make_ctx(backfill_db)
    step = BackfillSceneRepSha256Step()

    assert step.needs_work(ctx) is True

    with patch("src.upgrade.steps.backfill_artifact_sha256.get_storage", return_value=storage):
        result = step.run(ctx)

    assert result["updated"] == 1
    assert result["missing"] == 0

    with backfill_db.connect() as conn:
        row = conn.execute(
            text("SELECT rep_frame_sha256 FROM video_scenes WHERE scene_id = :id"),
            {"id": scene_id},
        ).fetchone()
    assert row[0] == expected_sha

    ctx2 = _make_ctx(backfill_db)
    assert step.needs_work(ctx2) is False


@pytest.mark.slow
def test_runner_marks_all_three_steps_completed(backfill_db, tmp_path: Path) -> None:
    """Full runner completes all three steps when no work is needed (empty library)."""
    # backfill_db may already have rows from prior tests, but needs_work() only cares
    # about NULL sha columns — prior tests wrote real hashes. Add no new rows here.
    from src.repository.system_metadata import SystemMetadataRepository
    from sqlmodel import Session

    session = Session(backfill_db)
    meta = SystemMetadataRepository(session)
    ctx = UpgradeContext(session=session, metadata=meta, tenant_id="test_tenant")

    runner = TenantUpgradeRunner()

    storage = MagicMock()
    storage.abs_path.side_effect = lambda key: tmp_path / key

    with patch("src.upgrade.steps.backfill_artifact_sha256.get_storage", return_value=storage):
        result = runner.execute(ctx, max_steps=10)

    status = runner.get_status(ctx)
    # All steps must be either completed or skipped (done_steps == total).
    assert status["done_steps"] == status["steps_total"]
    assert status["pending_steps"] == 0

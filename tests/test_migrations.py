"""Control plane migration tests: upgrade/downgrade against a fresh Postgres."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer
from uuid import uuid4


@pytest.mark.migration
def test_control_plane_migrations_upgrade_and_downgrade() -> None:
    """Run control plane migrations up and down on a fresh Postgres with pgvector."""
    # pgvector image so we can enable the extension (control plane may not use it; tenant DB does)
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        # Ensure psycopg2 driver for Alembic
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()

        env = os.environ.copy()
        env["ALEMBIC_CONTROL_URL"] = url

        # Upgrade to head
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic-control.ini", "upgrade", "head"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        # Assert all three tables exist
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN ('tenants', 'api_keys', 'tenant_db_routing')"
                )
            )
            tables = {row[0] for row in r}
        assert tables == {"tenants", "api_keys", "tenant_db_routing"}, tables

        # Downgrade to base
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic-control.ini", "downgrade", "base"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        # Assert all three tables are gone
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN ('tenants', 'api_keys', 'tenant_db_routing')"
                )
            )
            tables = {row[0] for row in r}
        assert tables == set(), tables


@pytest.mark.migration
def test_api_keys_is_admin_backfilled() -> None:
    """
    Control plane: api_keys table has label + is_admin, and existing rows are backfilled with is_admin = TRUE.
    """
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()

        env = os.environ.copy()
        env["ALEMBIC_CONTROL_URL"] = url

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Upgrade to head
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic-control.ini", "upgrade", "head"],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        with engine.connect() as conn:
            # Insert a legacy-style key row (is_admin should default TRUE via backfill).
            conn.execute(
                text(
                    """
                    INSERT INTO tenants (tenant_id, name, plan, status, created_at)
                    VALUES ('ten_test', 'Test', 'free', 'active', NOW())
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO api_keys (key_id, key_hash, tenant_id, name, scopes, created_at)
                    VALUES ('key_test', 'hash', 'ten_test', 'default', '["read","write"]'::jsonb, NOW())
                    """
                )
            )
            conn.commit()

            r = conn.execute(
                text("SELECT label, is_admin FROM api_keys WHERE key_id = 'key_test'")
            )
            row = r.fetchone()
            assert row is not None
            label, is_admin = row
            assert is_admin is True


TENANT_TABLES = [
    "libraries",
    "scans",
    "assets",
    "video_scenes",
    "video_index_chunks",
    "asset_metadata",
    "search_sync_queue",
    "worker_jobs",
    "system_metadata",
    "faces",
    "people",
    "face_person_matches",
    "pipeline_locks",
]


@pytest.mark.migration
def test_tenant_schema_upgrade_and_downgrade() -> None:
    """Run tenant migrations up and down on a fresh Postgres with pgvector."""
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()

        env = os.environ.copy()
        env["ALEMBIC_TENANT_URL"] = url

        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "upgrade", "heads"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        with engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('libraries', 'scans', 'assets', 'video_scenes', 'video_index_chunks', "
                    "'asset_metadata', 'search_sync_queue', 'worker_jobs', 'system_metadata', "
                    "'faces', 'people', 'face_person_matches', 'pipeline_locks')"
                )
            )
            tables = {row[0] for row in r}
        assert set(TENANT_TABLES) == tables, tables

        # Verify search_sync_latest view exists (from our migration)
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.views "
                    "WHERE table_schema = 'public' AND table_name = 'search_sync_latest'"
                )
            )
            views = {row[0] for row in r}
        assert "search_sync_latest" in views, "search_sync_latest view should exist"

        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "downgrade", "base"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        with engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('libraries', 'scans', 'assets', 'video_scenes', 'video_index_chunks', "
                    "'asset_metadata', 'search_sync_queue', 'worker_jobs', 'system_metadata', "
                    "'faces', 'people', 'face_person_matches', 'pipeline_locks')"
                )
            )
            tables = {row[0] for row in r}
        assert tables == set(), tables


@pytest.mark.migration
def test_migration_marks_invalid_ai_vision_missing_proxy_failed() -> None:
    """
    Regression for x1y2z3a4b5c6:
    - If an ai_vision job exists for an asset with NULL proxy_key (or non-image media_type),
      upgrade should mark it failed with a stable error_message.
    - Downgrade should restore those rows to pending and clear error_message/completed_at.
    """
    before_rev = "u2v3w4x5y6z7"
    target_rev = "x1y2z3a4b5c6"
    err = "Invalid ai_vision job: missing proxy_key (video proxy deferred or proxy not ready)"

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()

        env = os.environ.copy()
        env["ALEMBIC_TENANT_URL"] = url
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Upgrade to the revision *before* the data-fix migration.
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "upgrade", before_rev],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        # Insert a library, assets, and ai_vision jobs that are invalid vs valid.
        lib_id = "lib_" + uuid4().hex
        bad_img_id = "ast_" + uuid4().hex
        bad_vid_id = "ast_" + uuid4().hex
        good_img_id = "ast_" + uuid4().hex

        bad_img_job = "job_" + uuid4().hex
        bad_vid_job = "job_" + uuid4().hex
        good_img_job = "job_" + uuid4().hex

        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO libraries (library_id, name, root_path, status, scan_status, vision_model_id, created_at, updated_at)
                    VALUES (:id, 'migrate-test', '/tmp', 'active', 'idle', 'moondream', NOW(), NOW())
                    """
                ),
                {"id": lib_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, availability, status, created_at, updated_at, proxy_key, thumbnail_key)
                    VALUES
                      (:bad_img, :lib, 'bad_img.jpg', 1000, 'image/jpeg', 'online', 'pending', NOW(), NOW(), NULL, NULL),
                      (:bad_vid, :lib, 'bad_vid.mov', 2000, 'video/quicktime', 'online', 'pending', NOW(), NOW(), NULL, NULL),
                      (:good_img, :lib, 'good_img.jpg', 1000, 'image/jpeg', 'online', 'pending', NOW(), NOW(), 'proxy/good.jpg', 'thumb/good.jpg')
                    """
                ),
                {"bad_img": bad_img_id, "bad_vid": bad_vid_id, "good_img": good_img_id, "lib": lib_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO worker_jobs (job_id, job_type, asset_id, status, priority, created_at)
                    VALUES
                      (:j1, 'ai_vision', :bad_img, 'pending', 10, NOW()),
                      (:j2, 'ai_vision', :bad_vid, 'claimed', 10, NOW()),
                      (:j3, 'ai_vision', :good_img, 'pending', 10, NOW())
                    """
                ),
                {"j1": bad_img_job, "j2": bad_vid_job, "j3": good_img_job, "bad_img": bad_img_id, "bad_vid": bad_vid_id, "good_img": good_img_id},
            )
            conn.commit()

        # Upgrade to head; migration should rewrite invalid jobs.
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "upgrade", target_rev],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT job_id, status, error_message, completed_at FROM worker_jobs WHERE job_id IN (:a,:b,:c)"),
                {"a": bad_img_job, "b": bad_vid_job, "c": good_img_job},
            ).fetchall()
            by_id = {r[0]: r for r in rows}

            assert by_id[bad_img_job][1] == "failed"
            assert by_id[bad_img_job][2] == err
            assert by_id[bad_img_job][3] is not None

            assert by_id[bad_vid_job][1] == "failed"
            assert by_id[bad_vid_job][2] == err
            assert by_id[bad_vid_job][3] is not None

            # Valid image job should be untouched.
            assert by_id[good_img_job][1] == "pending"
            assert by_id[good_img_job][2] is None

        # Downgrade back to before_rev; rows we changed should be restored to pending.
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "downgrade", before_rev],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT job_id, status, error_message, completed_at FROM worker_jobs WHERE job_id IN (:a,:b,:c)"),
                {"a": bad_img_job, "b": bad_vid_job, "c": good_img_job},
            ).fetchall()
            by_id = {r[0]: r for r in rows}

            assert by_id[bad_img_job][1] == "pending"
            assert by_id[bad_img_job][2] is None
            assert by_id[bad_img_job][3] is None

            assert by_id[bad_vid_job][1] == "pending"
            assert by_id[bad_vid_job][2] is None
            assert by_id[bad_vid_job][3] is None

            assert by_id[good_img_job][1] == "pending"
            assert by_id[good_img_job][2] is None

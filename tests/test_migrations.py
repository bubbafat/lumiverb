"""Control plane migration tests: upgrade/downgrade against a fresh Postgres."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer


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


TENANT_TABLES = [
    "libraries",
    "scans",
    "assets",
    "video_scenes",
    "asset_metadata",
    "search_sync_queue",
    "worker_jobs",
    "system_metadata",
    "faces",
    "people",
    "face_person_matches",
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
            [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "upgrade", "head"],
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
                    "('libraries', 'scans', 'assets', 'video_scenes', 'asset_metadata', "
                    "'search_sync_queue', 'worker_jobs', 'system_metadata', "
                    "'faces', 'people', 'face_person_matches')"
                )
            )
            tables = {row[0] for row in r}
        assert set(TENANT_TABLES) == tables, tables

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
                    "('libraries', 'scans', 'assets', 'video_scenes', 'asset_metadata', "
                    "'search_sync_queue', 'worker_jobs', 'system_metadata', "
                    "'faces', 'people', 'face_person_matches')"
                )
            )
            tables = {row[0] for row in r}
        assert tables == set(), tables

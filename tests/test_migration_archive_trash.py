"""Migration b7e4c2a9d013: libraries in the trash get trashed_at, so their
trash days can be counted (Robert's archive and trash model, Oct 8).

Runs Alembic against a throwaway testcontainers database only.
"""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "ef56ab12cd34", "b7e4c2a9d013"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_TENANT_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", *args],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.migration
def test_trashed_libraries_start_counting_and_the_column_goes_on_downgrade() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            for lib, status in (("lib_gone", "trashed"), ("lib_live", "active")):
                conn.execute(text(
                    "INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                    " VALUES (:id, :id, '/' || :id, :status, now(), now())"
                ), {"id": lib, "status": status})

        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            rows = dict(conn.execute(text("SELECT library_id, trashed_at IS NOT NULL FROM libraries")).all())
        assert rows == {"lib_gone": True, "lib_live": False}

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            cols = conn.execute(text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'libraries' AND column_name = 'trashed_at'"
            )).all()
        assert cols == []
        engine.dispose()

"""Tenant migration f2b6d8a4c1e9: a reader's check-in keeps why it holds its
remakes back ("battery" or "metered", nothing else), and a downgrade drops
it. Runs Alembic against a throwaway testcontainers database only."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "e9a4c2f7b1d3", "f2b6d8a4c1e9"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_TENANT_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", *args],
                            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.migration
def test_a_check_in_keeps_why_its_remakes_wait_and_a_downgrade_drops_it() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
            conn.execute(text("INSERT INTO reader_check_ins (library_id, reader, client, version, state,"
                              " waiting_files, waiting_bytes, mark, checked_in_at)"
                              " VALUES ('lib_a', 'server', 'this server', '1', 'idle', 0, 0, 0, now())"))
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT remakes_held FROM reader_check_ins")).scalar() is None
        with engine.begin() as conn:
            conn.execute(text("UPDATE reader_check_ins SET remakes_held = 'battery'"))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE reader_check_ins SET remakes_held = 'tired'"))
        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'reader_check_ins'"))}
        assert "remakes_held" not in columns and "state" in columns
        engine.dispose()

"""Tenant migration d7f3b9e5c2a8: a library's reader is held to this server
or a device's id; a downgrade hands every library back to this server
before dropping the check. Runs Alembic against a throwaway testcontainers
database only."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "c6d2e8a4f1b7", "d7f3b9e5c2a8"
DEVICE = "dev_01M55ZW219FFW62X115FQ2FV6C"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_TENANT_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", *args],
                            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.migration
def test_a_reader_is_this_server_or_a_device_and_a_downgrade_hands_them_back() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
        _alembic(url, "upgrade", AFTER)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE libraries SET reader = 'a laptop'"))
        with engine.begin() as conn:
            conn.execute(text("UPDATE libraries SET reader = :d"), {"d": DEVICE})
        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT reader FROM libraries")).scalar() == "server"
        engine.dispose()

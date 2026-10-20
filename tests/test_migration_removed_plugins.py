"""Control migration d1f5a9c3e7b2: a plug-in uninstalled with what it made
kept or in the trash keeps its row in removed_plugins, and the install log
says when a trashed one is deleted for good (`purge`). Up and down again.
Alembic against a throwaway database only."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2

pytestmark = pytest.mark.slow
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "b5d9e3a7f1c4", "d1f5a9c3e7b2"
REMOVED = ("INSERT INTO removed_plugins (plugin_id, version, manifest, on_by_default, permissions, kept, removed_by,"
           " removed_at, purge_after) VALUES ('facts', '1', '{}', true, '{}', :kept, 'robert', now(), :purge)")
LOGGED = "INSERT INTO plugin_install_log (plugin_id, action, by, at) VALUES ('facts', :a, 'upkeep', now())"


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-control.ini", *args], cwd=PROJECT_ROOT,
                            env={**os.environ, "ALEMBIC_CONTROL_URL": url}, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_removed_plug_ins_are_kept_apart_and_a_purge_is_logged_up_and_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(LOGGED), {"a": "purge"})

        _alembic(url, "upgrade", AFTER)
        with engine.begin() as conn:
            conn.execute(text(REMOVED), {"kept": "trash", "purge": "2026-11-19T00:00:00Z"})
            conn.execute(text(LOGGED), {"a": "purge"})
        for kept, purge in (("keep", "2026-11-19T00:00:00Z"), ("trash", None), ("gone", None)):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("DELETE FROM removed_plugins"))
                conn.execute(text(REMOVED), {"kept": kept, "purge": purge})

        _alembic(url, "downgrade", BEFORE)
        assert "removed_plugins" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM plugin_install_log WHERE action = 'purge'")).scalar_one() == 0
        _alembic(url, "upgrade", AFTER)
        engine.dispose()

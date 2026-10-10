"""Migration 4e3bcb0d9be8: one pause table (ADR-016 phase 4).

processing_paused (a switch's work) and producer_redo_paused (a producer's
redo) become producer_pauses, a row per (target, scope), each keeping who
paused it and when (a new version's stop, by no one, stays by no one).
Going down puts them back. Runs Alembic against a throwaway database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "a7c3e9d1f2b4", "4e3bcb0d9be8"


@pytest.mark.migration
def test_both_pauses_become_one_table_and_come_back() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO processing_paused (target, paused_by, paused_at)"
                              " VALUES ('scans', 'usr_1', now()), ('ocr', 'usr_2', now())"))
            conn.execute(text("INSERT INTO producer_redo_paused (artifact, paused_by, paused_at)"
                              " VALUES ('ocr', 'usr_3', now()), ('faces', NULL, now())"))
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            rows = {(r[0], r[1]): r[2] for r in conn.execute(text(
                "SELECT target, scope, paused_by FROM producer_pauses"))}
            tables = {r[0] for r in conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        assert rows == {("scans", "work"): "usr_1", ("ocr", "work"): "usr_2", ("ocr", "redo"): "usr_3",
                        ("faces", "redo"): None}
        assert not tables & {"processing_paused", "producer_redo_paused"}
        with pytest.raises(Exception, match="ck_producer_pauses_scope"), engine.begin() as conn:
            conn.execute(text("INSERT INTO producer_pauses (target, scope, paused_at) VALUES ('x', 'all', now())"))
        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            work = dict(conn.execute(text("SELECT target, paused_by FROM processing_paused")).all())
            redo = dict(conn.execute(text("SELECT artifact, paused_by FROM producer_redo_paused")).all())
        assert work == {"scans": "usr_1", "ocr": "usr_2"} and redo == {"ocr": "usr_3", "faces": None}
        engine.dispose()

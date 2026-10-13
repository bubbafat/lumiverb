"""Migration b3d5f7a9c1e4: a scan keeps what it answered (scan_sessions.answers).

A scan made before has none (a call it answered then, sent again, is a 409
as before); a scan's answers are kept as JSON. Going back down drops the
column and keeps the scans. Runs Alembic against a throwaway
testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "e7b9d1f3a5c2", "b3d5f7a9c1e4"


def _columns(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'scan_sessions'"))}


@pytest.mark.migration
def test_a_scan_keeps_its_answers_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at,"
                              " is_public, revision) VALUES ('lib_1', 'L', '/l', 'active', now(), now(), false, 0)"))
            conn.execute(text("INSERT INTO scan_sessions (scan_id, library_id, started_at, expires_at)"
                              " VALUES ('scan_old', 'lib_1', now(), now() + interval '1 day')"))
        assert "answers" not in _columns(engine)

        _alembic(url, "upgrade", AFTER)
        with engine.begin() as conn:
            assert conn.execute(text("SELECT answers FROM scan_sessions WHERE scan_id = 'scan_old'")).scalar() is None
            conn.execute(text("UPDATE scan_sessions SET answers = CAST(:a AS jsonb) WHERE scan_id = 'scan_old'"),
                         {"a": '{"finish": {"archived": 2, "held_back_reason": null}}'})
            kept = conn.execute(text("SELECT answers FROM scan_sessions WHERE scan_id = 'scan_old'")).scalar()
        assert kept == {"finish": {"archived": 2, "held_back_reason": None}}

        _alembic(url, "downgrade", BEFORE)
        assert "answers" not in _columns(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM scan_sessions")).scalar() == 1
        engine.dispose()
